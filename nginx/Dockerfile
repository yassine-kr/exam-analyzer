FROM nginx:1.27-alpine
RUN apk add --no-cache openssl
COPY default.conf /etc/nginx/conf.d/default.conf
# Scripts in /docker-entrypoint.d/ run before nginx starts
COPY 40-select-cert.sh /docker-entrypoint.d/40-select-cert.sh
RUN chmod +x /docker-entrypoint.d/40-select-cert.sh
EXPOSE 80 443
