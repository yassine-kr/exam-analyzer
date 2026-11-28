#!/bin/sh
# Runs before nginx starts. Chooses the TLS certificate:
#  - DOMAIN set and a Let's Encrypt certificate exists -> use it (trusted)
#  - otherwise -> self-signed certificate (browser warning, works with an IP)
set -e
CERT_DIR=/etc/nginx/certs
LE_DIR="/etc/letsencrypt/live/${DOMAIN}"
mkdir -p "$CERT_DIR" /var/www/certbot

if [ ! -f "$CERT_DIR/server.crt" ]; then
  echo "Generating self-signed certificate..."
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" \
    -subj "/CN=${DOMAIN:-exam-analyzer}"
fi

if [ -n "$DOMAIN" ] && [ -f "$LE_DIR/fullchain.pem" ]; then
  echo "Using Let's Encrypt certificate for $DOMAIN"
  ln -sf "$LE_DIR/fullchain.pem" "$CERT_DIR/active.crt"
  ln -sf "$LE_DIR/privkey.pem" "$CERT_DIR/active.key"
else
  echo "Using self-signed certificate"
  ln -sf "$CERT_DIR/server.crt" "$CERT_DIR/active.crt"
  ln -sf "$CERT_DIR/server.key" "$CERT_DIR/active.key"
fi

# Reload nginx every 12 h so a renewed certificate is picked up
(while sleep 43200; do nginx -s reload; done) &
