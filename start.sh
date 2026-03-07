#!/bin/bash
# Script de démarrage pour PM2
# Usage: pm2 start start.sh --name search-app

rm -f /opt/search-app/gunicorn.ctl

export SEARCH_PASSWORD="${SEARCH_PASSWORD:-Sabuuu92i@08}"
export FLASK_SECRET="${FLASK_SECRET:-xK9mP2nQ8vR5wT7uY3jL}"
export BOOKLORE_DIR="${BOOKLORE_DIR:-/opt/booklore/bookdrop}"
export CONFIG_FILE="${CONFIG_FILE:-/opt/search-app/config.json}"

cd "$(dirname "$0")"
exec ./venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 app:app

