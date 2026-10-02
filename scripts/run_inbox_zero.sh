#!/bin/bash
set -e
cd "$(dirname "$0")/.."
mkdir -p logs
echo "=== $(date) ===" >> logs/inbox_zero.log
PYTHONPATH=src .venv/bin/python -m gmail_cleanup.inbox_reduce all \
    --all-accounts --no-dry-run >> logs/inbox_zero.log 2>&1
