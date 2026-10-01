# uc3m-ical-sync

Pasarela desatendida que convierte la descarga puntual del horario de la **UC3M** en un
**feed iCalendar vivo** servido por HTTPS desde tu propio servidor, para suscribirte desde
**Apple Calendar / iCloud** y tenerlo sincronizado en iPhone, Mac y Apple Watch.

Pensado para desplegarse con **Dockge** (un `compose.yaml` + un `.env`) detrás de **tu
nginx**, que es quien gestiona el dominio y el certificado.

```
                     stack Dockge "uc3m-calendario"
 ┌────────────────────────────────────────────────────────────────┐
 │ worker (Python + Chromium headless)       web (Caddy interno)  │
 │ 04:00 → SSO UC3M → .ics → valida ──vol──► :8080 HTTP ──────────┼─► 127.0.0.1:8088
 │         → publica si cambió                                    │          │
 └──────────────┬─────────────────────────────────────────────────┘          │
                │ alertas                                                    ▼
                ▼                                           TU nginx (dominio + HTTPS)
        Telegram / Discord                                                   │
                                                                             ▼
                                      iCloud ──► iPhone · Mac · Apple Watch
```

## Estructura del repositorio

```
uc3m-ical-sync/
├── compose.yaml                # ← lo que pegas en Dockge
├── .env.example                # ← plantilla del .env del stack (3 valores obligatorios)
├── nginx/calendario.conf       # ← virtual host para tu nginx
├── .github/workflows/
│   └── docker-publish.yml      # CI: tests → build amd64/arm64 → publica en GHCR
├── compose.build.yaml          # solo desarrollo: construir imágenes en local
├── web/                        # imagen del servidor interno (Caddy + config incluida)
└── worker/                     # imagen del worker (sync_uc3m.py + tests)
```

En el servidor **no necesitas el repositorio**: solo `compose.yaml`, `.env` y el fichero de nginx.

## Qué hace cada ejecución

1. **Login**: Chromium headless abre el portal de horarios; el portal redirige al SSO de la
   UC3M; se rellenan `adAS_username` / `adAS_password` y se espera la vuelta al portal.
   Distingue tres fallos: credenciales rechazadas, **MFA solicitado** y portal caído/cambiado.
   Los fallos de credenciales **no se reintentan** (evita bloquear tu cuenta).
2. **Descarga**: usa `UC3M_ICS_URL` (o `UC3M_EXP` + `UC3M_PER`) o, si no se da, descubre el
   enlace `fmt=ics` en la página. Se pide con la misma sesión y se guarda en
   `/tmp/horario_temp.ics`.
3. **Validación**: no vacío, no HTML, `BEGIN/END:VCALENDAR`, parseable, con eventos, y sin
   perder de golpe más del 80 % de los eventos publicados.
4. **Enriquecimiento**: nombre del calendario, intervalo de refresco sugerido, zona
   `Europe/Madrid` en horas sin huso (+ `VTIMEZONE`), UIDs si faltan.
5. **Publicación atómica**: solo si el contenido cambió. Se escribe junto al destino, `fsync`
   y `os.replace`: nunca se sirve un fichero a medias.
6. **Estado y alertas**: estado, logs rotados, últimas 15 versiones y capturas de
   diagnóstico en el volumen `datos`. Avisos de fallo, recuperación y **cambios de
   aula/horario** con el detalle.

Reintentos: hasta 4 por ejecución con espera creciente (60 s, 120 s, 240 s). Si todos
fallan, se alerta y **el feed sigue sirviendo la última versión válida**.

---

## Despliegue paso a paso

### 0. Requisitos

- Servidor Linux (amd64 o arm64) con Docker y Dockge.
- Tu nginx ya funcionando, y un subdominio (p. ej. `calendario.tudominio.es`) con registro
  DNS apuntando al servidor.
- ~1,5 GB de disco para la imagen del worker y 1 GB de RAM libre durante la sincronización.

### 1. Publicar las imágenes con GitHub Actions (una sola vez)

En tu ordenador, sube el proyecto a un repositorio de GitHub:

```bash
cd uc3m-ical-sync
git init -b main && git add . && git commit -m "uc3m-ical-sync"
gh repo create uc3m-ical-sync --public --source . --push     # o créalo desde la web y haz git push
```

El workflow (pestaña *Actions*) pasa los tests, construye las dos imágenes para amd64 y
arm64 y las publica como `ghcr.io/maxcorreiadiaz/uc3m-ical-sync-worker` y `…-web`. Se
repite en cada push a `main` y cada lunes (parches de las imágenes base). Un tag
`v1.0.0` publica también `:1.0.0` y `:1.0`.

El `.gitignore` excluye `.env`: **tus credenciales nunca van al repositorio ni a las
imágenes**. Por eso el repositorio puede ser público, y así el servidor descarga sin
login. Si lo prefieres privado, en el servidor ejecuta una vez
`docker login ghcr.io -u MaxCorreiaDiaz` con un token clásico con permiso `read:packages`.

### 2. Obtener la URL del `.ics` (recomendado)

1. Entra en `https://aplicaciones.uc3m.es/horarios-web/alumno/alumno.page`.
2. Localiza el enlace de exportación iCal/ICS → clic derecho → *Copiar dirección del enlace*.
3. Tendrá la forma `…/verHorario.page?exp=XXXX&per=YYYY&fmt=ics`: va a `UC3M_ICS_URL`.

> `per` cambia cada cuatrimestre: actualiza `UC3M_ICS_URL` al empezar el nuevo. Si la dejas
> vacía, el worker usa el primer enlace `.ics` que encuentre en el portal.

### 3. Crear el stack en Dockge

1. Dockge → **+ Compose** → nombre del stack: `uc3m-calendario`.
2. Pega el contenido de `compose.yaml` tal cual (las imágenes ya apuntan a
   `ghcr.io/maxcorreiadiaz/…`).
3. En el editor **.env** de Dockge pega `.env.example` y rellena:
   - `FEED_TOKEN`: la salida de `openssl rand -hex 24`
   - `UC3M_USER` y `UC3M_PASS`
   - `UC3M_ICS_URL` (paso 2) y, si quieres alertas, `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
4. **Deploy**. Dockge descarga las imágenes y arranca los dos contenedores; el worker hace
   una primera sincronización nada más arrancar (se ve en los logs del stack).

El feed queda escuchando **solo en `127.0.0.1:8088`**, sin exponerse a Internet. Si ese
puerto está ocupado, añade `WEB_BIND=127.0.0.1:OTRO` al `.env`.

### 4. Configurar tu nginx

**nginx instalado en el servidor** (lo habitual):

```bash
# 1) Primero el certificado (antes de activar el sitio, que ya lo referencia)
sudo certbot certonly --nginx -d TU.SUBDOMINIO
# 2) Luego el virtual host
sudo cp calendario.conf /etc/nginx/sites-available/     # el de la carpeta nginx/
sudo sed -i 's/calendario.tudominio.es/TU.SUBDOMINIO/g' /etc/nginx/sites-available/calendario.conf
sudo ln -s /etc/nginx/sites-available/calendario.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

(Si tu distribución usa `/etc/nginx/conf.d/` en lugar de `sites-available`, cópialo ahí.)

Lo importante del bloque es poco: `proxy_pass http://127.0.0.1:8088;`,
`proxy_set_header Host $host;` y `access_log off;` (la URL lleva el token). Las cabeceras
del calendario (tipo MIME, caché, ETag, 304) vienen del contenedor y nginx las deja pasar:
**no añadas `expires` ni `proxy_cache`** en ese bloque, o congelarías el calendario.
Si tu nginx es anterior a 1.25.1, cambia `http2 on;` por `listen 443 ssl http2;`.

**Nginx Proxy Manager** (si tu "nginx" es NPM en Docker):

1. En `compose.yaml` descomenta la línea `networks: [default, npm]` de `web` y el bloque
   `networks:` del final, poniendo el nombre real de la red de NPM (`docker network ls`).
2. En NPM → *Proxy Hosts* → *Add*: dominio `TU.SUBDOMINIO`, esquema `http`, destino
   `uc3m-web`, puerto `8080`. En la pestaña *SSL*: *Request a new certificate* +
   *Force SSL* + *HTTP/2*.

### 5. Probar

Desde la terminal del servidor (o el botón de terminal del contenedor `uc3m-worker` en Dockge):

```bash
docker exec uc3m-worker python /app/sync_uc3m.py --test-alert   # ¿llega a Telegram?
docker exec uc3m-worker cat /data/state.json                    # último éxito, nº de eventos
curl -I "https://TU.SUBDOMINIO/<FEED_TOKEN>/horario_uc3m.ics"
```

El `curl` debe dar `200` con `Content-Type: text/calendar; charset=utf-8`,
`Cache-Control: no-cache, must-revalidate, max-age=0` y `ETag`. Cualquier otra ruta da 404.

Si la primera sincronización falló en el login o no encontró el enlace:

```bash
docker exec uc3m-worker python /app/sync_uc3m.py --discover
docker cp uc3m-worker:/data/debug ./debug     # capturas, HTML, enlaces, tráfico y traza
```

Ahí se ve dónde se paró. Si la UC3M cambió su formulario, ajusta
`UC3M_USERNAME_SELECTOR` / `UC3M_PASSWORD_SELECTOR` / `UC3M_SUBMIT_SELECTOR` en el `.env`.

En `https://TU.SUBDOMINIO/<FEED_TOKEN>/` hay una página con el enlace `webcal://` para
suscribirte con un toque desde el iPhone.

### 6. Actualizaciones

En Dockge, el botón **Update** del stack descarga las imágenes nuevas y recrea lo que haya
cambiado. Para hacerlo automáticamente, una línea en el crontab del servidor (a las 03:30,
antes de la sincronización):

```cron
30 3 * * * cd /opt/stacks/uc3m-calendario && docker compose pull -q && docker compose up -d && docker image prune -f >/dev/null
```

Para no recibir cambios sin revisarlos, sustituye `:latest` por una versión (`:1.0`) en
`compose.yaml`.

---

## Suscripción en Apple Calendar vinculada a iCloud

La clave es que la suscripción quede **guardada en iCloud** y no "En mi Mac" / en un solo
dispositivo: así son los servidores de Apple los que refrescan el feed y lo propagan a
todos tus dispositivos, aunque el Mac esté apagado.

### Opción A — desde el Mac (la más fiable)

1. Abre **Calendario** → menú **Archivo → Nueva suscripción a calendario…** (⌥⌘S).
2. Pega la URL: `https://TU.SUBDOMINIO/<FEED_TOKEN>/horario_uc3m.ics` → **Suscribirse**.
3. En la hoja de opciones:
   - **Nombre**: Horario UC3M (se rellena solo).
   - **Ubicación: iCloud** ← imprescindible para que aparezca en iPhone y Watch.
   - **Actualización automática**: *Cada hora* (o *Cada día*; el feed solo cambia de madrugada).
   - **Eliminar**: marca *Alertas* si no quieres avisos que no hayas creado tú; *Adjuntos* también.
4. **Aceptar**. En unos minutos aparecerá en el iPhone (Calendario → Calendarios, bajo iCloud).

### Opción B — desde el iPhone

1. Abre en Safari `https://TU.SUBDOMINIO/<FEED_TOKEN>/` y pulsa **Suscribirse (webcal)**, o
   en **Calendario → Calendarios → Añadir calendario → Añadir calendario suscrito** pega la URL.
2. Si te deja elegir **Cuenta**, selecciona **iCloud**. Si solo lo añade bajo
   "Suscritos" / "Otros", quedará únicamente en ese iPhone: en ese caso usa la Opción A.

### Apple Watch

El reloj muestra los calendarios del iPhone: app **Watch** → **Calendario** →
*Espejo de mi iPhone* (o marca "Horario UC3M" en calendarios personalizados).

### Sobre la frecuencia real de refresco

Apple no documenta con qué cadencia exacta iCloud vuelve a pedir un calendario suscrito;
la *Actualización automática* es una preferencia y en la práctica puede tardar algo más.
Como el worker publica a las 04:00, lo normal es ver los cambios a primera hora. Para
forzarlo en el Mac: **Visualización → Actualizar calendarios** (⌘R).

---

## Operación diaria

| Tarea | Cómo |
|---|---|
| Ver logs | Dockge → stack → logs, o `docker logs -f uc3m-worker` |
| Estado (último éxito, errores, nº eventos) | `docker exec uc3m-worker cat /data/state.json` |
| Forzar sincronización ahora | `docker exec uc3m-worker python /app/sync_uc3m.py --once` |
| Cambiaste la contraseña UC3M | edita `UC3M_PASS` en el `.env` de Dockge → *Save* → *Restart* |
| Nuevo cuatrimestre | actualiza `UC3M_ICS_URL` en el `.env` → *Save* → *Restart* |
| Horario legítimamente mucho más corto | `docker exec -e ALLOW_SHRINK=true uc3m-worker python /app/sync_uc3m.py --once` |
| Rotar la URL secreta | nuevo `FEED_TOKEN` en el `.env` → *Restart* → vuelve a suscribirte |
| Actualizar a la última versión | Dockge → *Update* |
| Volver a una versión anterior | en `compose.yaml` cambia `:latest` por `:sha-<commit>` o `:1.0.2` → *Deploy* |
| Cambiar el código | push a `main` → check verde en *Actions* → Dockge *Update* |
| Construir en local sin GHCR | `docker compose -f compose.yaml -f compose.build.yaml up -d --build` |

**Monitorización externa (recomendado):** crea un check en healthchecks.io (o un monitor
"push" de Uptime Kuma) con periodo 1 día y gracia de 6 h, y pon su URL en
`HEALTHCHECK_PING_URL`. Te avisará incluso si el servidor entero se cae.

## Resolución de problemas

| Síntoma / alerta | Causa probable | Qué hacer |
|---|---|---|
| "Login rechazado" | Contraseña cambiada o caducada | Actualiza `UC3M_PASS`. No se reintenta para no bloquear la cuenta. |
| "El SSO pide un segundo factor (MFA)" | La UC3M exige MFA en tu cuenta | El login desatendido no es viable; tendrías que descargar el .ics a mano. |
| "Portal no disponible o ha cambiado" | Caída, mantenimiento o rediseño | Se reintenta solo. Si persiste varios días, `--discover` y ajusta selectores. |
| "No es un calendario válido" | Página de error servida con 200 | Normalmente transitorio. Revisa `/data/debug`. |
| "Cambio sospechoso" | El portal devolvió un horario casi vacío | Comprueba el portal; si es correcto, `ALLOW_SHRINK=true` una vez. |
| 502 Bad Gateway en tu dominio | El stack está parado o `WEB_BIND` no coincide con `proxy_pass` | Revisa el stack en Dockge y el puerto en ambos sitios. |
| El enlace webcal:// de la página sale con `127.0.0.1` | Falta `proxy_set_header Host $host;` en nginx | Añádelo y recarga nginx. |
| Deploy falla: "FEED_TOKEN is missing" | `.env` sin token | Rellénalo con `openssl rand -hex 24`. |
| Deploy falla al descargar la imagen | Usuario mal escrito o repositorio privado | Usuario en minúsculas en `compose.yaml`; si es privado, `docker login ghcr.io`. |
| Clases desplazadas 1–2 h | Horas sin zona en el .ics original | Deja `FORCE_TIMEZONE=true` (por defecto). |
| El iPhone no lo muestra | Suscripción creada fuera de iCloud | Repite la Opción A con *Ubicación: iCloud*. |

## Seguridad

- El `.env` que guarda Dockge (`/opt/stacks/uc3m-calendario/.env`) contiene tu contraseña:
  `chmod 600` y que solo root/tu usuario puedan leer esa carpeta.
- La contraseña y los tokens se enmascaran en los logs y en el HTML de diagnóstico.
- El feed solo se sirve con el token de la URL y todo lo demás da 404. Trátala como una
  contraseña: quien la tenga ve tu horario. El puerto interno solo escucha en `127.0.0.1`.
- Contenedores sin capacidades (`cap_drop: ALL`), `no-new-privileges`, worker como usuario
  sin privilegios y web de solo lectura.
- Automatizas el acceso **a tu propia cuenta** con una sola petición diaria; revisa en todo
  caso la normativa de uso de servicios TIC de la UC3M.

## Tests

```bash
cd worker
pip install -r requirements.txt pytest && python -m playwright install chromium
python -m pytest -q
```

Incluyen un portal y un SSO simulados (hosts distintos, formulario `adAS_*`) para comprobar
de extremo a extremo el login correcto, la contraseña errónea sin reintentos y la
detección de MFA con captura de diagnóstico.
