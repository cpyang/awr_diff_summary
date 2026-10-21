#!/usr/bin/env bash
#
# awr_all.sh
# ==========
# Run awr_to_markdown.py against every regular (single-range) AWR HTML report
# in a folder and write a Markdown conversion next to each input, using the
# same base name with a .md extension:
#
#     EAP-HCMSPN02 主库 10月20 18点至10月21 16点-awr.html  ->  ....md
#
# Compare-Period (diff) reports are skipped — those belong to awr_diff_all.sh.
# Detection: file name matching awrdiff*.html, or a "Compare Period" marker in
# the first 16 KB of the HTML.
#
# Usage:
#     ./awr_all.sh [DIR] [--json] [--sql-text N] [--force]
#
#   DIR            Folder to scan for *.html (default: directory containing
#                  this script).
#   --json         Also write a .json structured model next to each .md.
#   --sql-text N   SQL text truncation inside tables; forwarded to the Python
#                  tool (default: tool default 100).
#   --force        Overwrite existing .md files (default: skip if present).
#
# Exit status: 0 if every file succeeded (or was skipped), 1 if any failed.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$SCRIPT_DIR/awr_to_markdown.py"

TARGET_DIR="$SCRIPT_DIR"
FORCE=0
DUMP_JSON=0
SQL_TEXT=""

usage() {
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --json)     DUMP_JSON=1; shift ;;
        --force)    FORCE=1; shift ;;
        --sql-text) SQL_TEXT="${2:---sql-text requires a value}"; shift 2 ;;
        -h|--help)  usage; exit 0 ;;
        -*)         echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
        *)          TARGET_DIR="$1"; shift ;;
    esac
done

if [[ ! -f "$TOOL" ]]; then
    echo "error: tool not found: $TOOL" >&2
    exit 1
fi
if [[ ! -d "$TARGET_DIR" ]]; then
    echo "error: directory not found: $TARGET_DIR" >&2
    exit 1
fi

is_diff_report() {
    local f="$1" name
    name="$(basename "$f")"
    [[ "$name" =~ ^[Aa][Ww][Rr]diff ]] && return 0
    head -c 16384 "$f" 2>/dev/null | grep -qi "compare[ _-]*period" && return 0
    return 1
}

shopt -s nullglob
inputs=("$TARGET_DIR"/*.html)
shopt -u nullglob

if [[ ${#inputs[@]} -eq 0 ]]; then
    echo "No *.html files found in $TARGET_DIR — nothing to do."
    exit 0
fi

echo "Scanning ${#inputs[@]} HTML file(s) in $TARGET_DIR"
echo

ok=0
skip=0
fail=0
diffs=0
failed=()

for f in "${inputs[@]}"; do
    base="${f%.html}"
    out="${base}.md"
    if is_diff_report "$f"; then
        echo "diff   $(basename "$f")  (Compare-Period report — use awr_diff_all.sh)"
        diffs=$((diffs + 1))
        continue
    fi
    if [[ -f "$out" && $FORCE -eq 0 ]]; then
        echo "skip   $(basename "$out")  (exists; use --force to overwrite)"
        skip=$((skip + 1))
        continue
    fi
    args=("$f" -o "$out")
    [[ -n "$SQL_TEXT" ]] && args+=(--sql-text-chars "$SQL_TEXT")
    if [[ $DUMP_JSON -eq 1 ]]; then
        args+=(--dump-json "$base.json")
    fi
    if python3 "$TOOL" "${args[@]}" 2>/dev/null; then
        if [[ $DUMP_JSON -eq 1 ]]; then
            echo "wrote  $(basename "$out") + $(basename "$base".json)"
        else
            echo "wrote  $(basename "$out")"
        fi
        ok=$((ok + 1))
    else
        echo "FAIL   $(basename "$f")" >&2
        fail=$((fail + 1))
        failed+=("$f")
    fi
done

echo
echo "Done: $ok written, $skip skipped, $diffs diff-reports ignored, $fail failed."
if [[ $fail -gt 0 ]]; then
    printf '  failed: %s\n' "${failed[@]}"
    exit 1
fi
