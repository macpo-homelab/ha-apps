"""Client für die Shelly-JSON-RPC-API (Gen2, Gen3, Gen4)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import logging
import re
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

AUTH_USER = "admin"
CERT_CHUNK_SIZE = 1024
MIN_HTTPS_FIRMWARE = (2, 0, 0)

HttpFactory = Callable[..., httpx.AsyncClient]


class ShellyError(Exception):
    """Allgemeiner Fehler bei der Kommunikation mit einem Shelly."""


class ShellyUnreachable(ShellyError):
    pass


class ShellyAuthError(ShellyError):
    pass


class ShellyUnsupported(ShellyError):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    mac: str
    model: str
    gen: int
    ver: str
    app: str
    name: str | None
    auth_en: bool

    @property
    def mac_colon(self) -> str:
        return format_mac(self.mac)


def format_mac(mac: str) -> str:
    """'A8032AB12345' / 'a8-03-...' -> 'a8:03:2a:b1:23:45'."""
    digits = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    if len(digits) != 12:
        raise ValueError(f"Ungültige MAC-Adresse: {mac!r}")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def parse_version(ver: str) -> tuple[int, int, int]:
    """'1.4.4' / '1.5.0-beta2' / '20240101-120000/1.7.1-gabc' -> (1, 5, 0)."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", ver.split("/")[-1])
    if not match:
        raise ValueError(f"Unbekanntes Versionsformat: {ver!r}")
    major, minor, patch = (int(x) for x in match.groups())
    return major, minor, patch


def supports_custom_https(ver: str) -> bool:
    return parse_version(ver) >= MIN_HTTPS_FIRMWARE


def product_name(app: str, gen: int, model: str = "") -> str:
    """Lesbarer Produktname, z. B. 'PlugMG3', Gen 3 -> 'Shelly Plug M Gen 3'; 'Mini1PMG3' -> 'Shelly Mini 1PM Gen 3'.

    Die Shelly-App-Kennung endet bei Gen3/Gen4 auf 'G3'/'G4'; Wortgrenzen liegen vor Großbuchstaben nach
    Kleinbuchstaben und vor Ziffern nach Buchstaben ('PlusPlugS' -> 'Plus Plug S', 'Pro4PM' -> 'Pro 4PM').
    """
    name = re.sub(rf"G{gen}$", "", app) if app else ""
    name = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", name).strip()
    base = f"Shelly {name}" if name else f"Shelly {model}".strip()
    return f"{base} Gen {gen}" if gen else base


def derive_password(master: str, mac: str) -> str:
    """Gerätespezifisches Passwort: HMAC-SHA256(Master, MAC) – ein abgeflossener Hash betrifft nur ein Gerät."""
    digest = hmac.new(master.encode(), format_mac(mac).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest)[:22].decode()


def id_matches_mac(device_id: str, mac: str) -> bool:
    """Shelly-Geräte-IDs (= Digest-Realm) enden auf die MAC, z. B. 'shellyplus1pm-a8032ab12345'."""
    digits = re.sub(r"[^0-9a-f]", "", mac.lower())
    return len(digits) == 12 and device_id.lower().endswith(digits)


_REALM_RE = re.compile(r'realm="([^"]*)"', re.IGNORECASE)


class RealmCheckedDigestAuth(httpx.DigestAuth):
    """Digest-Auth, die nur auf Challenges des erwarteten Geräts (Realm = Geräte-ID) antwortet.

    Verhindert, dass ein fremder Host eine Challenge eines anderen Geräts durchreicht (Relay).
    """

    def __init__(self, username: str, password: str, expected_realm: str | None) -> None:
        super().__init__(username, password)
        self._expected_realm = expected_realm

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        flow = super().auth_flow(request)
        outgoing = next(flow)
        while True:
            response = yield outgoing
            if response.status_code == 401 and self._expected_realm is not None:
                match = _REALM_RE.search(response.headers.get("www-authenticate", ""))
                if not match or match.group(1) != self._expected_realm:
                    raise ShellyAuthError(
                        f"{request.url.host}: unerwarteter Digest-Realm – Gerät ist nicht {self._expected_realm}"
                    )
            try:
                outgoing = flow.send(response)
            except StopIteration:
                return


class ShellyClient:
    _ids = itertools.count(1)

    def __init__(
        self,
        host: str,
        password: str = "",
        *,
        expected_realm: str | None = None,
        http_factory: HttpFactory = httpx.AsyncClient,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self._password = password
        self.expected_realm = expected_realm
        self._http_factory = http_factory
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return f"http://{self.host}"

    def _auth(self, use_auth: bool) -> httpx.Auth | None:
        if use_auth and self._password:
            return RealmCheckedDigestAuth(AUTH_USER, self._password, self.expected_realm)
        return None

    async def _request(
        self, method: str, path: str, *, json: Any = None, use_auth: bool = True, what: str | None = None
    ) -> Any:
        # Nur Methode/Pfad loggen – Request-Bodies können Passwort-Hashes oder private Schlüssel enthalten.
        what = what or f"{method} {path}"
        url = f"{self.base_url}{path}"
        started = time.monotonic()
        log.debug("Shelly %s: %s → %s", self.host, what, url)
        try:
            async with self._http_factory(timeout=self._timeout) as http:
                response = await http.request(method, url, json=json, auth=self._auth(use_auth))
        except httpx.TimeoutException as exc:
            raise ShellyUnreachable(
                f"{self.host} nicht erreichbar: keine Antwort innerhalb von {self._timeout:.0f}s bei {what} "
                f"({type(exc).__name__})"
            ) from exc
        except (httpx.TransportError, OSError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise ShellyUnreachable(
                f"{self.host} nicht erreichbar bei {what}: {detail} ({type(exc).__name__})"
            ) from exc
        elapsed = time.monotonic() - started
        log.debug("Shelly %s: %s ← HTTP %s in %.2fs", self.host, what, response.status_code, elapsed)
        if response.status_code == 401:
            raise ShellyAuthError(
                f"{self.host}: Authentifizierung fehlgeschlagen bei {what} (HTTP 401 – Passwort falsch oder fehlt)"
            )
        if response.status_code >= 400 and not response.content:
            raise ShellyError(f"{self.host}: HTTP {response.status_code} bei {what}")
        try:
            return response.json()
        except ValueError as exc:
            snippet = response.text[:200].strip()
            raise ShellyError(
                f"{self.host}: ungültige Antwort bei {what} (HTTP {response.status_code}, kein JSON): {snippet!r}"
            ) from exc

    async def rpc(self, method: str, params: dict[str, Any] | None = None, *, use_auth: bool = True) -> Any:
        body: dict[str, Any] = {"id": next(self._ids), "method": method}
        if params is not None:
            body["params"] = params
        data = await self._request("POST", "/rpc", json=body, use_auth=use_auth, what=f"RPC {method}")
        if "error" in data:
            err = data["error"]
            if err.get("code") == 401:
                raise ShellyAuthError(f"{self.host}: Authentifizierung fehlgeschlagen bei RPC {method}")
            raise ShellyError(
                f"{self.host}: RPC {method} fehlgeschlagen: {err.get('message', err)} (Code {err.get('code', '?')})"
            )
        return data.get("result")

    # High-Level-Methoden ---------------------------------------------------
    async def get_device_info(self) -> DeviceInfo:
        data = await self._request("GET", "/shelly", use_auth=False, what="Geräteinfo (GET /shelly)")
        gen = data.get("gen")
        if not isinstance(gen, int) or gen < 2:
            raise ShellyUnsupported(f"{self.host}: nur Gen2+ wird unterstützt (Gen1 erkannt)")
        return DeviceInfo(
            id=data["id"],
            mac=data["mac"],
            model=data.get("model", ""),
            gen=gen,
            ver=data.get("ver", ""),
            app=data.get("app", ""),
            name=data.get("name"),
            auth_en=bool(data.get("auth_en", False)),
        )

    async def check_auth(self) -> None:
        """Prüft, ob das konfigurierte Passwort akzeptiert wird (Methode, die Auth erfordert)."""
        await self.rpc("Sys.GetConfig")

    async def set_auth(self, password: str, device_id: str) -> None:
        """Setzt bzw. ändert das Passwort. Bei geschütztem Gerät mit dem bisherigen Passwort authentifiziert."""
        if self.expected_realm is not None and device_id != self.expected_realm:
            raise ShellyAuthError(f"{self.host}: SetAuth nur für das geprüfte Gerät {self.expected_realm}")
        ha1 = hashlib.sha256(f"{AUTH_USER}:{device_id}:{password}".encode()).hexdigest()
        await self.rpc("Shelly.SetAuth", {"user": AUTH_USER, "realm": device_id, "ha1": ha1})
        self._password = password

    async def check_for_update(self) -> dict[str, str]:
        result = await self.rpc("Shelly.CheckForUpdate") or {}
        return {stage: info["version"] for stage, info in result.items() if info and "version" in info}

    async def set_config(self, component: str, config: dict[str, Any]) -> bool:
        """<Component>.SetConfig; liefert restart_required."""
        result = await self.rpc(f"{component}.SetConfig", {"config": config})
        return bool(result.get("restart_required")) if isinstance(result, dict) else False

    async def sys_status(self) -> dict[str, Any]:
        """Sys.GetStatus: u. a. `uptime` (s seit Neustart) und `restart_required`."""
        result = await self.rpc("Sys.GetStatus")
        return result if isinstance(result, dict) else {}

    async def update(self, stage: str) -> None:
        await self.rpc("Shelly.Update", {"stage": stage})

    async def reboot(self) -> None:
        await self.rpc("Shelly.Reboot")

    async def _put_chunked(self, method: str, pem: str) -> None:
        chunks = [pem[i : i + CERT_CHUNK_SIZE] for i in range(0, len(pem), CERT_CHUNK_SIZE)] or [""]
        for index, chunk in enumerate(chunks):
            await self.rpc(method, {"data": chunk, "append": index > 0})

    async def put_http_server_cert(self, pem: str) -> None:
        await self._put_chunked("Shelly.PutHTTPServerCert", pem)

    async def put_http_server_key(self, pem: str) -> None:
        await self._put_chunked("Shelly.PutHTTPServerKey", pem)
