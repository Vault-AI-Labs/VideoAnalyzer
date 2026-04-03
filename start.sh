#!/usr/bin/env bash
# Start VideoTranscriber — zero-setup launcher using uv
set -euo pipefail
cd "$(dirname "$0")"
echo "Starting VideoTranscriber on http://localhost:7745"
~/.local/bin/uv run --with flask app.py
