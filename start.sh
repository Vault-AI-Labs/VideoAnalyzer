#!/usr/bin/env bash
# Start VideoAnalyzer — zero-setup launcher using uv
set -euo pipefail
cd "$(dirname "$0")"
echo "Starting VideoAnalyzer on http://localhost:7745"
~/.local/bin/uv run --with flask app.py
