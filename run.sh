#!/bin/bash
cd "$(dirname "$0")"
set -a; . ./.env; set +a
exec python3 autofire2.py
