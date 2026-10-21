#!/usr/bin/env python3
"""
awr_to_markdown.py
==================

Convert a standard Oracle AWR (Workload Repository) **HTML report** — the
single-period kind produced by `awrrpt`, including RAC/global reports — into
**Markdown**, section by section, table by table.

Unlike `awr_diff_summary.py` (which distils a Compare-Period report into an
executive summary), this tool is a *faithful format conversion*: every section,
note, table and ADDM/SQL-text block in the report is emitted, so that the
resulting Markdown can be analysed by a human or by an LLM skill.

Pure stdlib: Python 3.8+, no third-party packages.

Usage
-----
    python3 awr_to_markdown.py report.html                  # stdout
    python3 awr_to_markdown.py report.html -o report.md     # file
    python3 awr_to_markdown.py report.html --dump-json m.json
    python3 awr_to_markdown.py report.html --sql-text-chars 60

Design notes
------------
* Headings map h1->#, h2->##, h3->### in document order (RAC reports nest
  their "Global"/"By Instance" variants as h3).
* Two-row table headers (group row with colspans + leaf row) are flattened
  into single header cells: "Total Time (s)" under group "Wait Time" becomes
  "Wait Time Total(s)".
* The "Complete List of SQL Text" table is rendered as per-SQLId fenced code
  blocks (its cells contain newlines); SQL text in other tables is truncated
  to --sql-text-chars.
* Table-of-contents link lists are skipped; descriptive bullet-note lists
  under a heading are kept as Markdown bullets.
* ADDM task sections are <pre> text dumps and pass through verbatim in code
  fences (they may be in the report's NLS language).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from awr_diff_summary import Node, build_dom, clean  # noqa: E402  (reuse lenient DOM)


# --------------------------------------------------------------------------- #
#  Raw text helpers                                                           #
# --------------------------------------------------------------------------- #

def node_raw(node):
    """Descendant text with newlines preserved (for <pre>/SQL text)."""
    parts = []
    for c in node.children:
        if isinstance(c, str):
            parts.append(c)
        else:
            parts.append(node_raw(c))
    s = "".join(parts).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", s).strip()


def node_text_flat(node):
    """Descendant text, whitespace-collapsed (safe inside a table cell)."""
    s = node_raw(node)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
#  Table -> Markdown                                                          #
# --------------------------------------------------------------------------- #

def table_rows(node):
    """List of rows; each row is a list of (is_th, text, colspan) expanded so
    that column positions align across rows."""
    rows = []
    for tr in node.children:
        if not (isinstance(tr, Node) and tr.tag == "tr"):
            continue
        cells = []
        for c in tr.children:
            if isinstance(c, Node) and c.tag in ("th", "td"):
                try:
                    cs = max(1, int(c.attrs.get("colspan", "1") or 1))
                except ValueError:
                    cs = 1
                txt = node_text_flat(c)
                raw = node_raw(c)
                for k in range(cs):
                    # forward-fill text across colspan in header cells so
                    # group labels cover every column they span
                    fill = txt if (k == 0 or c.tag == "th") else ""
                    cells.append((c.tag == "th", fill, 1, raw if k == 0 else ""))
        if cells:
            rows.append(cells)
    return rows


def flatten_headers(header_rows):
    """[(group, leaf)] -> display names."""
    if not header_rows:
        return None, 0
    n = max(len(r) for r in header_rows)
    def padded(row):
        return row + [(False, "", 1, "")] * (n - len(row))
    if len(header_rows) == 1:
        cols = [(None, clean(t) or "") for (_th, t, _c, _r) in padded(header_rows[0])]
    else:
        top, bot = padded(header_rows[0]), padded(header_rows[1])
        # a second header row exists only if it adds leaf detail; if the top
        # row already looks fully-populated use it alone
        n_th_top = sum(1 for (th, t, _c, _r) in top if th and t)
        n_th_bot = sum(1 for (th, t, _c, _r) in bot if th and t)
        if n_th_bot == 0:
            top, bot = bot, top
        cols = []
        for (gt, gx, _gc, _gr), (lt, lx, _lc, _lr) in zip(top, bot):
            g, l = clean(gx) or "", clean(lx) or ""
            if g and l:
                cols.append((g, f"{g} {l}" if l not in g else l))
            else:
                cols.append((None, g or l))
    # deduplicate repeated names with suffixes so JSON keys stay unique
    seen, names = {}, []
    for _g, name in cols:
        name = name or "col"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        names.append(name)
    return names, n


def is_sql_text_table(names, rows_data):
    # exactly the "SQL Id | SQL Text" appendix table (ASH tables share those
    # column names but carry many more metric columns)
    return bool(names) and len(names) == 2 \
        and re.sub(r"\s", "", names[0]).lower() == "sqlid" \
        and re.sub(r"\s", "", names[1]).lower() == "sqltext" \
        and bool(rows_data) \
        and max(len(r) for r in rows_data) <= 3


def render_table(node, opts):
    """Return (markdown_lines, structured_dict)."""
    all_rows = table_rows(node)
    header_rows = []
    while len(header_rows) < 2 and len(all_rows) > len(header_rows):
        r = all_rows[len(header_rows)]
        n_th = sum(1 for (th, t, _c, _r) in r if th)
        if n_th * 2 >= len(r) and any(t for (th, t, _c, _r) in r if th):
            header_rows.append(r)
        else:
            break
    data_rows = all_rows[len(header_rows):]
    names, ncols = flatten_headers(header_rows)
    summary = node.attrs.get("summary", "") or ""

    # -- special case: Complete List of SQL Text -> fenced blocks ---------- #
    if names and is_sql_text_table(names, data_rows):
        lines = []
        entries = []
        col_text = names.index("SQL Text") if "SQL Text" in names else 1
        for row in data_rows:
            sid = clean(row[0][1]) if row else None
            raw = row[col_text][3] if len(row) > col_text else ""
            if not sid or not raw:
                continue
            lines.append(f"**SQL Id `{sid}`**")
            lines.append("")
            lines.append("```sql")
            lines.append(raw)
            lines.append("```")
            lines.append("")
            entries.append({"sql_id": sid, "text": raw})
        return lines, {"type": "sql_text", "summary": summary, "entries": entries}

    if not names:
        names = [f"col{i+1}" for i in range(max((len(r) for r in all_rows), default=0))]
        ncols = len(names)

    def cell_text(t):
        c = clean(t)
        if c is None:
            return ""
        if len(c) > opts.max_cell:
            c = c[: opts.max_cell - 1] + "\u2026"
        return c.replace("|", "\\|")

    lines = ["| " + " | ".join(names) + " |",
             "|" + "|".join([" --- "] * len(names)) + "|"]
    model_rows = []
    sql_text_cols = {j for j, nm in enumerate(names) if nm == "SQL Text"}
    for row in data_rows:
        vals = [cell_text(t) for (_th, t, _c, _r) in row]
        if len(vals) < ncols:
            vals += [""] * (ncols - len(vals))
        vals = vals[:ncols]
        for j in sql_text_cols:
            if len(vals[j]) > opts.sql_text_chars and opts.sql_text_chars:
                vals[j] = vals[j][: opts.sql_text_chars - 1] + "\u2026"
        if not any(vals):
            continue
        lines.append("| " + " | ".join(vals) + " |")
        model_rows.append(dict(zip(names, vals)))
    lines.append("")
    return lines, {"type": "table", "summary": summary,
                   "columns": names, "rows": model_rows}


# --------------------------------------------------------------------------- #
#  Document walk                                                              #
# --------------------------------------------------------------------------- #

class Opts:
    def __init__(self, sql_text_chars, max_cell):
        self.sql_text_chars = sql_text_chars
        self.max_cell = max_cell


def list_is_toc(ul):
    """True if every <li> contains nothing but links (a section index)."""
    lis = [c for c in ul.children if isinstance(c, Node) and c.tag == "li"]
    if not lis:
        return True
    for li in lis:
        meaningful = [c for c in li.children
                      if (isinstance(c, str) and c.strip())
                      or (isinstance(c, Node) and c.tag not in ("a", "br", "img"))]
        if meaningful:
            return False
    return True


def render_list(ul, lines, model):
    if list_is_toc(ul):
        return
    for li in ul.children:
        if isinstance(li, Node) and li.tag == "li":
            t = node_text_flat(li)
            if t and t != "Back to Top":
                lines.append(f"- {t}")
                model.setdefault("notes", []).append(t)
    lines.append("")


def walk(node, lines, model, opts):
    for c in node.children:
        if isinstance(c, str):
            t = re.sub(r"\s+", " ", c).strip()
            if t:
                lines.append(t)
                lines.append("")
                model["sections"].setdefault(cur_heading[0], {"blocks": []})["blocks"] \
                    .append({"type": "text", "text": t})
            continue
        if not isinstance(c, Node):
            continue
        tag = c.tag
        if tag in ("h1", "h2", "h3", "h4"):
            txt = node_text_flat(c)
            level = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}[tag]
            lines.append("")
            lines.append(f"{level} {txt}")
            lines.append("")
            cur_heading[0] = (" / ".join(path_for(tag, txt)))
            push_section(model, cur_heading[0])
        elif tag == "table":
            tbl_lines, tbl_model = render_table(c, opts)
            push_block(model, tbl_model)
            lines.extend(tbl_lines)
        elif tag == "ul":
            push_block_model = push_section(model, cur_heading[0])
            render_list(c, lines, push_block_model)
        elif tag == "pre":
            txt = node_raw(c)
            if txt:
                fence = "```"
                if "```" in txt:
                    fence = "````"
                lines.extend([fence, txt, fence, ""])
                push_block(model, {"type": "pre", "text": txt})
        elif tag in ("p", "blockquote", "div", "font", "center"):
            has_block_child = any(isinstance(x, Node) and x.tag in
                                  ("table", "ul", "h1", "h2", "h3", "h4", "pre", "div")
                                  for x in c.children)
            if has_block_child:
                walk(c, lines, model, opts)
            else:
                t = node_text_flat(c)
                if t and t != "Back to Top":
                    lines.append(f"> {t}")
                    lines.append("")
                    push_block(model, {"type": "note", "text": t})
        elif tag in ("br", "img", "a", "span"):
            t = node_text_flat(c)
            if t and not t.startswith("Back to") and tag != "img":
                lines.append(t)
                lines.append("")
        # anything else: descend
        elif c.children:
            walk(c, lines, model, opts)


# -- small path state helpers (module-level, reset per document) ----------- #
cur_heading = [""]
_hstack = {}


def path_for(tag, txt):
    lvl = int(tag[1])
    _hstack[lvl] = txt
    for k in list(_hstack):
        if k > lvl:
            del _hstack[k]
    return [_hstack[k] for k in sorted(_hstack)]


def push_section(model, key):
    return model["sections"].setdefault(key, {"blocks": []})


def push_block(model, blk):
    push_section(model, cur_heading[0])["blocks"].append(blk)


# --------------------------------------------------------------------------- #
#  Top-level report info                                                      #
# --------------------------------------------------------------------------- #

def report_meta(raw_html, path):
    m = re.search(r"<title>(.*?)</title>", raw_html, re.S | re.I)
    title = clean(m.group(1)) if m else os.path.basename(path)
    return title


def bold_captions(lines):
    """A plain text line immediately followed by a table -> make it bold so it
    reads as a caption (e.g. 'Database Summary')."""
    out = list(lines)
    for i in range(len(out) - 3):
        ln = out[i]
        if not ln or ln[0] in "|#>`-*< ":
            continue
        if out[i + 1] == "" and out[i + 2].startswith("| ") \
                and out[i + 3].startswith("| ---"):
            out[i] = f"**{ln}**"
    return out


def convert(path, opts):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    dom = build_dom(raw)
    title = report_meta(raw, path)

    lines = []
    model = {"title": title, "source": os.path.basename(path), "sections": {}}
    # locate <body> (tolerating partial nesting)
    bodies = [n for n in _all_nodes(dom) if n.tag == "body"]
    root = bodies[0] if bodies else dom
    lines.append(f"<!-- Converted from `{os.path.basename(path)}` by "
                 f"awr_to_markdown.py -->")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    cur_heading[0] = "Preamble"
    _hstack.clear()
    walk(root, lines, model, opts)
    text = "\n".join(bold_captions(lines)).rstrip() + "\n"
    return text, model


def _all_nodes(node):
    yield node
    for c in node.children:
        if isinstance(c, Node):
            yield from _all_nodes(c)


# --------------------------------------------------------------------------- #
#  main                                                                       #
# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert an Oracle AWR HTML report to Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Path to the AWR HTML report")
    ap.add_argument("-o", "--output", help="Write Markdown to this file (default: stdout)")
    ap.add_argument("--sql-text-chars", type=int, default=100,
                    help="Truncate SQL text inside tables to N chars (0 = keep full; "
                         "default 100). The Complete List of SQL Text section is always full.")
    ap.add_argument("--max-cell", type=int, default=200,
                    help="Hard cap for any single table cell (default 200)")
    ap.add_argument("--dump-json", metavar="PATH",
                    help="Also dump the structured model as JSON to PATH")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.input):
        ap.error(f"input file not found: {args.input}")

    opts = Opts(args.sql_text_chars, args.max_cell)
    md, model = convert(args.input, opts)

    if args.dump_json:
        with open(args.dump_json, "w", encoding="utf-8") as fh:
            json.dump(model, fh, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(md)
        sys.stderr.write(f"Wrote {args.output} ({len(md)} chars, "
                         f"{len(model['sections'])} sections)\n")
    else:
        sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
