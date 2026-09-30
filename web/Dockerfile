# Servidor interno del feed: Caddy oficial + Caddyfile ya incluido, para que en
# el servidor baste con compose.yaml + .env (sin ficheros montados).
FROM caddy:2.11-alpine

COPY Caddyfile /etc/caddy/Caddyfile

# Falla el build si el Caddyfile no es válido (token ficticio solo para validar)
RUN FEED_TOKEN=0123456789abcdef0123456789abcdef \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

EXPOSE 8080
