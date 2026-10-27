#!/bin/bash
# Build the image and (re)create the container on Unraid.
# Run from this folder in the Unraid terminal:  bash unraid-install.sh
# Re-run it after copying in a new version of the code - your data is kept.
set -euo pipefail

NAME=account-cleanup
PORT=${PORT:-8080}
DATA_DIR=${DATA_DIR:-/mnt/user/appdata/account-cleanup/data}
TZ=${TZ:-Europe/London}
APP_PASSWORD=${APP_PASSWORD:-}

cd "$(dirname "$0")"

mkdir -p "$DATA_DIR"
chown 99:100 "$DATA_DIR"   # Unraid's nobody:users, which the container runs as

docker build -t "$NAME" .

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d \
  --name "$NAME" \
  --restart unless-stopped \
  --user 99:100 \
  -p "$PORT:8080" \
  -e TZ="$TZ" \
  -e APP_PASSWORD="$APP_PASSWORD" \
  -v "$DATA_DIR:/data" \
  -l "net.unraid.docker.webui=http://[IP]:[PORT:$PORT]/" \
  "$NAME"

echo "Running - open http://<your-unraid-ip>:$PORT"
