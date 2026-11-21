#!/bin/sh
# Generate a self-signed TLS certificate on first start (no domain needed).
# Browsers will show a warning: this is expected for a self-signed cert.
set -e
CERT_DIR=/etc/nginx/certs
if [ ! -f "$CERT_DIR/server.crt" ]; then
  mkdir -p "$CERT_DIR"
  echo "Generating self-signed certificate..."
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout "$CERT_DIR/server.key" -out "$CERT_DIR/server.crt" \
    -subj "/CN=exam-analyzer"
fi
