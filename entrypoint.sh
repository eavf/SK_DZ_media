#!/bin/sh
# Počká kým MariaDB prijíma spojenia, potom spustí hlavný príkaz.
set -e

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-3306}"
RETRIES=30
WAIT=3

echo "Waiting for MariaDB at ${DB_HOST}:${DB_PORT}..."
i=0
until python -c "
import socket, sys
s = socket.socket()
s.settimeout(2)
try:
    s.connect(('${DB_HOST}', ${DB_PORT}))
    s.close()
except Exception as e:
    sys.exit(1)
" 2>/dev/null; do
    i=$((i+1))
    if [ "$i" -ge "$RETRIES" ]; then
        echo "ERROR: MariaDB not available after $((RETRIES * WAIT))s. Giving up."
        exit 1
    fi
    echo "  attempt $i/$RETRIES — retrying in ${WAIT}s..."
    sleep "$WAIT"
done

echo "MariaDB is up. Flushing hosts..."
python -c "
import pymysql, os
try:
    conn = pymysql.connect(
        host=os.getenv('DB_HOST', '127.0.0.1'),
        port=int(os.getenv('DB_PORT', 3306)),
        user=os.getenv('DB_USER', ''),
        password=os.getenv('DB_PASS', ''),
        db=os.getenv('DB_NAME', 'dz_news'),
        connect_timeout=5,
    )
    conn.cursor().execute('FLUSH HOSTS')
    conn.close()
    print('FLUSH HOSTS OK')
except Exception as e:
    print(f'FLUSH HOSTS skipped: {e}')
" || true

echo "Starting application..."
exec "$@"