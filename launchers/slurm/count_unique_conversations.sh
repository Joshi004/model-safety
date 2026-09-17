#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="${1:-"$SCRIPT_DIR/out/pii_run"}"

if [[ -d "$RUN_DIR/stage1_scan/hits" ]]; then
  HITS_DIR="$RUN_DIR/stage1_scan/hits"
elif [[ -d "$RUN_DIR/hits" ]]; then
  HITS_DIR="$RUN_DIR/hits"
elif [[ -d "$RUN_DIR" ]]; then
  HITS_DIR="$RUN_DIR"
else
  echo "Input directory does not exist: $RUN_DIR" >&2
  exit 1
fi

OUT_DIR="$RUN_DIR/stage1_scan/unique_conversations"
COUNTS_CSV="$OUT_DIR/counts.csv"

categories=(
  emails phones phones_with_exts ukphones
  street_addresses po_boxes credit_cards
  ips ipv6s btc_addresses
)

mkdir -p "$OUT_DIR"
printf 'category,unique_conversations\n' > "$COUNTS_CSV"

for category in "${categories[@]}"; do
  input="$HITS_DIR/${category}.txt"
  output="$OUT_DIR/${category}.txt"

  if [[ ! -f "$input" ]]; then
    continue
  fi

  awk -F'|' 'NF >= 3 && $2 ~ /^[0-9]+$/ { print $1 "|" $2 }' "$input" \
    | LC_ALL=C sort -u \
    > "$output"

  printf '%s,%s\n' "$category" "$(wc -l < "$output")" >> "$COUNTS_CSV"
done

if compgen -G "$OUT_DIR/*.txt" > /dev/null; then
  LC_ALL=C sort -u "$OUT_DIR"/*.txt > "$OUT_DIR/global.txt"
  printf 'TOTAL,%s\n' "$(wc -l < "$OUT_DIR/global.txt")" >> "$COUNTS_CSV"
fi

echo "Hits directory: $HITS_DIR"
echo "Wrote unique conversation files to: $OUT_DIR"
echo "Wrote counts to: $COUNTS_CSV"
