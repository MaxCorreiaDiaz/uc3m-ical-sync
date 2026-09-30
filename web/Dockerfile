# Servidor interno del feed: Caddy oficial + Caddyfile ya incluido, para que en
# el servidor baste con compose.yaml + .env (sin ficheros montados).
FROM caddy:2.11-alpine

# La imagen oficial marca /usr/bin/caddy con la capability de fichero
# cap_net_bind_service (para escuchar en 80/443). Con `cap_drop: ALL` +
# `no-new-privileges` (como en nuestro compose) el kernel se niega a ejecutar un
# binario que pide capabilities fuera del conjunto permitido:
#   "exec /usr/bin/caddy: operation not permitted"
# Aquí escuchamos en 8080, así que no la necesitamos: una copia simple del binario
# no conserva los atributos extendidos y elimina esa marca.
RUN cp /usr/bin/caddy /usr/bin/caddy.tmp \
 && mv /usr/bin/caddy.tmp /usr/bin/caddy \
 && chmod 0755 /usr/bin/caddy

COPY Caddyfile /etc/caddy/Caddyfile

# Falla el build si el Caddyfile no es válido (token ficticio solo para validar)
RUN FEED_TOKEN=0123456789abcdef0123456789abcdef \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

EXPOSE 8080
