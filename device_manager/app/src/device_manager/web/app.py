"""FastAPI-App: HTML-Oberfläche (Jinja2 + htmx) und kleine JSON-API. Ingress-fähig über <base href>."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..context import Context
from ..jobs import Job, JobManager, Progress
from ..opnsense.client import OPNsenseError
from ..services import adopt as adopt_svc
from ..services import certificate, defaults, firmware, identity, inventory, provision, reservation, zigbee
from ..services.common import ServiceError
from ..shelly.client import ShellyError
from ..steps import describe_error
from ..store import Store
from ..z2m.bus import PahoBus
from ..z2m.client import Z2MError
from . import view

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
USER_ERRORS = (ServiceError, ShellyError, OPNsenseError, Z2MError, ValueError)


def log_user_error(request: Request, exc: BaseException) -> str:
    """Fehler einer direkten Aktion loggen (mit Ursachenkette) und die Meldung für die Anzeige liefern."""
    log.warning("Aktion %s %s fehlgeschlagen: %s", request.method, request.url.path, describe_error(exc))
    return str(exc) or type(exc).__name__


templates = Jinja2Templates(directory=str(HERE / "templates"))
try:
    templates.env.globals["asset_version"] = metadata.version("device-manager")  # Cache-Buster für static/
except metadata.PackageNotFoundError:  # pragma: no cover - nur ohne Installation
    templates.env.globals["asset_version"] = "dev"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_only(value: str) -> str:
    """'127.0.0.1:8099' -> '127.0.0.1', '[::1]:8099' -> '::1'."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def request_rejection(settings: Settings, request: Request) -> str | None:
    """Zugriffsprüfung. Liefert einen Ablehnungsgrund oder None.

    - Client-IP-Allowlist (HA: nur Ingress-Proxy).
    - Host-Allowlist gegen DNS-Rebinding (lokaler Betrieb).
    - Zustandsändernde Requests nur von htmx (Header HX-Request, von Cross-Site-Formularen nicht setzbar;
      fetch mit eigenem Header erzwingt einen CORS-Preflight, den die App nicht erlaubt) und – im lokalen
      Betrieb – nur mit passendem Origin/Referer.
    """
    clients = settings.allowed_clients
    if clients and (request.client is None or request.client.host not in clients):
        return "Client nicht erlaubt"
    host = request.headers.get("host", "")
    if settings.allowed_hosts and _host_only(host) not in {h.lower() for h in settings.allowed_hosts}:
        return "Host nicht erlaubt"
    if request.method in SAFE_METHODS:
        return None
    if request.headers.get("hx-request") != "true":
        return "HX-Request fehlt"
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return "Cross-Site-Anfrage"
    if settings.allowed_hosts:
        source = request.headers.get("origin") or request.headers.get("referer")
        if source and urlsplit(source).netloc.lower() != host.lower():
            return "Origin passt nicht"
    return None


async def _supervisor_mqtt(settings: Settings) -> Settings:
    """In HA: MQTT-Zugangsdaten vom Supervisor holen, wenn keine eigenen konfiguriert sind."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if settings.mqtt_host or not token:
        return settings
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get("http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"})
        data = r.json().get("data", {}) if r.status_code == 200 else {}
    except httpx.HTTPError as exc:
        log.warning("MQTT-Dienst vom Supervisor nicht abrufbar: %s", type(exc).__name__)
        return settings
    if not data.get("host"):
        return settings
    return settings.model_copy(
        update={
            "mqtt_host": data["host"],
            "mqtt_port": int(data.get("port", 1883)),
            "mqtt_username": data.get("username", ""),
            "mqtt_password": type(settings.mqtt_password)(data.get("password", "")),
        }
    )


def create_app(settings: Settings, context: Context | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned_bus: PahoBus | None = None
        ctx = context
        if ctx is None:
            s = await _supervisor_mqtt(settings)
            ctx = Context(settings=s, store=Store(s.db_path))
            if s.mqtt_host:
                owned_bus = PahoBus(s.mqtt_host, s.mqtt_port, s.mqtt_username, s.mqtt_password.get_secret_value())
                try:
                    await owned_bus.connect()
                    ctx.mqtt_bus = owned_bus
                except ConnectionError as exc:
                    log.warning("%s – Zigbee-Funktionen deaktiviert", exc)
                    owned_bus = None
        app.state.ctx = ctx
        app.state.jobs = JobManager(ctx.store)
        yield
        if owned_bus:
            await owned_bus.close()

    app = FastAPI(title="Device Manager", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        reason = request_rejection(settings, request)
        if reason:
            log.warning("Anfrage abgelehnt (%s): %s %s", reason, request.method, request.url.path)
            return Response(f"Zugriff verweigert: {reason}", status_code=403)
        return await call_next(request)

    def ctx_of(request: Request) -> Context:
        ctx: Context = request.app.state.ctx
        return ctx

    def render(request: Request, name: str, **data: Any) -> HTMLResponse:
        base = request.headers.get("X-Ingress-Path", "").rstrip("/") + "/"
        return templates.TemplateResponse(request, name, {"base": base, **data})

    def start_job(
        request: Request, title: str, key: str | None, fn: Callable[[Progress], Awaitable[Any]]
    ) -> HTMLResponse:
        job = request.app.state.jobs.start(title, key, fn)
        return render(request, "_job.html", job=job)

    def device_or_404(request: Request, key: str) -> Any:
        device = ctx_of(request).store.get_device(key)
        if device is None:
            raise HTTPException(404, "Gerät nicht gefunden")
        return device

    # Seiten --------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        ctx = ctx_of(request)
        stage = ctx.settings.default_firmware_stage
        devices = ctx.store.list_devices()
        rows = [(d, view.device_state(d, stage), view.cert_state(d)) for d in devices]
        checked = [d.checked_at for d in devices if d.checked_at]
        return render(
            request,
            "index.html",
            active="devices",
            rows=rows,
            last_checked=max(checked) if checked else "",
            stage=stage,
            mqtt=ctx.mqtt_bus is not None,
            ntp=ctx.settings.shelly_ntp_server,
        )

    @app.get("/devices/{key}", response_class=HTMLResponse)
    async def device_page(request: Request, key: str) -> HTMLResponse:
        ctx = ctx_of(request)
        device = device_or_404(request, key)
        stage = ctx.settings.default_firmware_stage
        return render(
            request,
            "device.html",
            active="devices",
            device=device,
            state=view.device_state(device, stage),
            cert=view.cert_state(device),
            entries=view.log_entries(ctx.store.actions(key, limit=5), [device]),
            domain=ctx.settings.dns_domain,
            default_stage=stage,
        )

    @app.get("/devices/{key}/firmware-info", response_class=HTMLResponse)
    async def firmware_info(request: Request, key: str) -> HTMLResponse:
        """Installierte und verfügbare Firmware (nur lesend), wird von der Geräteseite nachgeladen."""
        device = device_or_404(request, key)
        installed, updates, error = device.firmware or "", {}, None
        if device.connection == "zigbee" or not device.ip:
            error = "Gerät ist nicht per WLAN erreichbar"
        else:
            client = ctx_of(request).shelly(device.ip, device)
            try:
                installed = (await client.get_device_info()).ver
                updates = await client.check_for_update()
            except USER_ERRORS as exc:
                error = log_user_error(request, exc)
        return render(
            request,
            "_firmware_info.html",
            installed=installed,
            updates=updates,
            error=error,
            prerelease=lambda v: bool(firmware.PRERELEASE.search(v)),
        )

    @app.get("/provision", response_class=HTMLResponse)
    async def provision_page(request: Request, host: str = "") -> HTMLResponse:
        s = ctx_of(request).settings
        return render(
            request,
            "provision.html",
            active="provision",
            host=host,
            domain=s.dns_domain,
            default_stage=s.default_firmware_stage,
            tz=s.shelly_timezone,
            ntp=s.shelly_ntp_server,
        )

    @app.get("/log", response_class=HTMLResponse)
    async def log_page(request: Request, device: str = "") -> HTMLResponse:
        store = ctx_of(request).store
        devices = store.list_devices()
        entries = view.log_entries(store.actions(device or None), devices)
        selected = next((d for d in devices if d.key == device), None)
        return render(request, "log.html", active="log", entries=entries, device=selected, device_key=device)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_fragment(request: Request, job_id: int) -> HTMLResponse:
        job: Job | None = request.app.state.jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job nicht gefunden")
        return render(request, "_job.html", job=job)

    @app.post("/jobs/{job_id}/action", response_class=HTMLResponse)
    async def job_action(request: Request, job_id: int) -> HTMLResponse:
        """Vom Job angeforderte Aktion ausführen (z. B. Neustart nach Firmware-Update) – nur auf Klick."""
        job: Job | None = request.app.state.jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job nicht gefunden")
        try:
            await job.run_action()
        except LookupError:
            pass  # bereits ausgeführt oder Job beendet – aktuellen Stand anzeigen
        except USER_ERRORS as exc:
            job.messages.append(f"Aktion fehlgeschlagen: {log_user_error(request, exc)}")
        return render(request, "_job.html", job=job)

    # Aktionen -------------------------------------------------------------------
    @app.post("/discover", response_class=HTMLResponse)
    async def discover(request: Request, source: Annotated[str, Form()] = "lease") -> HTMLResponse:
        ctx = ctx_of(request)
        try:
            if source == "mdns":
                candidates = await inventory.discover_mdns(ctx)
            else:
                candidates = await inventory.discover_from_leases(ctx)
        except USER_ERRORS as exc:
            return render(request, "_message.html", error=log_user_error(request, exc))
        return render(request, "_candidates.html", candidates=candidates)

    @app.post("/refresh", response_class=HTMLResponse)
    async def refresh(request: Request) -> HTMLResponse:
        errors = await inventory.refresh_all(ctx_of(request))
        text = "; ".join(f"{k}: {v}" for k, v in errors.items())
        return render(
            request,
            "_message.html",
            error=f"Nicht erreichbar oder fehlerhaft: {text}" if text else None,
            message=None if text else "Status aller Geräte abgefragt",
            reload=not text,
            stale=bool(text),
        )

    @app.post("/zigbee/sync", response_class=HTMLResponse)
    async def zigbee_sync(request: Request) -> HTMLResponse:
        try:
            found = await inventory.sync_zigbee(ctx_of(request))
        except USER_ERRORS as exc:
            return render(request, "_message.html", error=log_user_error(request, exc))
        return render(request, "_message.html", message=f"{len(found)} Zigbee-Shelly(s) übernommen", reload=True)

    @app.post("/adopt", response_class=HTMLResponse)
    async def adopt(request: Request, host: Annotated[str, Form()]) -> HTMLResponse:
        ctx = ctx_of(request)
        return start_job(request, f"Übernehmen {host}", None, lambda p: adopt_svc.adopt(ctx, host.strip(), p))

    @app.post("/devices/{key}/reserve", response_class=HTMLResponse)
    async def reserve(
        request: Request,
        key: str,
        name: Annotated[str, Form()],
        ip: Annotated[str, Form()] = "",
        apply: Annotated[bool, Form()] = False,
    ) -> HTMLResponse:
        ctx = ctx_of(request)
        device = device_or_404(request, key)
        if not device.mac:
            return render(request, "_message.html", error="MAC unbekannt – Gerät zuerst einmal per WLAN abfragen")

        async def run(p: Progress) -> reservation.ReservationResult:
            result = await reservation.reserve(ctx, device.mac, name, ip.strip() or None, p)
            if apply and device.ip and device.connection != "zigbee":
                await reservation.apply_reservation(ctx, device.ip, result, p)
            return result

        return start_job(request, "IP-Reservierung", key, run)

    @app.post("/devices/{key}/firmware", response_class=HTMLResponse)
    async def update_fw(request: Request, key: str, stage: Annotated[str, Form()] = "stable") -> HTMLResponse:
        ctx = ctx_of(request)
        device = device_or_404(request, key)
        if device.connection == "zigbee":
            return start_job(
                request,
                f"Firmware über WLAN (Zigbee, {stage})",
                key,
                lambda p: zigbee.zigbee_firmware_update(ctx, key, stage, p),
            )
        if not device.ip:
            return render(request, "_message.html", error="IP-Adresse unbekannt")
        return start_job(
            request,
            f"Firmware-Update ({stage})",
            key,
            lambda p: firmware.update_firmware(ctx, device.ip, stage, p, device),
        )

    @app.post("/devices/{key}/certificate", response_class=HTMLResponse)
    async def install_cert(request: Request, key: str, name: Annotated[str, Form()]) -> HTMLResponse:
        ctx = ctx_of(request)
        device = device_or_404(request, key)
        if not device.ip:
            return render(request, "_message.html", error="IP-Adresse unbekannt")
        return start_job(
            request,
            "Zertifikat installieren",
            key,
            lambda p: certificate.install_certificate(ctx, device.ip, name, p, device),
        )

    @app.post("/devices/{key}/refresh", response_class=HTMLResponse)
    async def refresh_one(request: Request, key: str) -> HTMLResponse:
        device = device_or_404(request, key)
        try:
            await inventory.check_status(ctx_of(request), device)
        except USER_ERRORS as exc:
            return render(request, "_message.html", error=log_user_error(request, exc), stale=True)
        return render(request, "_message.html", message="Status abgefragt", reload=True)

    @app.post("/devices/{key}/password", response_class=HTMLResponse)
    async def show_password(request: Request, key: str) -> HTMLResponse:
        """Zeigt das Geräte-Passwort auf ausdrücklichen Klick (POST, nicht cachebar)."""
        device = device_or_404(request, key)
        # Das Master-Passwort (Altbestand 'shared') wird nie angezeigt – daraus ließen sich alle
        # Geräte-Passwörter ableiten.
        password = ctx_of(request).device_password(device) if device.auth_mode == "device" else ""
        response = render(request, "_password.html", device=device, password=password)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/devices/{key}/rotate-password", response_class=HTMLResponse)
    async def rotate_password(request: Request, key: str) -> HTMLResponse:
        ctx = ctx_of(request)
        device = device_or_404(request, key)
        return start_job(
            request, "Passwort umstellen", key, lambda p: identity.rotate_to_device_password(ctx, device, p)
        )

    @app.post("/devices/{key}/delete", response_class=HTMLResponse)
    async def delete(request: Request, key: str) -> Response:
        ctx_of(request).store.delete_device(key)
        return Response(status_code=200, headers={"HX-Redirect": request.headers.get("X-Ingress-Path", "") + "/"})

    @app.post("/provision", response_class=HTMLResponse)
    async def run_provision(
        request: Request,
        host: Annotated[str, Form()],
        name: Annotated[str, Form()],
        ip: Annotated[str, Form()] = "",
        stage: Annotated[str, Form()] = "stable",
        update: Annotated[bool, Form()] = False,
        defaults_: Annotated[bool, Form(alias="defaults")] = False,
        certificate_: Annotated[bool, Form(alias="certificate")] = False,
    ) -> HTMLResponse:
        ctx = ctx_of(request)
        return start_job(
            request,
            f"Neues Gerät {name}",
            None,
            lambda p: provision.provision(
                ctx,
                host.strip(),
                name,
                ip=ip.strip() or None,
                stage=stage,
                update=update,
                defaults=defaults_,
                certificate=certificate_,
                progress=p,
            ),
        )

    @app.post("/ntp/distribute", response_class=HTMLResponse)
    async def ntp_distribute(request: Request) -> HTMLResponse:
        ctx = ctx_of(request)
        server = ctx.settings.shelly_ntp_server
        return start_job(request, f"NTP-Server {server} verteilen", None, lambda p: defaults.distribute_ntp(ctx, p))

    # JSON-API ----------------------------------------------------------------------
    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        ctx = ctx_of(request)
        return {"status": "ok", "mqtt": ctx.mqtt_bus is not None, "opnsense": bool(ctx.settings.opnsense_url)}

    @app.get("/api/devices")
    async def api_devices(request: Request) -> list[dict[str, Any]]:
        return [asdict(d) for d in ctx_of(request).store.list_devices()]

    @app.get("/api/next-ip")
    async def api_next_ip(request: Request) -> JSONResponse:
        try:
            return JSONResponse({"ip": await reservation.next_free_ip(ctx_of(request))})
        except USER_ERRORS as exc:
            return JSONResponse({"error": log_user_error(request, exc)}, status_code=400)

    @app.get("/api/jobs/{job_id}")
    async def api_job(request: Request, job_id: int) -> dict[str, Any]:
        job: Job | None = request.app.state.jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job nicht gefunden")
        result = asdict(job.result) if is_dataclass(job.result) and not isinstance(job.result, type) else job.result
        return {
            "id": job.id,
            "status": job.status,
            "messages": job.messages,
            "error": job.error,
            "result": result if job.done else None,
        }

    return app
