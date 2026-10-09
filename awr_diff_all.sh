#!/usr/bin/env bash
#
# awr_diff_all.sh
# ===============
# Run awr_diff_summary.py against every AWR diff HTML report in a folder and
# write a Markdown executive summary next to each input, using the same base
# name with a .md extension:
#
#     awrdiff_IMSDB01_74705_1_74849.html  ->  awrdiff_IMSDB01_74705_1_74849.md
#
# Usage:
#     ./awr_diff_all.sh [DIR] [--top N] [--force]
#
#   DIR        Folder to scan for *.html (default: directory containing
#              this script).
#   --top N    Rows per top-N section (default: 8); forwarded to the Python
#              tool.
#   --force    Overwrite existing .md files (default: skip if present).
#
# Exit status: 0 if every file succeeded (or was skipped), 1 if any failed.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="$SCRIPT_DIR/awr_diff_summary.py"

TARGET_DIR="$SCRIPT_DIR"
TOP=8
FORCE=0

usage() {
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --top)   TOP="${2:---top requires a value}"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
        *)  TARGET_DIR="$1"; shift ;;
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

shopt -s nullglob
inputs=("$TARGET_DIR"/*.html)
shopt -u nullglob

if [[ ${#inputs[@]} -eq 0 ]]; then
    echo "No *.html files found in $TARGET_DIR — nothing to do."
    exit 0
fi

echo "Found ${#inputs[@]} HTML report(s) in $TARGET_DIR (top=$TOP)"
echo

ok=0
skip=0
fail=0
failed=()

for f in "${inputs[@]}"; do
    base="${f%.html}"
    out="${base}.md"
    if [[ -f "$out" && $FORCE -eq 0 ]]; then
        echo "skip   $(basename "$out")  (exists; use --force to overwrite)"
        skip=$((skip + 1))
        continue
    fi
    if python3 "$TOOL" "$f" --top "$TOP" -o "$out" 2>/dev/null; then
        echo "wrote  $(basename "$out")"
        ok=$((ok + 1))
    else
        echo "FAIL   $(basename "$f")" >&2
        fail=$((fail + 1))
        failed+=("$f")
    fi
done

echo
echo "Done: $ok written, $skip skipped, $fail failed."
if [[ $fail -gt 0 ]]; then
    printf '  failed: %s\n' "${failed[@]}"
    exit 1
fi
