#!/bin/sh
# Local monthly decay run. Deliberately not a GitHub Action: the vault is
# PRIVATE and local-clone-only by design, and the outputs are licence-
# restricted in a public repo.
set -eu
GRADER="$(cd "$(dirname "$0")/.." && pwd)"
VAULT="${STOCK_VAULT_CLONE:-$HOME/dev/Stock-Vault}"

# Refuse a stale archive: a decay curve computed on a stalled vault is worse
# than none.
NEWEST_EOD=$(ls "$VAULT"/data/market_eod/*/*.jsonl.gz | sed 's#.*/##; s#\.jsonl\.gz##' | sort | tail -1)
AGE_EOD=$(( ( $(date -u +%s) - $(date -ju -f %Y-%m-%d "$NEWEST_EOD" +%s 2>/dev/null || date -u -d "$NEWEST_EOD" +%s) ) / 86400 ))
[ "$AGE_EOD" -le 4 ] || { echo "vault EOD stale: newest $NEWEST_EOD (${AGE_EOD}d old)"; exit 1; }

NEWEST_PANEL=$(ls "$GRADER"/frozen_scores/all_weather/*.parquet | sed 's#.*/##; s#\.parquet##' | sort | tail -1)
AGE_PANEL=$(( ( $(date -u +%s) - $(date -ju -f %Y-%m-%d "$NEWEST_PANEL" +%s 2>/dev/null || date -u -d "$NEWEST_PANEL" +%s) ) / 86400 ))
[ "$AGE_PANEL" -le 40 ] || { echo "freeze clock dead: newest panel $NEWEST_PANEL (${AGE_PANEL}d old)"; exit 1; }

exec "$GRADER"/.venv/bin/stock-grader decay \
  --vault "$VAULT" \
  --frozen-dir "$GRADER"/frozen_scores \
  --profile all_weather --primary-horizon 21 \
  --allow-unverified-panel --format md
