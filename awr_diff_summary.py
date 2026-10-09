#!/usr/bin/env python3
"""
awr_diff_summary.py
===================

Turn an Oracle AWR **Compare Period** (a.k.a. "AWR Diff") HTML report into a
concise **executive summary** in Markdown.

The input is the HTML produced by `awrrpt`/`awrdiff` when two snapshot ranges
are compared (a "Workload Repository Compare Period Report").  This tool is a
pure-stdlib tool: it needs only Python 3.8+ and no third-party packages.

Usage
-----
    python3 awr_diff_summary.py report.html                 # print to stdout
    python3 awr_diff_summary.py report.html -o summary.md   # write to a file
    python3 awr_diff_summary.py report.html --top 10        # more top-N rows
    python3 awr_diff_summary.py report.html --dump-json j   # also dump parsed model

See README.md for the full option list and a description of what is extracted.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from html.parser import HTMLParser


# --------------------------------------------------------------------------- #
# 1.  Minimal dependency-free HTML DOM                                        #
# --------------------------------------------------------------------------- #

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent", "idx")

    def __init__(self, tag, attrs=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []      # list of Node or str
        self.parent = None
        self.idx = 0            # pre-order document index


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack = [self.root]
        self._n = 0

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        self._n += 1
        node.idx = self._n
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        # self-closing, e.g. <br/> ; add a leaf node and never push it
        node = Node(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        self._n += 1
        node.idx = self._n

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        # pop until we match (tolerates the malformed HTML AWR emits)
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def build_dom(text):
    b = _Builder()
    b.feed(text)
    b.close()
    return b.root


def _iter_nodes(node):
    yield node
    for c in node.children:
        if isinstance(c, Node):
            yield from _iter_nodes(c)


def node_text(node):
    """All descendant text of *node*, whitespace-collapsed."""
    parts = []
    for c in node.children:
        if isinstance(c, str):
            parts.append(c)
        else:
            parts.append(node_text(c))
    s = "".join(parts)
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def find_all(node, tag):
    return [n for n in _iter_nodes(node) if n.tag == tag]


# --------------------------------------------------------------------------- #
# 2.  Small helpers                                                           #
# --------------------------------------------------------------------------- #

def clean(s):
    if s is None:
        return None
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if s in ("", "-", "&#160;", "&nbsp;"):
        return None
    return s


def num(s):
    """Parse the first number out of a cell like '1,080,020.62' / '338.51us'."""
    s = clean(s)
    if not s:
        return None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", s.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def disp(s):
    c = clean(s)
    return c if c else "-"


def truncate(s, n):
    s = clean(s) or ""
    return s if len(s) <= n else s[: n - 1] + "\u2026"


# --------------------------------------------------------------------------- #
# 3.  Table model                                                             #
# --------------------------------------------------------------------------- #

class Table:
    def __init__(self, tbl_node):
        self.node = tbl_node
        self.idx = tbl_node.idx
        self.trs = find_all(tbl_node, "tr")
        # (tag, text, colspan, id) for every cell in a row
        self._raw = [self._row_cells(tr) for tr in self.trs]

        # header rows = leading rows that contain at least one <th> (max 2)
        h = 0
        if self._raw and any(t == "th" for (t, *_ ) in self._raw[0]):
            h = 1
            if len(self._raw) > 1 and any(t == "th" for (t, *_) in self._raw[1]):
                h = 2
        self.header_rows = self._raw[:h]
        self.data_raw = self._raw[h:]

        self.cols = self._expand(self.header_rows)       # list of (group, leaf)
        self.rows = [self._data_texts(r) for r in self.data_raw]

        self.colmap = {}
        for j, (g, l) in enumerate(self.cols):
            self.colmap.setdefault((g or "", l or ""), j)
        self.header_text = " ".join(
            t for row in self.header_rows for (_t, t, _c, _i) in row
        )

    def _row_cells(self, tr):
        cells = []
        for c in tr.children:
            if isinstance(c, Node) and c.tag in ("th", "td"):
                try:
                    cs = int(c.attrs.get("colspan", "1") or 1)
                except ValueError:
                    cs = 1
                cells.append((c.tag, node_text(c), cs, c.attrs.get("id") or ""))
        return cells

    def _expand(self, header_rows):
        def leaves(row):
            return [(t, cs) for (_tag, t, cs, _i) in row]

        if not header_rows:
            return []
        if len(header_rows) == 1:
            cols = []
            for text, cs in leaves(header_rows[0]):
                for _ in range(cs):
                    cols.append(("", text))
            return cols

        top, bot = leaves(header_rows[0]), leaves(header_rows[1])
        n = sum(cs for _t, cs in bot)
        leaf_list = [t for t, cs in bot for _ in range(cs)]
        groups = [None] * n
        i = 0
        for text, cs in top:
            for _ in range(cs):
                if i < n:
                    groups[i] = text or None
                i += 1
        return [(groups[j], leaf_list[j]) for j in range(n)]

    def _data_texts(self, row):
        return [t for (tag, t, _c, _i) in row if tag == "td"]

    # -- accessors ---------------------------------------------------------- #
    def cell(self, row, group, leaf):
        j = self.colmap.get((group or "", leaf or ""))
        if j is None or j >= len(row):
            return None
        return clean(row[j])

    def find_col(self, group_sub, leaf):
        """First column index whose group contains *group_sub* and leaf==leaf."""
        for (g, l), j in self.colmap.items():
            if (g or "").lower() == group_sub.lower() and (l or "") == leaf:
                return j
        # substring fallback
        for (g, l), j in self.colmap.items():
            if group_sub.lower() in (g or "").lower() and (l or "") == leaf:
                return j
        return None

    def cell_by_group(self, row, group_sub, leaf):
        j = self.find_col(group_sub, leaf)
        if j is None or j >= len(row):
            return None
        return clean(row[j])

    def cell_leaf(self, row, leaf, nth=0):
        idxs = [j for j, (g, l) in enumerate(self.cols) if (l or "") == leaf]
        if not idxs:
            return None
        j = idxs[min(nth, len(idxs) - 1)]
        return clean(row[j]) if j < len(row) else None


# --------------------------------------------------------------------------- #
# 4.  Document model                                                          #
# --------------------------------------------------------------------------- #

class Report:
    def __init__(self, path):
        self.path = path
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            self.raw = fh.read()
        self.dom = build_dom(self.raw)
        self.title = node_text(find_all(self.dom, "title")[0]) \
            if find_all(self.dom, "title") else os.path.basename(path)
        self.tables = [Table(t) for t in find_all(self.dom, "table")]
        self.headings = []  # (text, node)
        for tag in ("h2", "h3"):
            for n in find_all(self.dom, tag):
                self.headings.append((node_text(n), n))
        self.headings.sort(key=lambda x: x[1].idx)
        # attach a preceding title to every table
        for t in self.tables:
            t.title = self._preceding_title(t.idx)

    def _preceding_title(self, idx):
        best = None
        for text, n in self.headings:
            if n.idx < idx:
                best = text
            else:
                break
        return best

    def table_under(self, title):
        for t in self.tables:
            if t.title == title:
                return t
        # fuzzy: unique substring
        hits = [t for t in self.tables if t.title and title in t.title]
        return hits[0] if len(hits) == 1 else None

    def table_by_sig(self, sig):
        for t in self.tables:
            if sig in t.header_text:
                return t
        return None

    def table_where_rowlabel(self, prefix):
        """First table that has a data row whose label cell starts with *prefix*."""
        for t in self.tables:
            for row in t.rows:
                if row and (clean(row[0]) or "").rstrip(":").startswith(prefix):
                    return t
        return None

    def tables_after(self, title, limit=None):
        h = self._heading_node(title)
        if h is None:
            return []
        out = [t for t in self.tables if t.idx > h.idx]
        # cut off at next heading
        nxt = [n.idx for _t, n in self.headings if n.idx > h.idx]
        if nxt:
            out = [t for t in out if t.idx < min(nxt)]
        return out[:limit] if limit else out

    def _heading_node(self, title):
        for text, n in self.headings:
            if text == title:
                return n
        hits = [n for text, n in self.headings if title in text]
        return hits[0] if len(hits) == 1 else None


# --------------------------------------------------------------------------- #
# 5.  Section extractors  (each returns a small plain dict; None if missing)  #
# --------------------------------------------------------------------------- #

def e_db_info(rpt):
    t = rpt.table_by_sig("Std Block Size")
    if not t:
        return None
    out = {"1st": {}, "2nd": {}}
    for row in t.rows:
        label = clean(row[0]) or ""
        key = "1st" if "1st" in label or "First" in label else \
              ("2nd" if "2nd" in label or "Second" in label else None)
        if not key:
            continue
        for col in ("DB Name", "Unique Name", "DB Role", "Edition",
                    "Release", "Cluster", "CDB", "Host"):
            out[key][col] = t.cell(row, "", col)
    return out


def e_snapshots(rpt):
    t = rpt.table_by_sig("Begin Snap Time")
    if not t:
        return None
    out = {}
    for row in t.rows:
        label = (clean(row[0]) or "").strip()
        if label == "1st":
            key = "1st"
        elif label == "2nd":
            key = "2nd"
        else:
            continue
        out[key] = {
            "begin_id": t.cell(row, "", "Begin Snap Id"),
            "begin_time": t.cell(row, "", "Begin Snap Time"),
            "end_id": t.cell(row, "", "End Snap Id"),
            "end_time": t.cell(row, "", "End Snap Time"),
            "active_users": t.cell(row, "", "Avg Active Users"),
            "elapsed_min": t.cell(row, "", "Elapsed Time (min)"),
            "db_time_min": t.cell(row, "", "DB time (min)"),
        }
    return out or None


def e_instances(rpt):
    t = rpt.table_by_sig("Inst num")
    if not t:
        return None
    out = {}
    for row in t.rows:
        label = (clean(row[0]) or "")
        key = "1st" if "1st" in label or "First" in label else \
              ("2nd" if "2nd" in label or "Second" in label else None)
        if key:
            out[key] = {"instance": t.cell(row, "", "Instance"),
                        "num": t.cell(row, "", "Inst num")}
    return out or None


def e_host(rpt):
    t = rpt.table_where_rowlabel("Number of CPUs")
    if not t:
        return None
    wanted = ["Number of CPUs", "Number of CPU Cores", "Number of CPU Sockets",
              "Physical Memory", "Load at Start Snapshot", "Load at End Snapshot",
              "%User Time", "%System Time", "%Idle Time", "%IO Wait Time"]
    rows = {}
    for row in t.rows:
        label = (clean(row[0]) or "").rstrip(":")
        if label in wanted:
            rows[label] = {"1st": t.cell_leaf(row, "1st"),
                           "2nd": t.cell_leaf(row, "2nd"),
                           "pct": t.cell_leaf(row, "%Diff")}
    return rows or None


def e_cache(rpt):
    t = rpt.table_by_sig("Diff (M)")
    if not t:
        return None
    wanted = ["SGA Target", "Buffer Cache", "Shared Pool", "Large Pool",
              "Java Pool", "PGA Target", "Log Buffer", "In-Memory Area"]
    rows = {}
    for row in t.rows:
        label = (clean(row[0]) or "")
        if label in wanted:
            rows[label] = {"1st": t.cell_leaf(row, "1st (M)"),
                           "2nd": t.cell_leaf(row, "2nd (M)"),
                           "pct": t.cell_leaf(row, "%Diff")}
    return rows or None


_LOAD_WANTED = ["DB time", "CPU time", "Redo size (bytes)",
                "Logical read (blocks)", "Physical read (blocks)",
                "Physical write (blocks)", "User calls", "Parses (SQL)",
                "Hard parses (SQL)", "Transactions", "Commits", "Rollbacks"]


def e_load(rpt):
    t = rpt.table_by_sig("1st per sec")
    if not t:
        return None
    rows = []
    for row in t.rows:
        label = (clean(row[0]) or "").rstrip(":")
        if label in _LOAD_WANTED:
            rows.append({
                "label": label,
                "psec1": t.cell_leaf(row, "1st per sec"),
                "psec2": t.cell_leaf(row, "2nd per sec"),
                "psec_pct": t.cell_leaf(row, "%Diff", nth=0),
                "ptxn1": t.cell_leaf(row, "1st per txn"),
                "ptxn2": t.cell_leaf(row, "2nd per txn"),
            })
    return rows or None


def e_top_events(rpt):
    t = rpt.table_under("Top Timed Events")
    if not t:
        return None
    left, right = [], []
    for row in t.rows:
        if len(row) < 12:
            continue
        left.append({"event": clean(row[0]), "class": clean(row[1]),
                     "time": clean(row[3]), "pct": clean(row[5])})
        right.append({"event": clean(row[6]), "class": clean(row[7]),
                      "time": clean(row[9]), "pct": clean(row[11])})
    return {"1st": left, "2nd": right}


def e_time_model(rpt, n):
    t = rpt.table_under("Time Model Statistics")
    if not t:
        return None
    rows = []
    for row in t.rows:
        label = clean(row[0])
        if not label:
            continue
        rows.append({
            "stat": label,
            "p1": t.cell_by_group(row, "% of DB time", "1st"),
            "p2": t.cell_by_group(row, "% of DB time", "2nd"),
            "diff": t.cell_by_group(row, "% of DB time", "Diff"),
        })
    rows.sort(key=lambda r: abs(num(r["diff"]) or 0), reverse=True)
    return rows[:n]


def e_wait_classes(rpt, n):
    t = rpt.table_under("Wait Classes")
    if not t:
        return None
    rows = []
    for row in t.rows:
        label = clean(row[0])
        if not label:
            continue
        rows.append({
            "class": label,
            "p1": t.cell_by_group(row, "% of DB time", "1st"),
            "p2": t.cell_by_group(row, "% of DB time", "2nd"),
            "diff": t.cell_by_group(row, "% of DB time", "Diff"),
        })
    rows.sort(key=lambda r: abs(num(r["diff"]) or 0), reverse=True)
    return rows[:n]


def e_wait_events(rpt, n):
    t = rpt.table_under("Wait Events")
    if not t:
        return None
    rows = []
    for row in t.rows:
        event = clean(row[0])
        if not event:
            continue
        rows.append({
            "event": event,
            "class": clean(row[1]),
            "p1": t.cell_by_group(row, "% of DB time", "1st"),
            "p2": t.cell_by_group(row, "% of DB time", "2nd"),
            "diff": t.cell_by_group(row, "% of DB time", "Diff"),
            "avg2": t.cell_by_group(row, "Avg Wait Time", "2nd"),
        })
    rows.sort(key=lambda r: abs(num(r["diff"]) or 0), reverse=True)
    return rows[:n]


def e_services(rpt, n):
    t = rpt.table_under("Service Statistics")
    if not t:
        return None
    rows = []
    for row in t.rows:
        name = clean(row[0])
        if not name:
            continue
        rows.append({
            "name": name,
            "db1": t.cell_by_group(row, "% of DB time", "1st"),
            "db2": t.cell_by_group(row, "% of DB time", "2nd"),
            "dbd": t.cell_by_group(row, "% of DB time", "Diff"),
            "cpud": t.cell_by_group(row, "% CPU Time", "Diff"),
        })
    rows.sort(key=lambda r: num(r["db2"]) or 0, reverse=True)
    return rows[:n]


def e_top_sql(rpt, title, n):
    t = rpt.table_under(title)
    if not t:
        return None
    # the "% share" group is the one that carries a "1st Total" / "2nd Total" leaf
    share_group = None
    for (g, l) in t.cols:
        if g and (l == "1st Total" or l == "2nd Total"):
            share_group = g
            break
    share_group = share_group or "% of DB time"
    rows = []
    for row in t.rows:
        sqlid = clean(row[0])
        if not sqlid:
            continue
        text = clean(row[-1]) if row else None
        rows.append({
            "id": sqlid,
            "p1": t.cell_by_group(row, share_group, "1st"),
            "p2": t.cell_by_group(row, share_group, "2nd"),
            "diff": t.cell_by_group(row, share_group, "Diff"),
            "exec1": t.cell_by_group(row, "#Executions", "1st"),
            "exec2": t.cell_by_group(row, "#Executions", "2nd"),
            "text": text,
        })
    rows.sort(key=lambda r: abs(num(r["diff"]) or 0), reverse=True)
    return rows[:n]


def e_wait_profile(rpt, n, period="2nd"):
    """Top non-idle wait events for *period*, ranked by % of DB time in that period."""
    t = rpt.table_under("Wait Events")
    if not t:
        return None
    rows = []
    for row in t.rows:
        event = clean(row[0])
        cls = clean(row[1])
        if not event or (cls or "").lower() == "idle":
            continue
        val = t.cell_by_group(row, "% of DB time", period)
        if val is None:
            continue
        rows.append({"event": event, "class": cls, "pct": val})
    rows.sort(key=lambda r: num(r["pct"]) or 0, reverse=True)
    return rows[:n]


_SEG_METRIC = {
    "Top Segments Comparison by Physical Reads": "% of Total Physical Reads",
    "Top Segments Comparison by Logical Reads": "% of Total Logical Reads",
    "Top Segments Comparison by Physical Write Requests": "% of Total Physical Write Requests",
}


def e_top_segments(rpt, title, n):
    t = rpt.table_under(title)
    if not t:
        return None
    group = _SEG_METRIC.get(title, "% of Total Physical Reads")
    rows = []
    for row in t.rows:
        owner = clean(row[0])
        obj = clean(row[2])
        if not obj:
            continue
        sub = clean(row[3])
        otype = clean(row[4])
        name = f"{owner}.{obj}" if owner else obj
        if sub:
            name += f" ({sub})"
        rows.append({
            "name": name,
            "type": otype,
            "p1": t.cell_by_group(row, group, "1st"),
            "p2": t.cell_by_group(row, group, "2nd"),
            "diff": t.cell_by_group(row, group, "Diff"),
            "abs1": t.cell_leaf(row, "1st", nth=1),
            "abs2": t.cell_leaf(row, "2nd", nth=1),
        })
    rows.sort(key=lambda r: abs(num(r["diff"]) or 0), reverse=True)
    return rows[:n]


def e_init_ora(rpt):
    t = rpt.table_under("init.ora Parameters")
    if not t:
        return None
    changed, hidden_changed = [], 0
    total = 0
    for row in t.rows:
        name = clean(row[0])
        if not name:
            continue
        total += 1
        b1 = t.cell(row, "1st Period", "Begin Snap Value") or ""
        e1 = t.cell(row, "1st Period", "End Snap Value") or ""
        e2 = t.cell(row, "2nd Period", "End Snap Value") or ""
        if e1 != e2:
            if name.startswith("_"):
                hidden_changed += 1
            else:
                changed.append({"name": name, "old": e1 or "(default)",
                                "new": e2 or "(default)"})
    return {"total": total, "changed": changed, "hidden_changed": hidden_changed}


def e_rac(rpt):
    out = {}
    t = rpt.table_under("RAC Statistics Summary")
    if t:
        for row in t.rows:
            label = (clean(row[0]) or "")
            out.setdefault("instances", {})[label.rstrip(":")] = {
                "begin": t.cell_leaf(row, "Begin"),
                "end": t.cell_leaf(row, "End"),
            }
    g = rpt.table_by_sig("GCS/GES messages")
    if g:
        out["cache_load"] = []
        for row in g.rows:
            label = (clean(row[0]) or "").rstrip(":")
            out["cache_load"].append({
                "label": label,
                "psec1": t_cell_leaf(g, row, "1st Per Second"),
                "psec2": t_cell_leaf(g, row, "2nd Per Second"),
                "pct": t_cell_leaf(g, row, "%Diff"),
            })
    eff = rpt.table_by_sig("local cache")
    if eff:
        out["buffer_access"] = []
        for row in eff.rows:
            out["buffer_access"].append({
                "label": (clean(row[0]) or "").rstrip(":"),
                "first": t_cell_leaf(eff, row, "First"),
                "second": t_cell_leaf(eff, row, "Second"),
                "diff": t_cell_leaf(eff, row, "Diff"),
            })
    return out or None


def t_cell_leaf(t, row, leaf):
    return t.cell_leaf(row, leaf)


# --------------------------------------------------------------------------- #
# 6.  Markdown rendering                                                      #
# --------------------------------------------------------------------------- #

def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join([" --- "] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def render(rpt, ex, top):
    L = []
    add = L.append
    dbi = ex.get("db_info") or {}
    a, b = dbi.get("1st", {}), dbi.get("2nd", {})
    db_name = a.get("DB Name") or b.get("DB Name") or "database"

    add(f"# AWR Diff Executive Summary — {db_name}")
    add("")
    add(f"> Generated by `awr_diff_summary.py` from `{os.path.basename(rpt.path)}`.  ")
    add("> Compare-period report: 1st snapshot range vs 2nd snapshot range.  ")
    add("> Figures are taken verbatim from the report; `%Diff` = change from 1st to 2nd period.")
    add("")

    # ---- 1. environment & version ----------------------------------------- #
    add("## 1. Environment & version")
    add("")
    if dbi:
        add(_md_table(
            ["", "1st period", "2nd period"],
            [
                ["DB name", disp(a.get("DB Name")), disp(b.get("DB Name"))],
                ["Instance", disp((ex.get("instances") or {}).get("1st", {}).get("instance")),
                 disp((ex.get("instances") or {}).get("2nd", {}).get("instance"))],
                ["Host", disp(a.get("Host")), disp(b.get("Host"))],
                ["Oracle release", disp(a.get("Release")), disp(b.get("Release"))],
                ["Edition", disp(a.get("Edition")), disp(b.get("Edition"))],
                ["DB role", disp(a.get("DB Role")), disp(b.get("DB Role"))],
                ["Cluster / CDB", f"{disp(a.get('Cluster'))} / {disp(a.get('CDB'))}",
                 f"{disp(b.get('Cluster'))} / {disp(b.get('CDB'))}"],
            ]))
        add("")
    if dbi and (a.get("Release") or b.get("Release")) and \
            clean(a.get("Release")) != clean(b.get("Release")):
        add(f"**Version change detected:** {disp(a.get('Release'))} → "
            f"{disp(b.get('Release'))}. This comparison spans a release "
            f"change (e.g. migration/upgrade), which materially affects "
            f"wait-event names, I/O behaviour and performance.")
        add("")

    snap = ex.get("snapshots") or {}
    if snap:
        s1, s2 = snap.get("1st", {}), snap.get("2nd", {})
        add(_md_table(
            ["", "1st period", "2nd period", "Δ"],
            [
                ["Snapshots", f"{disp(s1.get('begin_id'))}–{disp(s1.get('end_id'))}",
                 f"{disp(s2.get('begin_id'))}–{disp(s2.get('end_id'))}", ""],
                ["Window", disp(s1.get("begin_time")), disp(s2.get("begin_time")), ""],
                ["Elapsed (min)", disp(s1.get("elapsed_min")), disp(s2.get("elapsed_min")), ""],
                ["DB time (min)", disp(s1.get("db_time_min")), disp(s2.get("db_time_min")),
                 _pct_disp(s1.get("db_time_min"), s2.get("db_time_min"))],
                ["Avg active users", disp(s1.get("active_users")), disp(s2.get("active_users")),
                 _pct_disp(s1.get("active_users"), s2.get("active_users"))],
            ]))
        add("")

    # ---- 2. host / hardware ------------------------------------------------ #
    host = ex.get("host")
    if host:
        add("## 2. Host / hardware")
        add("")
        rows = []
        for k, v in host.items():
            rows.append([k, disp(v["1st"]), disp(v["2nd"]), _pct(v["pct"])])
        add(_md_table(["Metric", "1st", "2nd", "%Diff"], rows))
        add("")

    # ---- 3. workload headline --------------------------------------------- #
    load = ex.get("load")
    if load:
        add("## 3. Headline workload (per second)")
        add("")
        rows = [[d["label"], disp(d["psec1"]), disp(d["psec2"]),
                 _pct(d["psec_pct"]), disp(d["ptxn1"]), disp(d["ptxn2"])]
                for d in load]
        add(_md_table(["Metric / sec", "1st", "2nd", "%Diff", "1st /txn", "2nd /txn"], rows))
        add("")

    cache = ex.get("cache")
    if cache:
        add("## 4. Memory / cache sizing (MB)")
        add("")
        rows = [[k, disp(v["1st"]), disp(v["2nd"]), _pct(v["pct"])] for k, v in cache.items()]
        add(_md_table(["Pool", "1st", "2nd", "%Diff"], rows))
        add("")

    # ---- 5. time model ----------------------------------------------------- #
    tm = ex.get("time_model")
    if tm:
        add("## 5. Time model — biggest %DB-time movers")
        add("")
        rows = [[d["stat"], _pct(d["p1"]), _pct(d["p2"]), _pct(d["diff"])] for d in tm]
        add(_md_table(["Statistic", "1st %DB", "2nd %DB", "Δ"], rows))
        add("")

    # ---- 6. wait classes --------------------------------------------------- #
    wc = ex.get("wait_classes")
    if wc:
        add("## 6. Wait classes")
        add("")
        rows = [[d["class"], _pct(d["p1"]), _pct(d["p2"]), _pct(d["diff"])] for d in wc]
        add(_md_table(["Wait class", "1st %DB", "2nd %DB", "Δ"], rows))
        add("")

    # ---- 7. top wait events ------------------------------------------------ #
    we = ex.get("wait_events")
    if we:
        add("## 7. Top wait-event movers")
        add("")
        rows = [[d["event"], d["class"] or "", _pct(d["p1"]), _pct(d["p2"]),
                 _pct(d["diff"]), disp(d["avg2"])] for d in we]
        add(_md_table(["Event", "Class", "1st %DB", "2nd %DB", "Δ", "2nd avg"], rows))
        add("")

    # ---- 8. top timed events side by side ---------------------------------- #
    te = ex.get("top_events")
    if te:
        add("## 8. Top timed events (as reported)")
        add("")
        for side in ("1st", "2nd"):
            add(f"**{side} period**")
            rows = [[e["event"] or "", e["class"] or "", disp(e["pct"]) + " %DB",
                     disp(e["time"]) + "s"]
                    for e in te[side][:top] if e["event"]]
            if rows:
                add(_md_table(["Event", "Class", "Share", "Time"], rows))
            add("")

    # ---- 9. services ------------------------------------------------------- #
    sv = ex.get("services")
    if sv:
        add("## 9. Services")
        add("")
        rows = [[d["name"], _pct(d["db1"]), _pct(d["db2"]), _pct(d["dbd"]),
                 _pct(d["cpud"])] for d in sv]
        add(_md_table(["Service", "1st %DB", "2nd %DB", "Δ DB", "Δ CPU"], rows))
        add("")

    # ---- 10. top SQL ------------------------------------------------------- #
    add("## 10. Top SQL by elapsed time")
    _sql_section(rpt, ex, "Elapsed Time", top, add)
    add("")
    add("### Top SQL by CPU time")
    _sql_section(rpt, ex, "CPU Time", min(top, 5), add)
    add("")
    add("### Top SQL by I/O time")
    _sql_section(rpt, ex, "I/O Time", min(top, 5), add)
    add("")

    # ---- 11. top segments -------------------------------------------------- #
    seg_pr = ex.get("segments_pr")
    seg_lr = ex.get("segments_lr")
    if seg_pr or seg_lr:
        add("## 11. Top segment movers")
        add("")
        if seg_pr:
            add("**By physical reads**")
            rows = [[d["name"], d["type"] or "", _pct(d["p1"]), _pct(d["p2"]),
                     _pct(d["diff"]), disp(d["abs1"]), disp(d["abs2"])] for d in seg_pr]
            add(_md_table(["Object", "Type", "1st %", "2nd %", "Δ", "1st reads", "2nd reads"], rows))
            add("")
        if seg_lr:
            add("**By logical reads**")
            rows = [[d["name"], d["type"] or "", _pct(d["p1"]), _pct(d["p2"]),
                     _pct(d["diff"]), disp(d["abs1"]), disp(d["abs2"])] for d in seg_lr]
            add(_md_table(["Object", "Type", "1st %", "2nd %", "Δ", "1st reads", "2nd reads"], rows))
            add("")

    # ---- 12. configuration ------------------------------------------------- #
    io = ex.get("init_ora")
    if io:
        add("## 12. Configuration changes (init.ora)")
        add("")
        add(f"Explicitly-set parameters: **{io['total']}**.  "
            f"Changed between the two end snapshots: "
            f"**{len(io['changed']) + io['hidden_changed']}** "
            f"({len(io['changed'])} visible, {io['hidden_changed']} hidden `_` parameters).")
        add("")
        if io["changed"]:
            rows = [[c["name"], c["old"], c["new"]] for c in io["changed"][:25]]
            add(_md_table(["Parameter", "1st end", "2nd end"], rows))
            if len(io["changed"]) > 25:
                add(f"\n… and {len(io['changed']) - 25} more visible parameters.")
            add("")

    # ---- 13. RAC ----------------------------------------------------------- #
    rac = ex.get("rac")
    if rac:
        add("## 13. RAC / cluster")
        add("")
        if rac.get("instances"):
            rows = [[k, disp(v["begin"]), disp(v["end"])] for k, v in rac["instances"].items()]
            add(_md_table(["Instances", "Begin", "End"], rows))
            add("")
        if rac.get("buffer_access"):
            rows = [[d["label"], disp(d["first"]), disp(d["second"]), disp(d["diff"])]
                    for d in rac["buffer_access"]]
            add("**Buffer access efficiency**")
            add(_md_table(["Access", "1st %", "2nd %", "Δ"], rows))
            add("")
        if rac.get("cache_load"):
            rows = [[d["label"], disp(d["psec1"]), disp(d["psec2"]), disp(d["pct"])]
                    for d in rac["cache_load"]]
            add("**Global cache load (per sec)**")
            add(_md_table(["Metric", "1st", "2nd", "%Diff"], rows))
            add("")

    # ---- 14. key takeaways ------------------------------------------------- #
    tk = takeaways(rpt, ex)
    if tk:
        add("## 14. Key takeaways")
        add("")
        for t in tk:
            add(f"- {t}")
        add("")

    add("---")
    add(f"*Top-N per section: {top}. Sections are omitted when not present in the source report. "
        f"Full detail remains in the original HTML.*")
    return "\n".join(L) + "\n"


def _sql_section(rpt, ex, which, n, add):
    rows = ex.get("sql_" + which.lower().replace(" ", "_").replace("/", ""))
    if not rows:
        add("_Not present in this report._")
        return
    md = []
    md.append(_md_table(
        ["SQL Id", "1st %DB", "2nd %DB", "Δ", "1st exec", "2nd exec", "SQL (truncated)"],
        [[f"`{d['id']}`", _pct(d["p1"]), _pct(d["p2"]), _pct(d["diff"]),
          disp(d["exec1"]), disp(d["exec2"]), "`" + truncate(d["text"] or "", 60) + "`"]
         for d in rows]))
    add("\n".join(md))


def _pct(v):
    c = clean(v)
    if c is None:
        return "-"
    f = num(v)
    if f is None:
        return c
    return f"{f:+.1f}%" if f != 0 else "0.0%"


def _pct_disp(v1, v2):
    a, b = num(v1), num(v2)
    if a is None or b is None or a == 0:
        return "-"
    return f"{(b - a) / a * 100:+.1f}%"


def takeaways(rpt, ex):
    tk = []
    dbi = ex.get("db_info") or {}
    a, b = dbi.get("1st", {}), dbi.get("2nd", {})

    r1, r2 = clean(a.get("Release")), clean(b.get("Release"))
    if r1 and r2 and r1 != r2:
        tk.append(f"**Release change:** {r1} → {r2}. Interpret all deltas with this in "
                  f"mind — wait events, I/O paths and optimiser behaviour differ across releases.")

    host = ex.get("host") or {}
    cpu1, cpu2 = num(host.get("Number of CPUs", {}).get("1st")), num(host.get("Number of CPUs", {}).get("2nd"))
    mem1, mem2 = num(host.get("Physical Memory", {}).get("1st")), num(host.get("Physical Memory", {}).get("2nd"))
    if cpu1 and cpu2 and cpu1 != cpu2:
        tk.append(f"**Hardware change:** CPUs {cpu1:g} → {cpu2:g}"
                  + (f", memory {mem1/1024:.0f} GB → {mem2/1024:.0f} GB" if mem1 and mem2 and mem1 != mem2 else ""))

    snap = ex.get("snapshots") or {}
    s1, s2 = snap.get("1st", {}), snap.get("2nd", {})
    dt1, dt2 = num(s1.get("db_time_min")), num(s2.get("db_time_min"))
    au1, au2 = num(s1.get("active_users")), num(s2.get("active_users"))
    if dt1 and dt2 and dt1 != 0:
        d = (dt2 - dt1) / dt1 * 100
        direction = "fell" if d < 0 else "rose"
        tk.append(f"**Total DB time** {direction} {abs(d):.0f}% "
                  f"({dt1:,.0f} → {dt2:,.0f} min over the window).")
    if au1 and au2:
        tk.append(f"**Concurrency:** avg active users {au1:g} → {au2:g}.")

    we = ex.get("wait_events") or []
    for d in we[:1]:
        p2 = num(d["p2"]) or 0
        p1 = num(d["p1"]) or 0
        if p2 >= 5 and p1 == 0:
            tk.append(f"**New dominant wait in 2nd period:** `{d['event']}` "
                      f"({d['class'] or 'n/a'}, {p2:.1f}% DB time) — absent in 1st period.")
        elif p1 >= 5 and p2 == 0:
            tk.append(f"**Wait eliminated in 2nd period:** `{d['event']}` "
                      f"({d['class'] or 'n/a'}, was {p1:.1f}% DB time).")

    sql = ex.get("sql_elapsed_time") or []
    for d in sql[:1]:
        p2 = num(d["p2"]) or 0
        p1 = num(d["p1"]) or 0
        if p2 >= 5 and p1 == 0:
            tk.append(f"**New top SQL in 2nd period:** `{d['id']}` "
                      f"({p2:.1f}% DB time; {truncate(d['text'] or '', 50)}).")
        elif p1 >= 5 and p2 == 0:
            tk.append(f"**Top SQL dropped out in 2nd period:** `{d['id']}` "
                      f"(was {p1:.1f}% DB time).")

    return tk


# --------------------------------------------------------------------------- #
# 7.  Collect + main                                                          #
# --------------------------------------------------------------------------- #

def build_extras(rpt, top):
    return {
        "db_info": e_db_info(rpt),
        "instances": e_instances(rpt),
        "snapshots": e_snapshots(rpt),
        "host": e_host(rpt),
        "cache": e_cache(rpt),
        "load": e_load(rpt),
        "top_events": e_top_events(rpt),
        "time_model": e_time_model(rpt, top),
        "wait_classes": e_wait_classes(rpt, top),
        "wait_events": e_wait_events(rpt, top),
        "wait_profile_2nd": e_wait_profile(rpt, 5, "2nd"),
        "services": e_services(rpt, top),
        "sql_elapsed_time": e_top_sql(rpt, "Top SQL Comparison by Elapsed Time", top),
        "sql_cpu_time": e_top_sql(rpt, "Top SQL Comparison by CPU Time", min(top, 5)),
        "sql_io_time": e_top_sql(rpt, "Top SQL Comparison by I/O Time", min(top, 5)),
        "segments_pr": e_top_segments(rpt, "Top Segments Comparison by Physical Reads", top),
        "segments_lr": e_top_segments(rpt, "Top Segments Comparison by Logical Reads", top),
        "init_ora": e_init_ora(rpt),
        "rac": e_rac(rpt),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Summarise an Oracle AWR Compare Period (Diff) HTML report as Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Path to the AWR diff HTML report")
    ap.add_argument("-o", "--output", help="Write Markdown to this file (default: stdout)")
    ap.add_argument("--top", type=int, default=8, help="Rows per top-N section (default 8)")
    ap.add_argument("--dump-json", metavar="PATH", help="Also dump the parsed model as JSON to PATH")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.input):
        ap.error(f"input file not found: {args.input}")

    rpt = Report(args.input)
    ex = build_extras(rpt, args.top)
    md = render(rpt, ex, args.top)

    if args.dump_json:
        with open(args.dump_json, "w", encoding="utf-8") as fh:
            json.dump(ex, fh, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(md)
        sys.stderr.write(f"Wrote {args.output} ({len(md)} chars)\n")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
