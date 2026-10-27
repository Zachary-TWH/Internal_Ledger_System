#!/bin/sh
set -e

echo "Creating tables and system accounts (safe to re-run)..."
python -m app.create_table

echo "Starting API..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000