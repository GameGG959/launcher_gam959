#!/bin/sh
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
cd server
exec gunicorn -w 2 -b 0.0.0.0:8080 app:app
