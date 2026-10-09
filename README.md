# AWR Diff Summary

A small, **dependency-free** Python tool that turns an Oracle **AWR Compare
Period** report (a.k.a. an *AWR Diff* report — two snapshot ranges compared
side by side) into a concise **executive summary in Markdown**.

It is built for the single-HTML reports that `awrrpt` / `awrdiff` produce when
you compare one snapshot range against another. Those reports are dense
(several thousand HTML tables/rows) and hard to scan. This tool pulls out the
decision-relevant parts and writes them as a clean, paste-ready Markdown
document.

```
awr_diff_summary.py          # the tool (stdlib only, Python 3.8+)
README.md                    # this file
awrdiff_*.html               # input report(s) — AWR Compare Period output
summary.md                   # example generated output (run the tool to create)
```

---

## 1. Requirements

* **Python 3.8 or newer.** No third-party packages — the standard library
  (`html`, `json`, `re`, `argparse`, `os`, `sys`) is all that is used.
* A single AWR Compare Period **HTML** file (the output of an `awrdiff` /
  `awrrpt` compare, *not* a regular single-range AWR report).

That's it. No network access, no database connection, no installation steps.

---

## 2. Quick start

```bash
# print the executive summary to stdout
python3 awr_diff_summary.py awrdiff_ORCLDB01_00001_1_00010.html

# write it to a file
python3 awr_diff_summary.py awrdiff_ORCLDB01_00001_1_00010.html -o summary.md

# more rows per section (default is 8)
python3 awr_diff_summary.py report.html --top 12

# also dump the fully-parsed model as JSON (useful for scripting / dashboards)
python3 awr_diff_summary.py report.html --dump-json model.json
```

Run from this directory. The input path can be absolute or relative.

### Command-line options

| Option | Default | Description |
| --- | --- | --- |
| `input` | *(required)* | Path to the AWR diff HTML report. |
| `-o`, `--output` | stdout | Write the Markdown to this file instead of stdout. |
| `--top N` | `8` | Number of rows to show per top-N section (wait events, SQL, segments, time model, …). |
| `--dump-json PATH` | off | Additionally serialise the parsed model as JSON to `PATH`. |

Exit code is `0` on success, non-zero if the input file is missing.

---

## 3. What goes into the summary

The output is a single Markdown document with numbered sections. **Any section
that is not present in the source report is omitted automatically** (e.g. a
non-RAC single-instance report has no RAC section).

1. **Environment & version** — DB name, instance, host, Oracle release, edition,
   DB role, cluster/CDB, plus the snapshot ranges, window times, elapsed time,
   total **DB time** and **avg active users** for each period. If the two
   periods are on **different Oracle releases**, a callout is added warning that
   the comparison spans a release change.
2. **Host / hardware** — CPUs, cores, sockets, physical memory, OS load and
   CPU-time breakdown (user/system/idle/IO wait) per period.
3. **Headline workload (per second)** — DB time, CPU, redo, logical/physical
   reads and writes, user calls, parses, hard parses, transactions — per second
   and per transaction.
4. **Memory / cache sizing (MB)** — SGA target, buffer cache, shared/large/Java
   pools, PGA, log buffer.
5. **Time model** — the biggest movers in **% of DB time** between periods.
6. **Wait classes** — per-class % of DB time and the change.
7. **Top wait-event movers** — the events whose % of DB time changed most, with
   class and 2nd-period average wait. *(Note: idle events can exceed 100% of
   DB time by design — that is a property of the AWR figures, not a bug.)*
8. **Top timed events (as reported)** — the top events exactly as the report
   lists them, for each period.
9. **Services** — per-service % of DB time and % CPU, with deltas.
10. **Top SQL** — by **elapsed time**, **CPU time** and **I/O time**: SQL id,
    % share per period, delta, executions, and a truncated SQL text.
11. **Top segment movers** — by **physical reads** and **logical reads**:
    `owner.object (subobject)`, type, % share and absolute read counts.
12. **Configuration changes (init.ora)** — how many explicitly-set parameters
    changed between the two end snapshots (visible vs hidden `_` parameters),
    with the visible ones listed (name → old → new).
13. **RAC / cluster** — number of instances, buffer-access efficiency and
    global-cache load (only present on RAC reports).
14. **Key takeaways** — a short, auto-generated bullet list of the most
    material findings (release change, hardware change, DB-time trend,
    concurrency, new/dropped top waits and top SQL).

All numbers are taken **verbatim** from the report; no re-basing or
normalisation is applied. `%Diff`/`Δ` means the change from the 1st to the 2nd
period.

---

## 4. Example output (abridged)

```markdown
# AWR Diff Executive Summary — ORCLDB01

> Compare-period report: 1st snapshot range vs 2nd snapshot range.

## 1. Environment & version
| | 1st period | 2nd period |
| --- | --- | --- |
| DB name | ORCLDB01 | ORCLDB01 |
| Host | dbhost01.example.com | dbhost02.example.com |
| Oracle release | 11.2.0.4.0 | 19.26.0.0.0 |
| Cluster / CDB | YES / NO | YES / NO |

**Version change detected:** 11.2.0.4.0 → 19.26.0.0.0 …

## 2. Host / hardware
| Metric | 1st | 2nd | %Diff |
| --- | --- | --- | --- |
| Number of CPUs | 48 | 128 | +166.7% |
| Physical Memory | 385192.2M | 1038889.7M | +169.7% |
| %Idle Time | 70.64 | 97.25 | +37.7% |

## 14. Key takeaways
- **Release change:** 11.2.0.4.0 → 19.26.0.0.0. Interpret all deltas with this in mind …
- **Hardware change:** CPUs 48 → 128, memory 376 GB → 1015 GB
- **Total DB time** fell 81% (18,000 → 3,340 min over the window).
- **Concurrency:** avg active users 13 → 2.4.
- **Top SQL dropped out in 2nd period:** `22zzbn4msv1g0` (was 32.4% DB time).
```

(See the full `summary.md` produced by the quick-start command.)

---

## 5. How it works

The tool is deliberately self-contained:

1. **Parse** — a minimal DOM built with the standard-library `HTMLParser`
   (no `lxml`/`BeautifulSoup`). It tolerates the malformed/unclosed HTML that
   AWR emits by using a lenient tag stack.
2. **Model tables** — every `<table>` is captured with its two header rows
   (group + leaf) and its data rows, giving a `Table` that can look a cell up
   by *(group, leaf)* or by leaf index.
3. **Locate sections** — each report section is found by its heading title
   (e.g. *Top SQL Comparison by Elapsed Time*, *Time Model Statistics*). A few
   "Report Summary" tables that share the same heading are instead located by a
   stable data-row label (e.g. *Number of CPUs*).
4. **Extract** — one small function per section returns a plain dict/list of
   the values that matter.
5. **Render** — the pieces are turned into Markdown tables and a takeaways
   list. Missing sections are simply skipped.

Because step 3–4 key off headings and column groups rather than hard-coded
positions, the tool copes with small layout differences between AWR versions.
If Oracle changes the report structure enough to break a section, only that
section's extractor needs updating (the rest of the document still renders).

---

## 6. Limitations & notes

* **Compare-period reports only.** A plain single-range AWR report has no
  "1st vs 2nd" columns and is not the intended input.
* **Verbatim figures.** The tool reports what the HTML says. It does not
  validate or recompute totals, and it does not fetch anything from the DB.
* **Top-N.** Each "movers" section is ranked by the magnitude of the change and
  truncated to `--top` rows (the SQL CPU/I/O tables are capped a little lower
  to keep the document readable).
* **Large files.** A full AWR diff HTML is ~3.5 MB; the tool parses it in
  seconds. The generated summary is a few KB of Markdown.
* **Encoding.** Files are read as UTF-8 with replacement on bad bytes, so an
  odd non-UTF-8 report won't crash the run.

---

## 7. Extending

* **Add a section:** write an `e_your_section(rpt)` extractor (return `dict`/
  `list`/`None`), register it in `build_extras(...)`, and add a rendering
  block in `render(...)`. Return `None` to make it auto-omit.
* **Add a takeaway:** append a bullet in `takeaways(rpt, ex)`.
* **Emit a different format:** the parsed model (see `--dump-json`) is a plain
  JSON-serialisable structure, so you can target HTML/CSV/PDF from it without
  touching the parsers.

---

## 8. License

Provided as-is for internal use. No warranty.
