#!/bin/bash
# Script de démarrage pour PM2
# Usage: pm2 start start.sh --name search-app

rm -f /opt/search-app/gunicorn.ctl

export SEARCH_PASSWORD="${SEARCH_PASSWORD:-Sabuuu92i@08}"
export FLASK_SECRET="${FLASK_SECRET:-xK9mP2nQ8vR5wT7uY3jL}"
export BOOKLORE_DIR="${BOOKLORE_DIR:-/srv/booklore/bookdrop}"
export CONFIG_FILE="${CONFIG_FILE:-/srv/search-app/config.json}"

cd "$(dirname "$0")"
exec ./venv/bin/waitress-serve --host=0.0.0.0 --port=5000 app:app

