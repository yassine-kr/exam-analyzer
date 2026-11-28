#!/usr/bin/env bash
# Gets a trusted Let's Encrypt certificate for DOMAIN (from .env), then restarts nginx.
# Prerequisites: DNS record DOMAIN -> this instance's public IP, port 80 open,
#                `docker compose up -d --build` already running.
set -euo pipefail
cd "$(dirname "$0")"
DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d= -f2- | tr -d '"[:space:]')
EMAIL=$(grep -E '^LETSENCRYPT_EMAIL=' .env | cut -d= -f2- | tr -d '"[:space:]')
[ -n "$DOMAIN" ] || { echo "Set DOMAIN=... in .env"; exit 1; }
[ -n "$EMAIL" ] || { echo "Set LETSENCRYPT_EMAIL=... in .env"; exit 1; }

echo "DNS check: $DOMAIN -> $(getent hosts "$DOMAIN" | awk '{print $1}')" \
     "(this instance: $(curl -s https://checkip.amazonaws.com))"

docker compose run --rm --entrypoint certbot certbot certonly \
  --webroot -w /var/www/certbot -d "$DOMAIN" \
  --email "$EMAIL" --agree-tos --no-eff-email --non-interactive

docker compose up -d --force-recreate nginx
echo "Done: https://$DOMAIN"
