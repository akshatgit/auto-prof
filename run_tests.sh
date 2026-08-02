#!/usr/bin/env bash
# Zero-dependency test runner (stdlib unittest, no pytest).
set -euo pipefail
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -p 'test_*.py' -v
