#!/usr/bin/env python3
"""
awr_rac_balance.py
==================

Compare the two (or more) instances of each RAC database from per-instance
AWR reports converted to Markdown by ``awr_to_markdown.py``, quantify the
load imbalance, and locate its cause (services / SQL that run on only one
node).

Usage
-----
    ./awr_rac_balance.py [report_dir]        # default: ./reports/HA
    ./awr_rac_balance.py reports/HA --json model.json
    ./awr_rac_balance.py reports/HA --db HFPCDB --db MESDB06
    ./awr_rac_balance.py reports/HA --threshold 1.1 --top 15

Output sections
---------------
 1. Balance table : AAS, DB Time split and high/low ratio per cluster
                    (sorted by ratio), plus per-instance key rates
                    (CPU/s, execs/s, redo, GC blocks, logons, sessions).
 2. Detail        : for every cluster whose ratio >= --threshold
                    (default 1.25): per-service DB Time on each node and
                    the SQL with the largest absolute Elapsed-Time delta
                    between nodes ("why" evidence).

Duplicate files covering the same (db, instance) - e.g. the ``dr``/``st``
copies of one report - are parsed once and counted.  ASH reports (no
instance AWR header) are skipped with a warning.

Pure stdlib, Python 3.8+.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys


# ------------------------------------------------------------------ parsing --
def num(s):
    if s is None:
        return None
    s = str(s).replace(',', '').replace('K', '')
    s = re.sub(r'\(mins\)|\(sec\)|bytes|blocks|MB', '', s)
    s = re.sub(r'[（(].*?[)）]', '', s)
    try:
        return float(s.strip())
    except ValueError:
        return None


def parse_tables(lines):
    tables, cur = [], None
    for ln in lines:
        if ln.startswith('|'):
            cells = [c.strip() for c in ln.strip('|').split('|')]
            if all(re.fullmatch(r'-{3,}|:?-+:?|col\d*|col \(?\d*\)?', c or '-')
                   for c in cells):
                continue
            if cur is None:
                cur = []
            cur.append(cells)
        else:
            if cur:
                tables.append(cur)
                cur = None
    if cur:
        tables.append(cur)
    return tables


def blocks(lines):
    """[(name, body)] split on ##..##### headings and **bold** sub-headings."""
    out, cur, name = [], [], '(top)'
    for ln in lines:
        m = re.match(r'^#{2,5} (.*)$', ln)
        b = re.fullmatch(r'\*\*(.+?)\*\*\s*', ln)
        if m or b:
            if cur:
                out.append((name, cur))
            name = (m.group(1) if m else b.group(1)).strip()
            cur = []
        else:
            cur.append(ln)
    if cur:
        out.append((name, cur))
    return out


def parse_report(path):
    lines = open(path, encoding='utf-8').read().splitlines()
    head = '\n'.join(lines[:10])
    if re.search(r'^# AWR Report', head, re.M) is None:
        return None                                  # not an instance AWR
    rec = {}
    m = re.search(r'DB: (\w+), Inst: (\w+), Snaps: (\d+)-(\d+)', head)
    if m:
        rec['db'], rec['inst'], rec['snaps'] = m.group(1), m.group(2), \
            f"{m.group(3)}-{m.group(4)}"
    for t in parse_tables(lines[:45]):
        j = ' | '.join(t[0])
        if 'DB Name' in j:
            rec['db_id'], rec['unique_name'] = t[1][1], t[1][2]
            rec['role'], rec['release'], rec['rac'] = t[1][3], t[1][5], t[1][6]
        elif 'Inst Num' in j:
            rec['inst_num'] = t[1][1]
        elif 'Host Name' in j:
            rec['host'], rec['cpus'] = t[1][0], num(t[1][2])
        elif 'Snap Id' in j:
            for r in t[1:]:
                if r[0].startswith('Begin Snap'):
                    rec['begin'], rec['begin_sessions'] = r[2], num(r[3])
                elif r[0].startswith('End Snap'):
                    rec['end'], rec['end_sessions'] = r[2], num(r[3])
                elif r[0].startswith('Elapsed'):
                    rec['elapsed_min'] = num(r[2])
                elif r[0].startswith('DB Time'):
                    rec['dbtime_min'] = num(r[2])
    bl = blocks(lines)

    def grab(name, exact=False, maxrows=40):
        for bn, body in bl:
            if (bn == name) if exact else (name.lower() in bn.lower()):
                for t in parse_tables(body):
                    if len(t) > 1 and any(c.strip() for c in t[0]):
                        return [t[0]] + t[1:maxrows + 1]
        return []

    lp = grab('Load Profile', exact=True)
    rec['load_profile'] = {r[0].rstrip(':'): [num(x) for x in r[1:]]
                           for r in lp[1:]} if lp else {}
    t = grab('Top 10 Foreground Events')
    rec['top10_fg'] = [dict(zip(t[0], r)) for r in t[1:]] if t else []
    t = grab('Wait Classes by Total Wait Time')
    rec['wait_classes'] = [dict(zip(t[0], r)) for r in t[1:]] if t else []
    t = grab('Service Statistics', exact=True)
    rec['service_stats'] = t[1:] if t else []
    t = grab('SQL ordered by Elapsed Time', exact=True)
    rec['top_sql'] = [dict(zip(t[0], r)) for r in t[1:21]] if t else []
    for bn, body in bl:
        if bn == 'SQL ordered by Elapsed Time':
            m = re.search(r'DB Time \(s\): ([\d,]+)', '\n'.join(body))
            if m:
                rec['dbtime_s'] = float(m.group(1).replace(',', ''))
    return rec


def lp(r, name):
    v = r.get('load_profile', {}).get(name)
    return v[0] if isinstance(v, list) and v else None


# --------------------------------------------------------------- reporting --
def F(x, fmt='{:,.1f}'):
    return fmt.format(x) if isinstance(x, (int, float)) else 'n/a'


def cluster_table(by_db):
    print('=' * 108)
    print('LOAD BALANCE  (AAS = DB Time / elapsed; split = each node\'s share of cluster DB Time)')
    print('=' * 108)
    print(f"{'DB':<10}{'ratio':>7} {'split':>8} {'inst':<12}{'host':<16}"
          f"{'AAS':>6}{'CPU/s':>7}{'exec/s':>8}{'redo KB/s':>11}{'txn/s':>8}"
          f"{'GCrecv/s':>9}{'logons/s':>9}{'sess':>6}")
    ratios = {}
    for db, insts in by_db.items():
        a = [lp(r, 'DB Time(s)') or 0 for _, r in insts]
        ratios[db] = max(a) / min(a) if min(a) > 0 else float('inf')
    for db in sorted(by_db, key=lambda d: -ratios[d]):
        insts = sorted(by_db[db])
        tot = sum(lp(r, 'DB Time(s)') or 0 for _, r in insts) or 1
        for i, (n, r) in enumerate(insts):
            flag = ''
            if i == 0 and ratios[db] >= 1.25:
                flag = '  <<<' if ratios[db] < 1.5 else '  <<<<<'
            print(f"{db if i == 0 else '':<10}"
                  f"{(F(ratios[db], '{:.2f}x') if i == 0 else ''):>7}"
                  f"{('' if i else '%d/%d' % tuple(round(100 * (lp(x[1], 'DB Time(s)') or 0) / tot) for x in insts)) if len(insts) == 2 else '':>8}"
                  f" {r.get('inst', ''):<12}{r.get('host', ''):<16}"
                  f"{F(lp(r, 'DB Time(s)')):>6}{F(lp(r, 'DB CPU(s)')):>7}"
                  f"{F(lp(r, 'Executes (SQL)')):>8}"
                  f"{F((lp(r, 'Redo size (bytes)') or 0) / 1024):>11}"
                  f"{F(lp(r, 'Transactions')):>8}"
                  f"{F(lp(r, 'Global Cache blocks received')):>9}"
                  f"{F(lp(r, 'Logons')):>9}"
                  f"{F(r.get('begin_sessions'), '{:,.0f}'):>6}{flag}")


def detail(by_db, db, recs_by_num, top):
    insts = sorted(by_db[db])
    if len(insts) != 2:
        return
    (n1, r1), (n2, r2) = insts[0], insts[1]
    d1, d2 = r1.get('dbtime_s', 0), r2.get('dbtime_s', 0)
    print('-' * 108)
    print(f"{db}: node1 {d1/3600:.1f}h vs node2 {d2/3600:.1f}h DB Time"
          f"   (delta {abs(d1-d2)/3600:.1f}h)")

    def svc(r):
        return {row[0]: num(row[1]) or 0 for row in r.get('service_stats', [])}
    s1, s2 = svc(r1), svc(r2)
    for name in sorted(set(s1) | set(s2)):
        m1, m2 = s1.get(name, 0) / 60, s2.get(name, 0) / 60
        if m1 + m2 < 5:
            continue
        verdict = 'even'
        if m1 > m2 * 1.3:
            verdict = f'-> node1 x{m1/max(m2,0.1):.1f}'
        elif m2 > m1 * 1.3:
            verdict = f'-> node2 x{m2/max(m1,0.1):.1f}'
        print(f"   service {name:<18} n1={m1:>8,.0f} min  n2={m2:>8,.0f} min   {verdict}")

    sqls = {}
    for r, n in ((r1, n1), (r2, n2)):
        for s in r.get('top_sql', []):
            sid = s.get('SQL Id', '?')
            rec = sqls.setdefault(sid, {'n1': 0, 'n2': 0,
                                        'mod': s.get('SQL Module', '') or '',
                                        'txt': re.sub(r'\s+', ' ', s.get('SQL Text', '') or '')[:48]})
            rec['n' + str(n)] = max(rec['n' + str(n)], num(s.get('Elapsed Time (s)')) or 0)
    rows = sorted(((abs(v['n1'] - v['n2']), k, v) for k, v in sqls.items()), reverse=True)
    printed = 0
    for delta, sid, v in rows:
        if delta < 3600 or printed >= top:
            continue
        node = 'node1' if v['n1'] > v['n2'] else 'node2'
        print(f"   {sid:<15} {v['mod'][:22]:<22} {v['txt']:<48} "
              f"{v['n1']/3600:>6.1f}h vs {v['n2']/3600:>6.1f}h  [{node}-only/heavier]")
        printed += 1
    if not printed:
        print('   (no top SQL with >=1h absolute node differential)')


def main():
    ap = argparse.ArgumentParser(
        description='RAC instance load-balance analysis over per-instance AWR markdown reports.')
    ap.add_argument('folder', nargs='?', default='reports/HA',
                    help='folder of awr_to_markdown.py output (*.md), default ./reports/HA')
    ap.add_argument('--json', metavar='OUT', help='also dump the parsed per-instance model')
    ap.add_argument('--db', action='append', metavar='NAME',
                    help='restrict detail section to this DB (repeatable)')
    ap.add_argument('--threshold', type=float, default=1.25,
                    help='ratio at/above which a cluster gets the detail section (default 1.25)')
    ap.add_argument('--top', type=int, default=10, help='max SQL rows per cluster in detail (default 10)')
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.folder, '*.md')))
    if not files:
        sys.exit(f'no .md files under {args.folder}')
    by_key, dups, skipped = {}, 0, []
    for path in files:
        rec = parse_report(path)
        if rec is None:
            skipped.append(path)
            continue
        key = (rec.get('db'), rec.get('inst_num'))
        if key in by_key:
            dups += 1                             # duplicate copy of same report
            continue
        by_key[key] = rec

    by_db = {}
    for (db, n), r in by_key.items():
        by_db.setdefault(db, []).append((str(n), r))
    for db in by_db:
        by_db[db].sort()

    print(f'parsed {len(by_key)} unique instance reports '
          f'({dups} duplicate copies skipped'
          + (f'; non-AWR files: {", ".join(os.path.basename(p) for p in skipped)}' if skipped else '')
          + ')')
    cluster_table(by_db)

    skew = [db for db in sorted(by_db)
            if len(by_db[db]) == 2
            and (lambda a: (max(a) / min(a) if min(a) > 0 else 99) >= args.threshold)(
                [lp(r, 'DB Time(s)') or 0 for _, r in by_db[db]])]
    if args.db:
        skew = [d for d in by_db if d in {x.upper() for x in args.db}]
    print()
    print('=' * 108)
    print(f'CAUSE DETAIL (clusters with ratio >= {args.threshold}: {", ".join(skew) or "none"})')
    print('=' * 108)
    for db in skew:
        detail(by_db, db, None, args.top)

    if args.json:
        json.dump({f"{k[0]}/{k[1]}": v for k, v in by_key.items()},
                  open(args.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f'\nmodel written to {args.json}')


if __name__ == '__main__':
    main()
