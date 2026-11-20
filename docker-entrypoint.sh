#!/bin/sh
set -eu

mkdir -p /data
chown -R 10001:10001 /data
chmod 700 /data
if [ -f /data/users.db ]; then
    chmod 600 /data/users.db
fi

exec gosu 10001:10001 "$@"
