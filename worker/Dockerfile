# syntax=docker/dockerfile:1.7
# ──────────────────────────────────────────────────────────────────────────────
# Worker de sincronización UC3M → iCalendar
#
# Base oficial de Microsoft: trae Python 3, Playwright y los navegadores con
# TODAS sus dependencias de sistema (fuentes, libnss3, libgbm...). La versión de
# la imagen DEBE coincidir con la de `playwright` en requirements.txt; el workflow
# de GitHub Actions la lee de ahí y la pasa como build-arg automáticamente.
# ──────────────────────────────────────────────────────────────────────────────
ARG PLAYWRIGHT_VERSION=1.63.0
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Madrid

WORKDIR /app

COPY requirements.txt .
RUN pip install --break-system-packages -r requirements.txt \
 # La imagen ya incluye Chromium; esto solo verifica que la versión casa
 # (no descarga nada si ya está) y falla el build si no.
 && python -m playwright install chromium \
 && python -c "import playwright, icalendar, apscheduler; print('deps OK')"

COPY sync_uc3m.py .

# Directorios de los volúmenes, propiedad del usuario sin privilegios `pwuser`
# (incluido en la imagen). Los volúmenes con nombre heredan este propietario.
RUN mkdir -p /var/www/calendarios /data/logs /data/debug /data/archive \
 && chown -R pwuser:pwuser /var/www/calendarios /data /app

USER pwuser

# Sano = hubo una sincronización correcta en las últimas STALE_HOURS horas.
HEALTHCHECK --interval=15m --timeout=20s --start-period=10m --retries=2 \
  CMD ["python", "/app/sync_uc3m.py", "--healthcheck"]

ENTRYPOINT ["python", "/app/sync_uc3m.py"]
# Sin argumentos = planificador residente. `docker compose run --rm worker --once`
CMD []
