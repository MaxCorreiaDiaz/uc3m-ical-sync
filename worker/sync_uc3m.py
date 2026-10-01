#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_uc3m.py — Pasarela de sincronización UC3M → feed iCalendar (Apple Calendar / iCloud).

Flujo de una ejecución
======================
1. ``fetch_ics``      Chromium headless (Playwright) abre el portal de horarios, que
                      redirige al SSO de la UC3M (adAS). Rellena usuario/contraseña,
                      vuelve al portal ya autenticado y descarga el .ics con la misma
                      sesión (``context.request``, comparte cookies con el navegador).
2. ``validate_ics``   El fichero temporal (/tmp/horario_temp.ics) se valida: no vacío,
                      no HTML, VCALENDAR bien formado, parseable, con >= N eventos y sin
                      una caída brusca de eventos respecto a la versión publicada.
3. ``enrich_ics``     Se añaden propiedades que Apple Calendar entiende
                      (X-WR-CALNAME, REFRESH-INTERVAL, X-PUBLISHED-TTL, X-WR-TIMEZONE) y
                      UID deterministas si faltan.
4. ``publish_atomic`` Solo si el contenido ha cambiado: se escribe junto al destino y se
                      hace ``os.replace`` (rename atómico en el mismo sistema de ficheros).
                      Nginx/Caddy nunca sirven un fichero a medio escribir.
5. Estado, métricas y alertas (Telegram / Discord) + ping opcional a un "dead man's
   switch" (healthchecks.io, Uptime Kuma push...).

Modos de ejecución
==================
    python sync_uc3m.py                 # planificador residente (cron 04:00 por defecto)
    python sync_uc3m.py --once          # una sincronización y salir (código 0/1)
    python sync_uc3m.py --discover      # login + volcado de diagnóstico para calibrar
    python sync_uc3m.py --validate F    # valida un .ics local y muestra resumen
    python sync_uc3m.py --healthcheck   # usado por el HEALTHCHECK de Docker
    python sync_uc3m.py --test-alert    # envía una alerta de prueba

Toda la configuración se lee de variables de entorno (ver ``.env.example``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar, vDuration

log = logging.getLogger("sync_uc3m")

# --------------------------------------------------------------------------------------
# Excepciones
# --------------------------------------------------------------------------------------


class SyncError(Exception):
    """Base de todos los errores del pipeline.

    ``retryable`` indica si tiene sentido reintentar dentro de la misma ejecución.
    ``alert_key`` agrupa errores del mismo tipo para no repetir alertas idénticas.
    """

    retryable: bool = True
    alert_key: str = "sync_error"
    title: str = "Error de sincronización"


class AuthenticationError(SyncError):
    """Credenciales rechazadas por el SSO. NO se reintenta: repetir logins fallidos
    puede provocar el bloqueo temporal de la cuenta."""

    retryable = False
    alert_key = "auth"
    title = "Login rechazado (¿contraseña cambiada?)"


class MFARequiredError(AuthenticationError):
    """El SSO pide un segundo factor: el login desatendido ya no es posible."""

    alert_key = "mfa"
    title = "El SSO de la UC3M pide un segundo factor (MFA)"


class PortalUnavailableError(SyncError):
    """Timeouts, 5xx, páginas de mantenimiento, cambios de maquetación..."""

    alert_key = "portal"
    title = "Portal UC3M no disponible o ha cambiado"


class InvalidCalendarError(SyncError):
    """Lo descargado no es un iCalendar válido (HTML de error, vacío, truncado...)."""

    alert_key = "invalid_ics"
    title = "El fichero descargado no es un calendario válido"


class SuspiciousChangeError(SyncError):
    """El nuevo calendario tiene muchos menos eventos que el publicado. No se
    reintenta ni se publica: se mantiene la versión anterior y se avisa."""

    retryable = False
    alert_key = "shrink"
    title = "Cambio sospechoso: el horario ha perdido muchos eventos"


class ConfigError(SyncError):
    retryable = False
    alert_key = "config"
    title = "Configuración inválida"


# --------------------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------------------


def _env(name: str, default: str | None = None) -> str | None:
    """Lee NAME o, si existe, el contenido del fichero NAME_FILE (Docker secrets)."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"No se puede leer {name}_FILE={file_path}: {exc}") from exc
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un entero (valor: {raw!r})") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigError(f"{name} debe ser un número (valor: {raw!r})") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


@dataclass
class Config:
    # Credenciales
    user: str | None
    password: str | None = field(repr=False)

    # Portal / SSO
    portal_url: str
    ics_url: str | None
    exp: str | None
    per: str | None
    ics_link_selector: str
    username_selector: str
    password_selector: str
    submit_selector: str
    login_error_selector: str
    mfa_selector: str
    mfa_text_regex: str
    nav_timeout_ms: int
    login_timeout_ms: int
    chromium_executable: str | None
    chromium_args: list[str]
    user_agent: str

    # Ficheros
    temp_path: Path
    publish_path: Path
    data_dir: Path
    log_dir: Path

    # Validación / publicación
    min_events: int
    max_bytes: int
    min_ratio: float
    allow_shrink: bool
    calendar_name: str
    calendar_tz: str
    refresh_interval: timedelta
    force_timezone: bool
    archive_keep: int

    # Planificación / reintentos
    cron: str
    jitter_s: int
    run_on_start: bool
    max_attempts: int
    backoff_base_s: float
    stale_hours: int

    # Alertas
    telegram_token: str | None = field(repr=False)
    telegram_chat_id: str | None
    discord_webhook: str | None = field(repr=False)
    notify_on_change: bool
    notify_on_recovery: bool
    ping_url: str | None

    # Logs
    log_level: str
    log_max_bytes: int
    log_backups: int
    debug_keep: int

    @classmethod
    def from_env(cls) -> "Config":
        refresh_h = _env_float("REFRESH_INTERVAL_HOURS", 6)
        cfg = cls(
            user=_env("UC3M_USER"),
            password=_env("UC3M_PASS"),
            portal_url=_env(
                "UC3M_PORTAL_URL",
                "https://aplicaciones.uc3m.es/horarios-web/alumno/alumno.page",
            ),
            ics_url=_env("UC3M_ICS_URL"),
            exp=_env("UC3M_EXP"),
            per=_env("UC3M_PER"),
            ics_link_selector=_env("UC3M_ICS_LINK_SELECTOR", 'a[href*="fmt=ics"]'),
            username_selector=_env(
                "UC3M_USERNAME_SELECTOR",
                'input[name="adAS_username"], input#username, input[name="username"], '
                'input[type="email"]',
            ),
            password_selector=_env(
                "UC3M_PASSWORD_SELECTOR",
                'input[name="adAS_password"], input#password, input[name="password"], '
                'input[type="password"]',
            ),
            submit_selector=_env(
                "UC3M_SUBMIT_SELECTOR",
                '#submit_ok, button[type="submit"], input[type="submit"]',
            ),
            login_error_selector=_env(
                "UC3M_LOGIN_ERROR_SELECTOR",
                ".error, .alert-danger, .alert-error, #error, [role=alert]",
            ),
            mfa_selector=_env(
                "UC3M_MFA_SELECTOR",
                'input[autocomplete="one-time-code"], input[name*="otp" i], '
                'input[name*="totp" i], input[name*="token" i][type="text"]',
            ),
            mfa_text_regex=_env(
                "UC3M_MFA_TEXT_REGEX",
                r"(segundo factor|doble factor|autenticaci[oó]n multifactor|c[oó]digo de "
                r"verificaci[oó]n|verification code|two[- ]factor|\bMFA\b|\b2FA\b|OTP)",
            ),
            nav_timeout_ms=_env_int("NAV_TIMEOUT_MS", 45_000),
            login_timeout_ms=_env_int("LOGIN_TIMEOUT_MS", 30_000),
            chromium_executable=_env("CHROMIUM_EXECUTABLE"),
            chromium_args=[a for a in (_env("CHROMIUM_ARGS", "") or "").split() if a],
            user_agent=_env(
                "USER_AGENT",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36",
            ),
            temp_path=Path(_env("TEMP_PATH", "/tmp/horario_temp.ics")),
            publish_path=Path(
                _env("PUBLISH_PATH", "/var/www/calendarios/horario_uc3m.ics")
            ),
            data_dir=Path(_env("DATA_DIR", "/data")),
            log_dir=Path(_env("LOG_DIR", "/data/logs")),
            min_events=_env_int("MIN_EVENTS", 1),
            max_bytes=_env_int("MAX_ICS_BYTES", 10 * 1024 * 1024),
            min_ratio=_env_float("MIN_EVENT_RATIO", 0.2),
            allow_shrink=_env_bool("ALLOW_SHRINK", False),
            calendar_name=_env("CALENDAR_NAME", "Horario UC3M"),
            calendar_tz=_env("CALENDAR_TZ", "Europe/Madrid"),
            refresh_interval=timedelta(hours=refresh_h),
            force_timezone=_env_bool("FORCE_TIMEZONE", True),
            archive_keep=_env_int("ARCHIVE_KEEP", 15),
            cron=_env("SYNC_CRON", "0 4 * * *"),
            jitter_s=_env_int("SYNC_JITTER_SECONDS", 300),
            run_on_start=_env_bool("RUN_ON_START", True),
            max_attempts=_env_int("SYNC_MAX_ATTEMPTS", 4),
            backoff_base_s=_env_float("SYNC_BACKOFF_SECONDS", 60),
            stale_hours=_env_int("STALE_HOURS", 50),
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            discord_webhook=_env("DISCORD_WEBHOOK_URL"),
            notify_on_change=_env_bool("NOTIFY_ON_CHANGE", True),
            notify_on_recovery=_env_bool("NOTIFY_ON_RECOVERY", True),
            ping_url=_env("HEALTHCHECK_PING_URL"),
            log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
            log_max_bytes=_env_int("LOG_MAX_BYTES", 2 * 1024 * 1024),
            log_backups=_env_int("LOG_BACKUP_COUNT", 7),
            debug_keep=_env_int("DEBUG_KEEP", 10),
        )
        return cfg

    def require_credentials(self) -> None:
        if not self.user or not self.password:
            raise ConfigError("Faltan UC3M_USER y/o UC3M_PASS en el entorno (.env).")

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def debug_dir(self) -> Path:
        return self.data_dir / "debug"

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    def explicit_ics_url(self) -> str | None:
        """URL directa del .ics si se ha configurado (entera o por exp/per)."""
        if self.ics_url:
            return self.ics_url
        if self.exp and self.per:
            base = urljoin(self.portal_url, "verHorario.page")
            return f"{base}?exp={self.exp}&per={self.per}&fmt=ics"
        return None


# --------------------------------------------------------------------------------------
# Logging con rotación
# --------------------------------------------------------------------------------------


class _RedactingFormatter(logging.Formatter):
    """Formatter que enmascara secretos en la línea YA formateada, incluidos
    tracebacks y stack traces (un filtro sobre el mensaje no los cubre)."""

    def __init__(self, fmt: str, datefmt: str, secrets: Iterable[str | None]):
        super().__init__(fmt, datefmt)
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for s in self._secrets:
            text = text.replace(s, "***")
        return text


def setup_logging(cfg: Config) -> None:
    fmt = _RedactingFormatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
        [cfg.password, cfg.telegram_token, cfg.discord_webhook, _env("FEED_TOKEN")],
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(cfg.log_level)

    stream = logging.StreamHandler(sys.stdout)  # → docker logs
    stream.setFormatter(fmt)
    root.addHandler(stream)

    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        fileh = RotatingFileHandler(
            cfg.log_dir / "sync_uc3m.log",
            maxBytes=cfg.log_max_bytes,
            backupCount=cfg.log_backups,
            encoding="utf-8",
        )
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError as exc:  # p. ej. volumen de solo lectura: seguimos solo con stdout
        log.warning("No se puede escribir el log en %s: %s", cfg.log_dir, exc)

    for noisy in ("apscheduler", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------------------
# Estado persistente (/data/state.json)
# --------------------------------------------------------------------------------------


def load_state(cfg: Config) -> dict[str, Any]:
    try:
        return json.loads(cfg.state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("state.json ilegible (%s); se empieza con estado vacío", exc)
        return {}


def save_state(cfg: Config, state: dict[str, Any]) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cfg.state_file)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------------------
# Notificaciones
# --------------------------------------------------------------------------------------


class Notifier:
    """Envía mensajes a Telegram y/o Discord. Nunca lanza excepciones: un fallo
    notificando no debe tumbar la sincronización."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(
            (self.cfg.telegram_token and self.cfg.telegram_chat_id) or self.cfg.discord_webhook
        )

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("Alertas no configuradas; mensaje no enviado: %s", text.splitlines()[0])
            return False
        ok = True
        if self.cfg.telegram_token and self.cfg.telegram_chat_id:
            ok &= self._post(
                f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                {
                    "chat_id": self.cfg.telegram_chat_id,
                    "text": text[:4000],
                    "disable_web_page_preview": True,
                },
                "Telegram",
            )
        if self.cfg.discord_webhook:
            ok &= self._post(self.cfg.discord_webhook, {"content": text[:1900]}, "Discord")
        return ok

    @staticmethod
    def _post(url: str, payload: dict[str, Any], name: str) -> bool:
        for attempt in (1, 2):
            try:
                r = requests.post(url, json=payload, timeout=15)
                if r.status_code < 300:
                    return True
                log.warning("%s respondió %s: %s", name, r.status_code, r.text[:200])
            except requests.RequestException as exc:
                log.warning("Fallo enviando a %s (intento %d): %s", name, attempt, exc)
            time.sleep(2)
        return False


def ping_healthcheck(cfg: Config, suffix: str = "") -> None:
    """Dead man's switch: si el worker muere, el servicio externo avisa por su cuenta."""
    if not cfg.ping_url:
        return
    try:
        requests.get(cfg.ping_url.rstrip("/") + suffix, timeout=10)
    except requests.RequestException as exc:
        log.warning("No se pudo hacer ping a HEALTHCHECK_PING_URL: %s", exc)


# --------------------------------------------------------------------------------------
# 1) Extracción con Playwright
# --------------------------------------------------------------------------------------


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _prune_dir(directory: Path, keep: int) -> None:
    try:
        items = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except FileNotFoundError:
        return
    for old in items[keep:]:
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)


def _dump_debug(cfg: Config, page: Any, label: str) -> Path | None:
    """Guarda captura + HTML + URL del estado actual del navegador (sin credenciales)."""
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = cfg.debug_dir / f"{stamp}-{label}"
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / "page.png"), full_page=True)
        html = page.content()
        if cfg.password:
            html = html.replace(cfg.password, "***")
        (out / "page.html").write_text(html, encoding="utf-8")
        (out / "url.txt").write_text(page.url + "\n", encoding="utf-8")
        _prune_dir(cfg.debug_dir, cfg.debug_keep)
        log.info("Diagnóstico guardado en %s", out)
        return out
    except Exception as exc:  # noqa: BLE001 — el volcado es best-effort
        log.warning("No se pudo guardar el diagnóstico: %s", exc)
        return None


def _first_visible(page: Any, selector: str, timeout_ms: int) -> Any | None:
    """Devuelve el primer elemento visible que case con alguno de los selectores
    (lista separada por comas) o None si no aparece en ``timeout_ms``."""
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        return loc
    except Exception:  # noqa: BLE001 — TimeoutError de Playwright
        return None


def _looks_like_mfa(cfg: Config, page: Any) -> bool:
    if _first_visible(page, cfg.mfa_selector, 1_000):
        return True
    try:
        body = page.inner_text("body", timeout=2_000)
    except Exception:  # noqa: BLE001
        return False
    return bool(re.search(cfg.mfa_text_regex, body, re.IGNORECASE))


def _login(cfg: Config, page: Any, portal_host: str) -> None:
    """Rellena el formulario del SSO y espera a volver al portal."""
    if _host(page.url) == portal_host and not _first_visible(
        page, cfg.password_selector, 2_000
    ):
        log.info("Sin formulario de login: la sesión ya está autenticada.")
        return

    user_box = _first_visible(page, cfg.username_selector, cfg.login_timeout_ms)
    pass_box = _first_visible(page, cfg.password_selector, 5_000)
    if not user_box or not pass_box:
        _dump_debug(cfg, page, "no-login-form")
        raise PortalUnavailableError(
            f"No se encontró el formulario de login en {page.url}. ¿Ha cambiado el SSO? "
            "Ajusta UC3M_USERNAME_SELECTOR / UC3M_PASSWORD_SELECTOR (usa --discover)."
        )

    log.info("Formulario SSO detectado en %s; enviando credenciales.", _host(page.url))
    user_box.fill(cfg.user or "")
    pass_box.fill(cfg.password or "")

    submit = _first_visible(page, cfg.submit_selector, 3_000)
    if submit:
        submit.click()
    else:
        pass_box.press("Enter")

    try:
        page.wait_for_url(lambda u: _host(u) == portal_host, timeout=cfg.login_timeout_ms)
        page.wait_for_load_state("domcontentloaded", timeout=cfg.nav_timeout_ms)
    except Exception:  # noqa: BLE001 — seguimos en el SSO: averiguamos por qué
        page.wait_for_load_state("domcontentloaded", timeout=5_000)
        if _looks_like_mfa(cfg, page):
            _dump_debug(cfg, page, "mfa")
            raise MFARequiredError(
                "El SSO solicita un segundo factor. El login desatendido no puede "
                "continuar; revisa la configuración de tu cuenta UC3M."
            ) from None
        if _first_visible(page, cfg.password_selector, 1_000):
            detail = ""
            err = _first_visible(page, cfg.login_error_selector, 1_000)
            if err:
                detail = f" Mensaje del SSO: {err.inner_text()[:200].strip()}"
            _dump_debug(cfg, page, "auth-failed")
            raise AuthenticationError(
                "El SSO ha rechazado las credenciales (el formulario sigue en "
                f"pantalla).{detail}"
            ) from None
        _dump_debug(cfg, page, "login-stuck")
        raise PortalUnavailableError(
            f"Tras el login no se volvió al portal (URL actual: {page.url})."
        ) from None
    log.info("Login correcto; de vuelta en %s", portal_host)


def _discover_ics_links(cfg: Config, page: Any) -> list[str]:
    try:
        hrefs = page.eval_on_selector_all(
            cfg.ics_link_selector, "els => els.map(e => e.href).filter(Boolean)"
        )
    except Exception:  # noqa: BLE001
        hrefs = []
    seen: list[str] = []
    for h in hrefs:
        if h not in seen:
            seen.append(h)
    return seen


def _choose_ics_url(cfg: Config, page: Any) -> str:
    explicit = cfg.explicit_ics_url()
    if explicit:
        return explicit
    links = _discover_ics_links(cfg, page)
    if not links:
        _dump_debug(cfg, page, "no-ics-link")
        raise PortalUnavailableError(
            "No se encontró ningún enlace .ics en el portal "
            f"(selector {cfg.ics_link_selector!r}). Fija UC3M_ICS_URL o "
            "UC3M_EXP/UC3M_PER, o ajusta UC3M_ICS_LINK_SELECTOR (usa --discover)."
        )
    log.info("Enlaces .ics encontrados: %d", len(links))
    for link in links:
        log.debug("  · %s", link)
    if cfg.per:
        for link in links:
            if parse_qs(urlparse(link).query).get("per", [None])[0] == cfg.per:
                return link
        log.warning("Ningún enlace coincide con UC3M_PER=%s; se usa el primero.", cfg.per)
    return links[0]


def _launch(p: Any, cfg: Config) -> Any:
    args = ["--disable-dev-shm-usage", *cfg.chromium_args]
    kwargs: dict[str, Any] = {"headless": True, "args": args}
    if cfg.chromium_executable:
        kwargs["executable_path"] = cfg.chromium_executable
    return p.chromium.launch(**kwargs)


def fetch_ics(cfg: Config, *, discover: bool = False) -> Path:
    """Autentica en la UC3M y guarda el .ics en ``cfg.temp_path``.

    Estrategia: (1) abrir el portal → redirección al SSO → login; (2) ya autenticados,
    resolver la URL del .ics (configurada o descubierta en la página); (3) pedirla con
    ``context.request`` — comparte cookies con el navegador y evita depender del
    diálogo de descargas de Chromium.
    """
    cfg.require_credentials()
    from playwright.sync_api import Error as PWError  # import perezoso (healthcheck ligero)
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    portal_host = _host(cfg.portal_url)
    with sync_playwright() as p:
        browser = _launch(p, cfg)
        context = browser.new_context(
            user_agent=cfg.user_agent,
            locale="es-ES",
            timezone_id=cfg.calendar_tz,
            accept_downloads=True,
            ignore_https_errors=False,
        )
        context.set_default_timeout(cfg.nav_timeout_ms)
        page = context.new_page()
        network: list[str] = []
        tracing = False
        if discover:
            # Solo método, URL, estado y tipo: nunca cuerpos ni cabeceras.
            page.on(
                "response",
                lambda r: network.append(
                    f"{r.status} {r.request.method} {r.url} "
                    f"[{r.headers.get('content-type', '')}]"
                ),
            )
        try:
            log.info("Abriendo portal %s", cfg.portal_url)
            try:
                page.goto(cfg.portal_url, wait_until="domcontentloaded")
            except PWTimeout as exc:
                raise PortalUnavailableError(f"Timeout abriendo el portal: {exc}") from exc
            except PWError as exc:
                raise PortalUnavailableError(f"Error de red abriendo el portal: {exc}") from exc

            _login(cfg, page, portal_host)

            if discover:
                # La traza se empieza DESPUÉS del login: una traza de Playwright guarda
                # el tráfico (incluido el POST del formulario) y los valores escritos,
                # así que grabarla durante el login dejaría la contraseña en el .zip.
                context.tracing.start(screenshots=True, snapshots=True)
                tracing = True
                out = _dump_debug(cfg, page, "discover-portal")
                links = _discover_ics_links(cfg, page)
                all_links = page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => `${e.innerText.trim()} -> ${e.href}`)"
                )
                if out:
                    (out / "ics_links.txt").write_text("\n".join(links) + "\n", "utf-8")
                    (out / "all_links.txt").write_text("\n".join(all_links) + "\n", "utf-8")
                    (out / "network.txt").write_text("\n".join(network) + "\n", "utf-8")
                log.info("Enlaces .ics detectados: %s", links or "ninguno")

            url = _choose_ics_url(cfg, page)
            log.info("Descargando calendario: %s", url)
            try:
                resp = context.request.get(url, timeout=cfg.nav_timeout_ms, max_redirects=10)
            except PWError as exc:
                raise PortalUnavailableError(f"Fallo descargando el .ics: {exc}") from exc

            if resp.status >= 500:
                raise PortalUnavailableError(f"El portal devolvió HTTP {resp.status}")
            if resp.status in (401, 403):
                raise PortalUnavailableError(
                    f"HTTP {resp.status} al pedir el .ics: la sesión no es válida."
                )
            if resp.status != 200:
                raise InvalidCalendarError(f"HTTP {resp.status} al pedir el .ics")
            if _host(resp.url) != portal_host and _host(resp.url) != _host(url):
                raise PortalUnavailableError(
                    f"La descarga redirigió a {_host(resp.url)} (¿sesión caducada?)."
                )

            body = resp.body()
            ctype = resp.headers.get("content-type", "")
            log.info("Recibidos %d bytes (Content-Type: %s)", len(body), ctype or "?")

            cfg.temp_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.temp_path.write_bytes(body)
            return cfg.temp_path
        except SyncError:
            raise
        except PWTimeout as exc:
            _dump_debug(cfg, page, "timeout")
            raise PortalUnavailableError(f"Timeout en el portal: {exc}") from exc
        except PWError as exc:
            _dump_debug(cfg, page, "playwright-error")
            raise PortalUnavailableError(f"Error del navegador: {exc}") from exc
        finally:
            if tracing:
                trace = cfg.debug_dir / f"trace-{datetime.now():%Y%m%d-%H%M%S}.zip"
                try:
                    context.tracing.stop(path=str(trace))
                    log.info(
                        "Traza Playwright: %s (ábrela con `playwright show-trace`). "
                        "Contiene las cookies de la sesión UC3M: no la compartas.",
                        trace,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("No se pudo guardar la traza: %s", exc)
            context.close()
            browser.close()


# --------------------------------------------------------------------------------------
# 2) Validación
# --------------------------------------------------------------------------------------


def decode_ics(raw: bytes) -> str:
    """Decodifica tolerando BOM y ficheros en Windows-1252 (habitual en exportadores
    antiguos); el feed publicado siempre será UTF-8."""
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise InvalidCalendarError("Codificación de texto desconocida")


def validate_ics(
    raw: bytes, *, min_events: int = 1, max_bytes: int = 10 * 1024 * 1024
) -> Calendar:
    """Comprueba que ``raw`` es un iCalendar utilizable y devuelve el objeto parseado.

    Lanza ``InvalidCalendarError`` si está vacío, es HTML (típico de una página de
    login o de error servida con 200), está truncado o no tiene eventos suficientes.
    """
    if not raw or not raw.strip():
        raise InvalidCalendarError("El fichero descargado está vacío")
    if len(raw) > max_bytes:
        raise InvalidCalendarError(f"Tamaño anómalo: {len(raw)} bytes")

    text = decode_ics(raw)
    head = text.lstrip()[:512].lower()
    if head.startswith("<") or "<html" in head or "<!doctype" in head:
        raise InvalidCalendarError("Se ha recibido HTML en lugar de un calendario")
    if not text.lstrip().upper().startswith("BEGIN:VCALENDAR"):
        raise InvalidCalendarError("No empieza por BEGIN:VCALENDAR")
    if "END:VCALENDAR" not in text.rstrip()[-64:].upper():
        raise InvalidCalendarError("Falta END:VCALENDAR (¿descarga truncada?)")

    try:
        cal = Calendar.from_ical(text)
    except Exception as exc:  # noqa: BLE001 — icalendar lanza ValueError/KeyError/...
        raise InvalidCalendarError(f"iCalendar mal formado: {exc}") from exc

    events = cal.walk("VEVENT")
    if len(events) < min_events:
        raise InvalidCalendarError(
            f"El calendario solo tiene {len(events)} eventos (mínimo {min_events})"
        )
    for ev in events:
        if ev.get("DTSTART") is None:
            raise InvalidCalendarError(f"Evento sin DTSTART: {ev.get('SUMMARY')!r}")
    return cal


def check_shrink(new_count: int, old_count: int | None, min_ratio: float) -> None:
    """Protege frente a exportaciones parciales (p. ej. el portal devuelve solo una
    semana o un cuatrimestre vacío por error)."""
    if old_count and old_count > 0 and new_count < old_count * min_ratio:
        raise SuspiciousChangeError(
            f"El nuevo horario tiene {new_count} eventos frente a {old_count} publicados "
            f"(< {min_ratio:.0%}). Se mantiene la versión anterior. Si es un cambio "
            "legítimo (fin de cuatrimestre), ejecuta una vez con ALLOW_SHRINK=true."
        )


# --------------------------------------------------------------------------------------
# 3) Enriquecimiento para Apple Calendar
# --------------------------------------------------------------------------------------


def _set_prop(cal: Calendar, name: str, value: Any) -> None:
    if name in cal:
        del cal[name]
    cal.add(name, value)


def _ensure_aware(ev: Any, prop: str, tz: ZoneInfo) -> bool:
    """Convierte horas "flotantes" (sin zona) a Europe/Madrid. Sin esto, un iPhone
    en otro huso mostraría las clases desplazadas."""
    value = ev.get(prop)
    if value is None:
        return False
    dt = value.dt
    if isinstance(dt, datetime) and dt.tzinfo is None:
        del ev[prop]
        ev.add(prop, dt.replace(tzinfo=tz))
        return True
    return False


def enrich_ics(cal: Calendar, cfg: Config) -> Calendar:
    tz = ZoneInfo(cfg.calendar_tz)

    _set_prop(cal, "X-WR-CALNAME", cfg.calendar_name)
    _set_prop(cal, "NAME", cfg.calendar_name)
    _set_prop(cal, "X-WR-TIMEZONE", cfg.calendar_tz)
    _set_prop(cal, "X-APPLE-CALENDAR-COLOR", "#1B4F9C")
    # RFC 7986 + extensión de Microsoft: sugerencia de frecuencia de refresco.
    refresh = vDuration(cfg.refresh_interval)
    refresh.params["VALUE"] = "DURATION"
    _set_prop(cal, "REFRESH-INTERVAL", refresh)
    _set_prop(cal, "X-PUBLISHED-TTL", vDuration(cfg.refresh_interval))
    if "PRODID" not in cal:
        cal.add("PRODID", "-//uc3m-ical-sync//ES")
    if "VERSION" not in cal:
        cal.add("VERSION", "2.0")

    fixed_tz = 0
    for ev in cal.walk("VEVENT"):
        if cfg.force_timezone:
            fixed_tz += _ensure_aware(ev, "DTSTART", tz)
            fixed_tz += _ensure_aware(ev, "DTEND", tz)
        if not ev.get("UID"):
            ev.add("UID", f"{_event_key_hash(ev)}@uc3m-ical-sync")
        if not ev.get("DTSTAMP"):
            ev.add("DTSTAMP", _now())

    if fixed_tz:
        log.info("Asignada zona %s a %d fechas sin huso", cfg.calendar_tz, fixed_tz)
    # Añade los VTIMEZONE que falten (icalendar >= 6.1)
    add_tz = getattr(cal, "add_missing_timezones", None)
    if callable(add_tz):
        try:
            add_tz()
        except Exception as exc:  # noqa: BLE001
            log.debug("add_missing_timezones falló: %s", exc)
    return cal


# --------------------------------------------------------------------------------------
# Detección de cambios
# --------------------------------------------------------------------------------------

_VOLATILE = re.compile(r"^(DTSTAMP|LAST-MODIFIED|CREATED|SEQUENCE)[:;].*$", re.M)


def content_hash(ics_bytes: bytes) -> str:
    """Hash estable ignorando marcas de tiempo que el portal regenera en cada descarga.
    Evita reescribir el fichero (y cambiar ETag/Last-Modified) si nada cambió."""
    text = ics_bytes.decode("utf-8", "replace").replace("\r\n", "\n")
    text = re.sub(r"\n[ \t]", "", text)  # desplegar líneas (RFC 5545 §3.1)
    return hashlib.sha256(_VOLATILE.sub("", text).encode()).hexdigest()


def _fmt_dt(v: Any) -> str:
    dt = getattr(v, "dt", v)
    if isinstance(dt, datetime):
        return dt.strftime("%a %d/%m %H:%M")
    if isinstance(dt, date):
        return dt.strftime("%a %d/%m")
    return str(dt)


def _event_key_hash(ev: Any) -> str:
    raw = f"{ev.get('SUMMARY')}|{_fmt_dt(ev.get('DTSTART'))}|{_fmt_dt(ev.get('DTEND'))}"
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]


def summarize_events(cal: Calendar) -> dict[str, str]:
    """{clave: descripción legible} — clave = asignatura + inicio + aula."""
    out: dict[str, str] = {}
    for ev in cal.walk("VEVENT"):
        summary = str(ev.get("SUMMARY", "")).strip()
        where = str(ev.get("LOCATION", "")).strip()
        start = _fmt_dt(ev.get("DTSTART"))
        out[f"{summary}|{start}|{where}"] = f"{start} · {summary}" + (
            f" · {where}" if where else ""
        )
    return out


def diff_events(old: dict[str, str], new: dict[str, str]) -> tuple[list[str], list[str]]:
    added = [new[k] for k in new.keys() - old.keys()]
    removed = [old[k] for k in old.keys() - new.keys()]
    return sorted(added), sorted(removed)


# --------------------------------------------------------------------------------------
# 4) Publicación atómica
# --------------------------------------------------------------------------------------


def publish_atomic(data: bytes, dest: Path) -> None:
    """Escribe ``data`` en ``dest`` de forma atómica.

    ``os.replace`` solo es atómico dentro del mismo sistema de ficheros: /tmp y el
    volumen publicado suelen ser distintos (fallaría con EXDEV), así que el fichero
    intermedio se crea en el MISMO directorio que el destino, se hace fsync y después
    rename. Un lector concurrente ve siempre la versión vieja o la nueva completa.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        dir_fd = os.open(dest.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SyncError(f"No se pudo publicar {dest}: {exc}") from exc


def archive_copy(cfg: Config, data: bytes) -> None:
    try:
        cfg.archive_dir.mkdir(parents=True, exist_ok=True)
        (cfg.archive_dir / f"horario_{datetime.now():%Y%m%d-%H%M%S}.ics").write_bytes(data)
        _prune_dir(cfg.archive_dir, cfg.archive_keep)
    except OSError as exc:
        log.warning("No se pudo archivar la versión: %s", exc)


# --------------------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------------------


@dataclass
class SyncResult:
    changed: bool
    events: int
    added: list[str]
    removed: list[str]


def process_and_publish(cfg: Config, raw: bytes, state: dict[str, Any]) -> SyncResult:
    """Valida, enriquece y publica. Separado de la descarga para poder testearlo."""
    cal = validate_ics(raw, min_events=cfg.min_events, max_bytes=cfg.max_bytes)
    n_events = len(cal.walk("VEVENT"))
    if not cfg.allow_shrink:
        check_shrink(n_events, state.get("event_count"), cfg.min_ratio)

    new_summary = summarize_events(cal)
    cal = enrich_ics(cal, cfg)
    out = cal.to_ical()  # UTF-8, CRLF, líneas plegadas a 75 octetos

    new_hash = content_hash(out)
    if new_hash == state.get("content_hash") and cfg.publish_path.exists():
        log.info("Sin cambios en el horario (%d eventos); no se reescribe.", n_events)
        return SyncResult(False, n_events, [], [])

    old_summary: dict[str, str] = state.get("events_summary") or {}
    added, removed = diff_events(old_summary, new_summary) if old_summary else ([], [])

    publish_atomic(out, cfg.publish_path)
    archive_copy(cfg, out)
    state.update(
        content_hash=new_hash,
        event_count=n_events,
        events_summary=new_summary,
        last_change=_now().isoformat(),
    )
    log.info(
        "Publicado %s (%d eventos, %d bytes; +%d/-%d)",
        cfg.publish_path, n_events, len(out), len(added), len(removed),
    )
    return SyncResult(True, n_events, added, removed)


def _change_message(cfg: Config, res: SyncResult) -> str | None:
    if not (res.added or res.removed):
        return None
    lines = [f"📅 {cfg.calendar_name}: el horario ha cambiado"]
    for label, items in (("➕ Nuevos/modificados", res.added), ("➖ Eliminados", res.removed)):
        if items:
            lines.append(f"{label} ({len(items)}):")
            lines += [f"  • {x}" for x in items[:8]]
            if len(items) > 8:
                lines.append(f"  … y {len(items) - 8} más")
    return "\n".join(lines)


def run_sync(
    cfg: Config,
    notifier: Notifier,
    *,
    fetcher: Callable[[Config], Path] = fetch_ics,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Una sincronización completa con reintentos. Devuelve True si terminó bien."""
    state = load_state(cfg)
    state["last_attempt"] = _now().isoformat()
    ping_healthcheck(cfg, "/start")

    last_exc: SyncError | None = None
    result: SyncResult | None = None
    for attempt in range(1, cfg.max_attempts + 1):
        try:
            log.info("── Sincronización: intento %d/%d", attempt, cfg.max_attempts)
            tmp = fetcher(cfg)
            raw = tmp.read_bytes()
            result = process_and_publish(cfg, raw, state)
            break
        except SyncError as exc:
            last_exc = exc
            log.error("Intento %d fallido [%s]: %s", attempt, type(exc).__name__, exc)
            if not exc.retryable or attempt == cfg.max_attempts:
                break
            delay = cfg.backoff_base_s * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.2)
            log.info("Reintentando en %.0f s", delay)
            sleep(delay)
        except Exception as exc:  # noqa: BLE001 — bug inesperado: lo tratamos como fallo
            log.exception("Error inesperado en el intento %d", attempt)
            last_exc = SyncError(f"{type(exc).__name__}: {exc}")
            if attempt == cfg.max_attempts:
                break
            sleep(cfg.backoff_base_s)
        finally:
            try:
                cfg.temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    if result is None:
        _handle_failure(cfg, notifier, state, last_exc or SyncError("Fallo desconocido"))
        save_state(cfg, state)
        ping_healthcheck(cfg, "/fail")
        return False

    was_failing = state.get("consecutive_failures", 0) > 0
    state.update(
        last_success=_now().isoformat(),
        consecutive_failures=0,
        last_error=None,
        last_error_type=None,
    )
    save_state(cfg, state)
    ping_healthcheck(cfg)

    if was_failing and cfg.notify_on_recovery:
        notifier.send(f"✅ {cfg.calendar_name}: la sincronización vuelve a funcionar.")
    if result.changed and cfg.notify_on_change:
        msg = _change_message(cfg, result)
        if msg:
            notifier.send(msg)
    return True


def _handle_failure(
    cfg: Config, notifier: Notifier, state: dict[str, Any], exc: SyncError
) -> None:
    failures = state.get("consecutive_failures", 0) + 1
    state.update(
        consecutive_failures=failures,
        last_error=str(exc),
        last_error_type=type(exc).__name__,
        last_failure=_now().isoformat(),
    )
    last_ok = state.get("last_success")
    age = ""
    if last_ok:
        hours = (_now() - datetime.fromisoformat(last_ok)).total_seconds() / 3600
        age = f"\nÚltima sincronización correcta: hace {hours:.0f} h."
    advice = {
        "auth": "Actualiza UC3M_PASS en .env y ejecuta `docker compose up -d worker`.",
        "mfa": "El login automático no es viable con MFA; revisa las opciones de tu cuenta.",
        "portal": "Se reintentará en la próxima ejecución. Si persiste, usa --discover.",
        "shrink": "Revisa el portal; si es legítimo ejecuta una vez con ALLOW_SHRINK=true.",
    }.get(exc.alert_key, "Revisa `docker compose logs worker`.")
    notifier.send(
        f"⚠️ {cfg.calendar_name}: {exc.title}\n"
        f"{exc}\n"
        f"Fallos consecutivos: {failures}.{age}\n"
        f"El feed sigue sirviendo la última versión válida.\n→ {advice}"
    )


# --------------------------------------------------------------------------------------
# Healthcheck / planificador / CLI
# --------------------------------------------------------------------------------------


def healthcheck(cfg: Config) -> int:
    """0 = sano. Sano si hubo éxito en las últimas STALE_HOURS horas, o si el
    contenedor acaba de arrancar y todavía no ha terminado la primera ejecución."""
    state = load_state(cfg)
    last_ok = state.get("last_success")
    if last_ok:
        age_h = (_now() - datetime.fromisoformat(last_ok)).total_seconds() / 3600
        if age_h <= cfg.stale_hours:
            return 0
        print(f"UNHEALTHY: última sincronización correcta hace {age_h:.1f} h")
        return 1
    if state.get("consecutive_failures"):
        print(f"UNHEALTHY: {state.get('last_error_type')}: {state.get('last_error')}")
        return 1
    print("STARTING: aún no hay ninguna sincronización completada")
    return 0


def run_scheduler(cfg: Config, notifier: Notifier) -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    tz = ZoneInfo(cfg.calendar_tz)
    sched = BlockingScheduler(timezone=tz)
    trigger = CronTrigger.from_crontab(cfg.cron, timezone=tz)
    sched.add_job(
        lambda: run_sync(cfg, notifier),
        trigger,
        id="sync_uc3m",
        jitter=cfg.jitter_s or None,  # no "clavar" el SSO siempre al mismo segundo
        misfire_grace_time=3 * 3600,  # si el host estaba apagado a las 04:00, recupera
        coalesce=True,
        max_instances=1,
    )

    def _stop(signum: int, _frame: Any) -> None:
        log.info("Señal %s recibida; parando planificador.", signal.Signals(signum).name)
        sched.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if cfg.run_on_start:
        log.info("RUN_ON_START=true → sincronización inicial")
        run_sync(cfg, notifier)

    log.info("Planificador activo: '%s' (%s).", cfg.cron, cfg.calendar_tz)
    sched.start()  # bloquea hasta SIGTERM


def _warn_weak_feed_token() -> None:
    """El token de la URL es lo único que protege el feed. Si es corto, se puede
    adivinar; si no es hexadecimal, el filtro de logs de Caddy no lo enmascara."""
    token = _env("FEED_TOKEN")
    if token is None:
        return  # el worker no lo necesita; solo lo comprueba si está en el .env
    if len(token) < 32 or not re.fullmatch(r"[0-9a-fA-F]+", token):
        log.warning(
            "FEED_TOKEN débil o no hexadecimal (%d caracteres). Genera uno con "
            "`openssl rand -hex 24` y vuelve a suscribirte con la nueva URL.",
            len(token),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sincroniza el horario UC3M a un feed .ics")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="una ejecución y salir")
    mode.add_argument("--discover", action="store_true", help="login + volcado diagnóstico")
    mode.add_argument("--validate", metavar="FICHERO", help="valida un .ics local")
    mode.add_argument("--healthcheck", action="store_true", help="estado para Docker")
    mode.add_argument("--test-alert", action="store_true", help="envía alerta de prueba")
    args = parser.parse_args(argv)

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 2

    if args.healthcheck:
        return healthcheck(cfg)

    setup_logging(cfg)
    notifier = Notifier(cfg)
    _warn_weak_feed_token()

    if args.validate:
        try:
            cal = validate_ics(Path(args.validate).read_bytes(), min_events=cfg.min_events)
        except (OSError, SyncError) as exc:
            log.error("NO válido: %s", exc)
            return 1
        events = summarize_events(cal)
        log.info("Válido: %d eventos. Primeros:", len(events))
        for line in sorted(events.values())[:10]:
            log.info("  %s", line)
        return 0

    if args.test_alert:
        ok = notifier.send(f"🔔 {cfg.calendar_name}: alerta de prueba desde uc3m-ical-sync.")
        return 0 if ok else 1

    try:
        cfg.require_credentials()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.discover:
        try:
            path = fetch_ics(cfg, discover=True)
            cal = validate_ics(path.read_bytes(), min_events=cfg.min_events)
            log.info("Discover OK: %d eventos descargados en %s", len(cal.walk("VEVENT")), path)
            return 0
        except SyncError as exc:
            log.error("Discover falló [%s]: %s", type(exc).__name__, exc)
            log.info("Revisa %s para ver capturas, HTML y red.", cfg.debug_dir)
            return 1

    if args.once:
        return 0 if run_sync(cfg, notifier) else 1

    run_scheduler(cfg, notifier)
    return 0


if __name__ == "__main__":
    sys.exit(main())
