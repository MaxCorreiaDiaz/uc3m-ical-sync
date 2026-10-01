# syntax=docker/dockerfile:1.7
# ──────────────────────────────────────────────────────────────────────────────
# Worker de sincronización UC3M → iCalendar
#
# Base ligera (Debian slim) + SOLO el "headless shell" de Chromium con sus
# dependencias de sistema. Es exactamente lo que usa el worker (siempre corre
# sin ventana) y evita los ~1,5 GB de la imagen oficial de Playwright, que trae
# Chromium, Firefox y WebKit completos.
#
# La versión de Playwright sale de requirements.txt (pip), así que el navegador
# instalado siempre coincide con la librería.
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Madrid

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
 # Solo chromium-headless-shell + librerías del sistema que necesita
 && python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /root/.cache /tmp/* \
 && python -c "import playwright, icalendar, apscheduler; print('deps OK')"

COPY sync_uc3m.py .

# Usuario sin privilegios. UID/GID 1001 = el mismo que tenía `pwuser` en la
# imagen anterior, para que los volúmenes ya creados sigan siendo escribibles.
RUN groupadd --gid 1001 app \
 && useradd --uid 1001 --gid 1001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /var/www/calendarios /data/logs /data/debug /data/archive \
 && chown -R app:app /var/www/calendarios /data

USER app

# Sano = hubo una sincronización correcta en las últimas STALE_HOURS horas.
HEALTHCHECK --interval=15m --timeout=20s --start-period=10m --retries=2 \
  CMD ["python", "/app/sync_uc3m.py", "--healthcheck"]

ENTRYPOINT ["python", "/app/sync_uc3m.py"]
# Sin argumentos = planificador residente.  Una ejecución: `... --once`
CMD []
