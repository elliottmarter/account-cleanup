#!/bin/sh
# Run as PUID:PGID (default 1000:1000; Unraid uses 99:100) and make sure /data
# is writable by that user, even when Docker created the host folder as root.
set -e

if [ -z "$APP_USERNAME" ] || [ -z "$APP_PASSWORD" ]; then
    echo "account-cleanup: APP_USERNAME and APP_PASSWORD must both be set. Refusing to start." >&2
    exit 1
fi

if [ "$(id -u)" = "0" ]; then
    PUID=${PUID:-1000}
    PGID=${PGID:-1000}
    mkdir -p /data
    chown -R "$PUID:$PGID" /data
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

# Started with --user: just run as that user.
exec "$@"
