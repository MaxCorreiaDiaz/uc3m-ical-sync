"""Tests del worker. Ejecutar: pytest -q  (desde worker/)

Los tests e2e levantan un portal + SSO simulados en localhost que imitan el flujo
adAS de la UC3M y necesitan un Chromium: se saltan si Playwright no puede lanzarlo
(exporta CHROMIUM_EXECUTABLE para usar un binario concreto).
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

import sync_uc3m as s

ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//UC3M//Horarios//ES\r\n"
    "BEGIN:VEVENT\r\nUID:1@uc3m\r\nDTSTAMP:20260901T000000Z\r\n"
    "DTSTART:20260914T090000\r\nDTEND:20260914T103000\r\n"
    "SUMMARY:Sistemas Distribuidos\r\nLOCATION:Aula 2.1.A03\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:2@uc3m\r\nDTSTAMP:20260901T000000Z\r\n"
    "DTSTART:20260915T110000\r\nDTEND:20260915T123000\r\n"
    "SUMMARY:Redes de Ordenadores\r\nLOCATION:Aula 4.0.E02\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("UC3M_USER", "100000000")
    monkeypatch.setenv("UC3M_PASS", "secreto-123")
    monkeypatch.setenv("TEMP_PATH", str(tmp_path / "tmp" / "horario_temp.ics"))
    monkeypatch.setenv("PUBLISH_PATH", str(tmp_path / "www" / "horario_uc3m.ics"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "data" / "logs"))
    monkeypatch.setenv("SYNC_BACKOFF_SECONDS", "0")
    for var in ("TELEGRAM_BOT_TOKEN", "DISCORD_WEBHOOK_URL", "HEALTHCHECK_PING_URL"):
        monkeypatch.delenv(var, raising=False)
    return s.Config.from_env()


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


# ─── validación ──────────────────────────────────────────────────────────────


def test_valid_ics():
    cal = s.validate_ics(ICS.encode())
    assert len(cal.walk("VEVENT")) == 2


@pytest.mark.parametrize(
    "raw, msg",
    [
        (b"", "vac"),
        (b"   \n", "vac"),
        (b"<!DOCTYPE html><html><body>Login</body></html>", "HTML"),
        (b"\n<html><head></head></html>", "HTML"),
        (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n", "END:VCALENDAR"),
        (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", "0 eventos"),
        (b"hola", "BEGIN:VCALENDAR"),
    ],
)
def test_invalid_ics(raw, msg):
    with pytest.raises(s.InvalidCalendarError, match=msg):
        s.validate_ics(raw)


def test_cp1252_is_accepted():
    raw = ICS.replace("Redes", "Programación").encode("cp1252")
    cal = s.validate_ics(raw)
    assert any("Programación" in str(e["SUMMARY"]) for e in cal.walk("VEVENT"))


def test_shrink_guard():
    s.check_shrink(10, 50, 0.2)  # 20 % exacto: permitido
    with pytest.raises(s.SuspiciousChangeError):
        s.check_shrink(9, 50, 0.2)
    s.check_shrink(1, None, 0.2)  # primera ejecución


# ─── enriquecimiento / hash ──────────────────────────────────────────────────


def test_enrich_adds_apple_props_and_timezone(cfg):
    cal = s.enrich_ics(s.validate_ics(ICS.encode()), cfg)
    out = cal.to_ical().decode()
    assert "X-WR-CALNAME:Horario UC3M" in out
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT6H" in out
    assert "X-PUBLISHED-TTL:PT6H" in out
    assert "DTSTART;TZID=Europe/Madrid:20260914T090000" in out
    assert "BEGIN:VTIMEZONE" in out
    s.validate_ics(out.encode())  # sigue siendo válido


def test_hash_ignores_dtstamp():
    a = ICS.encode()
    b = ICS.replace("DTSTAMP:20260901T000000Z", "DTSTAMP:20261231T235959Z").encode()
    c = ICS.replace("Aula 2.1.A03", "Aula 7.0.J01").encode()
    assert s.content_hash(a) == s.content_hash(b)
    assert s.content_hash(a) != s.content_hash(c)


# ─── publicación atómica ─────────────────────────────────────────────────────


def test_publish_atomic_replaces_and_leaves_no_tmp(tmp_path):
    dest = tmp_path / "pub" / "horario_uc3m.ics"
    s.publish_atomic(b"v1", dest)
    s.publish_atomic(b"v2", dest)
    assert dest.read_bytes() == b"v2"
    assert [p.name for p in dest.parent.iterdir()] == ["horario_uc3m.ics"]
    assert oct(dest.stat().st_mode & 0o777) == "0o644"


# ─── orquestación con fetcher simulado ───────────────────────────────────────


def _fetcher_returning(content: bytes):
    def fetch(cfg):
        cfg.temp_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.temp_path.write_bytes(content)
        return cfg.temp_path

    return fetch


def test_run_sync_publishes_then_detects_no_change(cfg):
    n = FakeNotifier()
    assert s.run_sync(cfg, n, fetcher=_fetcher_returning(ICS.encode()), sleep=lambda _: None)
    first = cfg.publish_path.stat().st_mtime_ns
    assert not cfg.temp_path.exists(), "el temporal debe limpiarse"
    # Segunda descarga idéntica salvo DTSTAMP → no se reescribe
    again = ICS.replace("20260901T000000Z", "20260902T000000Z").encode()
    assert s.run_sync(cfg, n, fetcher=_fetcher_returning(again), sleep=lambda _: None)
    assert cfg.publish_path.stat().st_mtime_ns == first
    assert n.messages == []  # primera publicación y sin cambios: nada que avisar


def test_run_sync_notifies_room_change(cfg):
    n = FakeNotifier()
    s.run_sync(cfg, n, fetcher=_fetcher_returning(ICS.encode()), sleep=lambda _: None)
    moved = ICS.replace("Aula 2.1.A03", "Aula 7.0.J01").encode()
    s.run_sync(cfg, n, fetcher=_fetcher_returning(moved), sleep=lambda _: None)
    assert len(n.messages) == 1 and "7.0.J01" in n.messages[0] and "2.1.A03" in n.messages[0]


def test_html_error_keeps_previous_version_and_alerts(cfg):
    n = FakeNotifier()
    s.run_sync(cfg, n, fetcher=_fetcher_returning(ICS.encode()), sleep=lambda _: None)
    good = cfg.publish_path.read_bytes()
    ok = s.run_sync(
        cfg, n, fetcher=_fetcher_returning(b"<html>Mantenimiento</html>"), sleep=lambda _: None
    )
    assert not ok
    assert cfg.publish_path.read_bytes() == good
    assert "no es un calendario" in n.messages[-1]
    state = s.load_state(cfg)
    assert state["consecutive_failures"] == 1
    # recuperación
    s.run_sync(cfg, n, fetcher=_fetcher_returning(ICS.encode()), sleep=lambda _: None)
    assert "vuelve a funcionar" in n.messages[-1]


def test_auth_error_is_not_retried(cfg):
    calls = []

    def fetch(_cfg):
        calls.append(1)
        raise s.AuthenticationError("credenciales rechazadas")

    n = FakeNotifier()
    assert not s.run_sync(cfg, n, fetcher=fetch, sleep=lambda _: None)
    assert len(calls) == 1
    assert "Login rechazado" in n.messages[0]


def test_portal_error_is_retried(cfg):
    calls = []

    def flaky(c):
        calls.append(1)
        if len(calls) < 3:
            raise s.PortalUnavailableError("503")
        return _fetcher_returning(ICS.encode())(c)

    assert s.run_sync(cfg, FakeNotifier(), fetcher=flaky, sleep=lambda _: None)
    assert len(calls) == 3


def test_healthcheck(cfg):
    assert s.healthcheck(cfg) == 0  # arrancando
    s.run_sync(cfg, FakeNotifier(), fetcher=_fetcher_returning(b""), sleep=lambda _: None)
    assert s.healthcheck(cfg) == 1  # fallando
    s.run_sync(cfg, FakeNotifier(), fetcher=_fetcher_returning(ICS.encode()), sleep=lambda _: None)
    assert s.healthcheck(cfg) == 0


def test_password_is_redacted_from_logs(cfg, capsys):
    s.setup_logging(cfg)
    s.log.error("fallo con %s", cfg.password)
    assert cfg.password not in capsys.readouterr().out


# ─── e2e: portal + SSO simulados ─────────────────────────────────────────────


def _serve(handler_cls) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def fake_uc3m():
    """Portal en 127.0.0.1:P1 y SSO en localhost:P2 (hosts distintos, como en la
    realidad: aplicaciones.uc3m.es vs. el servidor adAS)."""
    ports: dict[str, int] = {}

    class Portal(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _authed(self):
            return "session=ok" in (self.headers.get("Cookie") or "")

        def _to_sso(self):
            self.send_response(302)
            svc = f"http://127.0.0.1:{ports['portal']}{self.path}"
            self.send_header("Location", f"http://localhost:{ports['sso']}/login?service={svc}")
            self.end_headers()

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/auth":  # ticket del SSO → cookie de sesión
                self.send_response(302)
                self.send_header("Set-Cookie", "session=ok; Path=/; HttpOnly")
                self.send_header("Location", parse_qs(u.query)["next"][0])
                self.end_headers()
            elif not self._authed():
                self._to_sso()
            elif u.path.endswith("alumno.page"):
                body = (
                    "<html><body><h1>Mis horarios</h1>"
                    '<a href="verHorario.page?exp=999&per=1&fmt=ics">1º cuatri (ics)</a>'
                    '<a href="verHorario.page?exp=999&per=2&fmt=ics">2º cuatri (ics)</a>'
                    "</body></html>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
            elif u.path.endswith("verHorario.page"):
                per = parse_qs(u.query).get("per", ["?"])[0]
                body = ICS.replace("Sistemas", f"[per{per}] Sistemas").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/calendar")
                self.send_header("Content-Disposition", 'attachment; filename="h.ics"')
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    class SSO(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _form(self, service, error=""):
            html = (
                f"<html><body>{error}<form method='post' action='/login?service={service}'>"
                "<input name='adAS_username'><input type='password' name='adAS_password'>"
                "<input type='submit' id='submit_ok' value='Entrar'></form></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        def do_GET(self):
            self._form(parse_qs(urlparse(self.path).query)["service"][0])

        def do_POST(self):
            service = parse_qs(urlparse(self.path).query)["service"][0]
            n = int(self.headers["Content-Length"])
            data = parse_qs(self.rfile.read(n).decode())
            pwd = data.get("adAS_password", [""])[0]
            if pwd == "secreto-123":
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{ports['portal']}/auth?next={service}"
                )
                self.end_headers()
            elif pwd == "pide-mfa":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    b"<html><body><p>Introduce el codigo de verificacion</p>"
                    b"<input autocomplete='one-time-code' name='code'></body></html>"
                )
            else:
                self._form(service, "<div class='error'>Usuario o clave incorrectos</div>")

    portal, sso = _serve(Portal), _serve(SSO)
    ports["portal"], ports["sso"] = portal.server_port, sso.server_port
    yield f"http://127.0.0.1:{ports['portal']}/horarios-web/alumno/alumno.page"
    portal.shutdown()
    sso.shutdown()


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            kw = {}
            if os.environ.get("CHROMIUM_EXECUTABLE"):
                kw["executable_path"] = os.environ["CHROMIUM_EXECUTABLE"]
            p.chromium.launch(args=os.environ.get("CHROMIUM_ARGS", "").split(), **kw).close()
        return True
    except Exception:
        return False


# Con REQUIRE_E2E=1 (CI) no se saltan: si no hay Chromium, fallan.
e2e = pytest.mark.skipif(
    not os.environ.get("REQUIRE_E2E") and not _chromium_available(),
    reason="Chromium no disponible",
)


def _e2e_cfg(monkeypatch, portal_url, **env):
    monkeypatch.setenv("UC3M_PORTAL_URL", portal_url)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return s.Config.from_env()


@e2e
def test_e2e_login_discovers_link_and_downloads(cfg, fake_uc3m, monkeypatch):
    c = _e2e_cfg(monkeypatch, fake_uc3m, UC3M_PER="2")
    path = s.fetch_ics(c)
    body = path.read_bytes().decode()
    assert "[per2] Sistemas" in body
    assert s.run_sync(c, FakeNotifier(), sleep=lambda _: None)
    assert "X-WR-CALNAME" in c.publish_path.read_text()


@e2e
def test_e2e_wrong_password(cfg, fake_uc3m, monkeypatch):
    c = _e2e_cfg(monkeypatch, fake_uc3m, UC3M_PASS="mala", LOGIN_TIMEOUT_MS="4000")
    with pytest.raises(s.AuthenticationError, match="incorrectos"):
        s.fetch_ics(c)


@e2e
def test_e2e_mfa_detected(cfg, fake_uc3m, monkeypatch):
    c = _e2e_cfg(monkeypatch, fake_uc3m, UC3M_PASS="pide-mfa", LOGIN_TIMEOUT_MS="4000")
    with pytest.raises(s.MFARequiredError):
        s.fetch_ics(c)
    assert any(Path(c.debug_dir).iterdir()), "debe guardar captura de diagnóstico"


@e2e
def test_e2e_discover_artifacts_never_contain_password(cfg, fake_uc3m, monkeypatch):
    """Los ficheros de diagnóstico (capturas, HTML, red, traza) no deben incluir
    la contraseña en ningún formato (texto plano, URL-encoded, base64...)."""
    import base64
    import urllib.parse
    import zipfile

    c = _e2e_cfg(monkeypatch, fake_uc3m)
    s.fetch_ics(c, discover=True)
    pwd = c.password.encode()
    needles = {pwd, urllib.parse.quote_plus(c.password).encode(), base64.b64encode(pwd)}
    blobs: list[tuple[str, bytes]] = []
    for f in Path(c.debug_dir).rglob("*"):
        if f.is_file():
            if f.suffix == ".zip":
                with zipfile.ZipFile(f) as z:
                    blobs += [(f"{f.name}:{n}", z.read(n)) for n in z.namelist()]
            else:
                blobs.append((str(f), f.read_bytes()))
    assert blobs, "discover debe generar ficheros"
    leaks = [name for name, data in blobs if any(n in data for n in needles)]
    assert not leaks, f"contraseña filtrada en: {leaks}"


def test_password_is_redacted_from_exception_tracebacks(cfg, capsys):
    s.setup_logging(cfg)
    try:
        raise RuntimeError(f"fallo raro con {cfg.password}")
    except RuntimeError:
        s.log.exception("Error inesperado")
    assert cfg.password not in capsys.readouterr().out


@pytest.mark.parametrize(
    "token, weak", [("abc", True), ("x" * 40, True), ("0123456789abcdef" * 3, False)]
)
def test_weak_feed_token_warning(cfg, capsys, monkeypatch, token, weak):
    monkeypatch.setenv("FEED_TOKEN", token)
    s.setup_logging(cfg)
    s._warn_weak_feed_token()
    out = capsys.readouterr().out
    assert ("FEED_TOKEN débil" in out) is weak
    if not weak:
        assert token not in out
