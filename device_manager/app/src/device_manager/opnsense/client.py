"""Client für die OPNsense-API (Kea DHCPv4 und Trust/Zertifikate).

Feldnamen und Endpunkte stammen aus dem OPNsense-Quellcode (Models KeaDhcpv4.xml, Cert.xml).
"""

from __future__ import annotations

import logging
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

HttpFactory = Callable[..., httpx.AsyncClient]

ALL_ROWS = {"current": 1, "rowCount": -1, "searchPhrase": ""}


class OPNsenseError(Exception):
    pass


def _error_text(response: httpx.Response) -> str:
    """Fehlermeldung aus einer OPNsense-Fehlerantwort (gekürzt)."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:200].strip() or "(leere Antwort)"
    if isinstance(data, dict):
        return str(data.get("message") or data.get("errorMessage") or data)[:300]
    return str(data)[:300]


@dataclass(frozen=True)
class Subnet:
    uuid: str
    cidr: str


@dataclass(frozen=True)
class Reservation:
    uuid: str
    subnet: str
    ip_address: str
    hw_address: str
    hostname: str
    description: str = ""


@dataclass(frozen=True)
class CertificateAuthority:
    uuid: str
    refid: str
    descr: str


@dataclass(frozen=True)
class IssuedCert:
    uuid: str
    cert_pem: str
    key_pem: str


class OPNsenseClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        *,
        verify: bool | str = True,
        http_factory: HttpFactory = httpx.AsyncClient,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise OPNsenseError("OPNsense-URL ist nicht konfiguriert")
        self._base = base_url.rstrip("/")
        self._auth = (api_key, api_secret)
        self._ca_file = verify if isinstance(verify, str) and verify else ""
        self._verify: bool | ssl.SSLContext = bool(verify)
        if self._ca_file:
            try:
                self._verify = ssl.create_default_context(cafile=self._ca_file)
            except (OSError, ssl.SSLError) as exc:
                raise OPNsenseError(
                    f"CA-Datei für OPNsense nicht lesbar: {self._ca_file} ({exc}) – Pfad in opnsense_ca_file prüfen, "
                    "erwartet wird das CA-Zertifikat im PEM-Format"
                ) from exc
        self._factory = http_factory
        self._timeout = timeout

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        # Nur Methode/Pfad loggen – Bodies und Antworten können private Schlüssel enthalten.
        started = time.monotonic()
        log.debug("OPNsense: %s %s", method, path)
        try:
            async with self._factory(timeout=self._timeout, verify=self._verify) as http:
                response = await http.request(method, f"{self._base}/api/{path}", json=body, auth=self._auth)
        except httpx.TimeoutException as exc:
            raise OPNsenseError(
                f"OPNsense ({self._base}) antwortet nicht innerhalb von {self._timeout:.0f}s bei {path} "
                f"({type(exc).__name__})"
            ) from exc
        except httpx.TransportError as exc:
            detail = str(exc).strip() or type(exc).__name__
            hint = ""
            if "CERTIFICATE" in detail.upper():
                hint = (
                    f" – die CA-Datei {self._ca_file} passt nicht zum Webzertifikat der OPNsense"
                    if self._ca_file
                    else " – CA-Zertifikat der OPNsense als PEM-Datei ablegen und in opnsense_ca_file eintragen"
                )
            raise OPNsenseError(
                f"OPNsense ({self._base}) nicht erreichbar bei {path}: {detail} ({type(exc).__name__}){hint}"
            ) from exc
        elapsed = time.monotonic() - started
        log.debug("OPNsense: %s %s ← HTTP %s in %.2fs", method, path, response.status_code, elapsed)
        if response.status_code in (401, 403):
            raise OPNsenseError(
                f"OPNsense: API-Zugang abgelehnt bei {path} (HTTP {response.status_code}) – Key/Secret oder "
                "Benutzerrechte prüfen"
            )
        if response.status_code >= 400:
            raise OPNsenseError(f"OPNsense: HTTP {response.status_code} bei {path}: {_error_text(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise OPNsenseError(f"OPNsense: ungültige Antwort bei {path} (kein JSON): {response.text[:200]!r}") from exc

    async def _search(self, path: str) -> list[dict[str, Any]]:
        data = await self._call("POST", path, ALL_ROWS)
        rows: list[dict[str, Any]] = data.get("rows", [])
        return rows

    @staticmethod
    def _check_saved(data: dict[str, Any], what: str) -> str:
        if data.get("result") != "saved":
            details = data.get("validations") or data.get("message") or data
            raise OPNsenseError(f"{what} fehlgeschlagen: {details}")
        return str(data.get("uuid", ""))

    # Kea --------------------------------------------------------------------
    async def kea_subnets(self) -> list[Subnet]:
        return [Subnet(r["uuid"], r["subnet"]) for r in await self._search("kea/dhcpv4/search_subnet")]

    async def kea_reservations(self) -> list[Reservation]:
        return [
            Reservation(
                uuid=r["uuid"],
                subnet=r.get("subnet", ""),
                ip_address=r.get("ip_address", ""),
                hw_address=r.get("hw_address", "").lower(),
                hostname=r.get("hostname", ""),
                description=r.get("description", ""),
            )
            for r in await self._search("kea/dhcpv4/search_reservation")
        ]

    async def kea_leases(self) -> list[dict[str, Any]]:
        return await self._search("kea/leases4/search")

    async def kea_add_reservation(self, subnet_uuid: str, ip: str, mac: str, hostname: str, description: str) -> str:
        body = {
            "reservation": {
                "subnet": subnet_uuid,
                "ip_address": ip,
                "hw_address": mac,
                "hostname": hostname,
                "description": description,
            }
        }
        return self._check_saved(await self._call("POST", "kea/dhcpv4/add_reservation", body), "Reservierung")

    async def kea_set_reservation(
        self, uuid: str, subnet_uuid: str, ip: str, mac: str, hostname: str, description: str
    ) -> None:
        body = {
            "reservation": {
                "subnet": subnet_uuid,
                "ip_address": ip,
                "hw_address": mac,
                "hostname": hostname,
                "description": description,
            }
        }
        self._check_saved(await self._call("POST", f"kea/dhcpv4/set_reservation/{uuid}", body), "Reservierung ändern")

    async def kea_reconfigure(self) -> None:
        data = await self._call("POST", "kea/service/reconfigure", {})
        if str(data.get("status", "")).lower() not in ("ok", "done"):
            raise OPNsenseError(f"Kea-Reconfigure fehlgeschlagen: {data}")

    # Unbound ----------------------------------------------------------------
    async def unbound_reconfigure(self) -> None:
        """Unbound neu laden, damit registrierte DHCP-Reservierungen per DNS auflösbar werden."""
        try:
            data = await self._call("POST", "unbound/service/reconfigure", {})
        except OPNsenseError as exc:
            if "API-Zugang abgelehnt" in str(exc):
                raise OPNsenseError(f"{exc} – der API-Benutzer braucht das Recht 'Services: Unbound DNS'") from exc
            raise
        if str(data.get("status", "")).lower() not in ("ok", "done"):
            raise OPNsenseError(f"Unbound-Reconfigure fehlgeschlagen: {data}")

    # Trust ------------------------------------------------------------------
    async def find_ca(self, descr: str) -> CertificateAuthority:
        for row in await self._search("trust/ca/search"):
            if row.get("descr") == descr:
                return CertificateAuthority(row["uuid"], row["refid"], row["descr"])
        raise OPNsenseError(f"CA '{descr}' wurde in OPNsense nicht gefunden")

    async def ca_certificate(self, ca: CertificateAuthority) -> str:
        return await self._generate_file("ca", ca.uuid, "crt")

    async def _generate_file(self, kind: str, uuid: str, file_type: str) -> str:
        data = await self._call("POST", f"trust/{kind}/generate_file/{uuid}/{file_type}", {})
        if data.get("status") != "ok" or not data.get("payload"):
            raise OPNsenseError(f"Download {kind}/{file_type} fehlgeschlagen: {data.get('error', data)}")
        return str(data["payload"])

    async def issue_server_cert(
        self,
        ca: CertificateAuthority,
        *,
        common_name: str,
        dns_names: list[str],
        ip_addresses: list[str],
        lifetime_days: int,
        descr: str,
        key_location: str = "firewall",
    ) -> IssuedCert:
        body = {
            "cert": {
                "action": "internal",
                "descr": descr,
                "caref": ca.refid,
                "key_type": "prime256v1",
                "digest": "sha256",
                "cert_type": "server_cert",
                "lifetime": str(lifetime_days),
                "private_key_location": key_location,
                "commonname": common_name,
                "altnames_dns": "\n".join(dns_names),
                "altnames_ip": "\n".join(ip_addresses),
            }
        }
        data = await self._call("POST", "trust/cert/add", body)
        uuid = self._check_saved(data, "Zertifikat erstellen")
        cert_pem = await self._generate_file("cert", uuid, "crt")
        key_pem = data.get("private_key") or await self._generate_file("cert", uuid, "prv")
        return IssuedCert(uuid=uuid, cert_pem=cert_pem, key_pem=str(key_pem))

    async def search_certs(self) -> list[dict[str, Any]]:
        return await self._search("trust/cert/search")

    async def delete_cert(self, uuid: str) -> None:
        data = await self._call("POST", f"trust/cert/del/{uuid}", {})
        if data.get("result") != "deleted":
            raise OPNsenseError(f"Zertifikat {uuid} konnte nicht gelöscht werden: {data}")
