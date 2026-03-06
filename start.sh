#!/bin/bash
# Script de démarrage pour PM2
# Usage: pm2 start start.sh --name search-app

export SEARCH_PASSWORD="${SEARCH_PASSWORD:-changeme}"
export FLASK_SECRET="${FLASK_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"
export BOOKLORE_DIR="${BOOKLORE_DIR:-/opt/booklore/bookdrop}"
export CONFIG_FILE="${CONFIG_FILE:-/opt/search-app/config.json}"

cd "$(dirname "$0")"
exec ./venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 app:app
