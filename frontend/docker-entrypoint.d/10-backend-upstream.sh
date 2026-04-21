#!/bin/sh
set -e
# Railway injects PORT; nginx must listen on it (default 80 for docker-compose).
LISTEN_PORT="${PORT:-80}"
sed -i "s|listen 80;|listen ${LISTEN_PORT};|" /etc/nginx/conf.d/default.conf
echo "nginx listen port: ${LISTEN_PORT}"
