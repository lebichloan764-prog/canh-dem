#!/bin/bash
cd "$(dirname "$0")"
set -a; . ./.env; set +a
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec $PY autofire2.py
