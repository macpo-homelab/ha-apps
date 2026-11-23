"""F5 – Zertifikat in OPNsense ausstellen und als HTTPS-Serverzertifikat auf dem Shelly installieren."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime

from ..context import Context
from ..jobs import Progress
from ..opnsense.client import OPNsenseError
from ..shelly.client import supports_custom_https
from ..store import Device
from ..tls import CertInfo, key_matches_cert
from .common import ServiceError, fqdn, noop_progress, normalize_hostname, step, validate_hostname, wait_for_device
from .identity import verify_inventory_device

DESCR_PREFIX = "Device Manager"
# Zertifikate aus der Zeit als Shelly-Manager weiter erkennen und beim Erneuern entfernen
LEGACY_DESCR_PREFIXES = ("Shelly-Manager",)


@dataclass(frozen=True)
class CertificateResult:
    fqdn: str
    cert_uuid: str
    not_after: datetime
    sha256: str
    removed_old: int


def _descr(name: str) -> str:
    return f"{DESCR_PREFIX} {name}"


async def install_certificate(
    ctx: Context,
    host: str,
    hostname: str,
    progress: Progress = noop_progress,
    device: Device | None = None,
) -> CertificateResult:
    with step(f"Zertifikat {host}"):
        return await _install(ctx, host, hostname, progress, device)


async def _install(
    ctx: Context, host: str, hostname: str, progress: Progress, device: Device | None
) -> CertificateResult:
    s = ctx.settings
    with step("Voraussetzungen prüfen"):
        if not s.cert_ca_name:
            raise ServiceError("cert_ca_name (Name der OPNsense-CA) ist nicht konfiguriert")
        hostname = validate_hostname(hostname)
        name = fqdn(ctx, hostname)
        try:
            ipaddress.IPv4Address(host)
        except ValueError as exc:
            raise ServiceError(f"'{host}' ist keine IPv4-Adresse") from exc
        ips = [host]

        # Schlüssel nur an verifizierte, reservierte Geräte: Inventar ↔ Gerät ↔ Kea-Reservierung (IP + Hostname)
        device = device or ctx.store.find_device(ip=host)
        if device is None or not device.mac:
            raise ServiceError(f"{host} ist kein übernommenes Gerät im Inventar – zuerst übernehmen")
        info = await verify_inventory_device(ctx, host, device)
        client = ctx.shelly(host, device)
        api = ctx.opnsense()
        reservation = next((r for r in await api.kea_reservations() if r.hw_address == device.mac), None)
        if reservation is None or reservation.ip_address != host:
            raise ServiceError(f"Zertifikate nur für Geräte mit Kea-Reservierung auf {host} – zuerst IP reservieren")
        if normalize_hostname(reservation.hostname) != hostname:
            raise ServiceError(f"Hostname muss der Reservierung entsprechen ('{reservation.hostname}')")
        if not supports_custom_https(info.ver):
            raise ServiceError(
                f"Firmware {info.ver} unterstützt keine eigenen HTTPS-Zertifikate (ab 2.0.0) – zuerst aktualisieren"
            )

    with step(f"CA '{s.cert_ca_name}' in OPNsense laden"):
        ca = await api.find_ca(s.cert_ca_name)
        ca_pem = await api.ca_certificate(ca)
        descrs = {_descr(name), *(f"{prefix} {name}" for prefix in LEGACY_DESCR_PREFIXES)}
        old = [c["uuid"] for c in await api.search_certs() if c.get("descr") in descrs]

    with step(f"Zertifikat für {name} ausstellen"):
        dns_names = [name] if name == hostname else [name, hostname]
        issued = await api.issue_server_cert(
            ca,
            common_name=name,
            dns_names=dns_names,
            ip_addresses=ips,
            lifetime_days=s.cert_lifetime_days,
            descr=_descr(name),
            key_location=s.cert_key_location,
        )
        progress(f"Zertifikat für {name} von CA '{ca.descr}' ausgestellt")
        if not key_matches_cert(issued.cert_pem, issued.key_pem):
            raise ServiceError("Von OPNsense geliefertes Zertifikat passt nicht zum privaten Schlüssel")

    with step("Zertifikat und Schlüssel auf das Gerät übertragen"):
        await client.put_http_server_cert(issued.cert_pem)
        await client.put_http_server_key(issued.key_pem)
    progress("Zertifikat und Schlüssel auf das Gerät übertragen, starte neu …")
    with step("Neustart abwarten"):
        await client.reboot()
        await ctx.sleep(s.poll_interval)
        await wait_for_device(ctx, host, timeout=s.reboot_timeout, what="Neustart:")

    with step("HTTPS prüfen"):
        try:
            served: CertInfo = await ctx.tls_verifier(host, server_hostname=name, ca_pem=ca_pem)
        except Exception as exc:  # noqa: BLE001 - jede TLS-Fehlerart gleich behandeln
            raise ServiceError(f"HTTPS-Prüfung fehlgeschlagen: {exc}") from exc
    progress(f"HTTPS geprüft: gültig bis {served.not_after:%d.%m.%Y}, SHA256 {served.sha256[:23]}…")

    with step("Alte Zertifikate in OPNsense entfernen"):
        removed = 0
        for uuid in old:
            try:
                await api.delete_cert(uuid)
                removed += 1
            except OPNsenseError as exc:
                progress(f"Hinweis: altes Zertifikat {uuid} nicht gelöscht ({exc})")
        if removed:
            progress(f"{removed} altes Zertifikat(e) in OPNsense entfernt")

    device = ctx.store.find_device(mac=info.mac_colon) or device
    device.hostname = hostname
    device.cert_not_after = served.not_after.astimezone(UTC).isoformat(timespec="seconds")
    ctx.store.upsert_device(device)
    return CertificateResult(name, issued.uuid, served.not_after, served.sha256, removed)
