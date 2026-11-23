"""F1 – Geräte-Inventar und Discovery."""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass

from ..context import Context
from ..shelly.client import ShellyError
from ..steps import step
from ..store import Device, now
from .common import ServiceError, device_from_info, lease_mac

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Candidate:
    ip: str
    mac: str | None
    name: str
    source: str  # lease | mdns
    known: bool = False


async def refresh_device(ctx: Context, host: str) -> Device:
    """Liest Geräteinfos per /shelly und aktualisiert das Inventar."""
    info = await ctx.shelly(host).get_device_info()
    existing = ctx.store.get_device(info.mac_colon)
    device = device_from_info(info, host, existing)
    device.online, device.checked_at = 1, now()
    return ctx.store.upsert_device(device)


def _mark_offline(ctx: Context, key: str) -> None:
    stored = ctx.store.get_device(key)  # neu laden: die IP kann inzwischen einem anderen Gerät gehören
    if stored is not None:
        stored.online, stored.checked_at = 0, now()
        ctx.store.upsert_device(stored)


async def check_status(ctx: Context, device: Device) -> Device:
    """F12: Erreichbarkeit, Geräteinfo und verfügbare Firmware abfragen und im Inventar speichern.

    Nicht erreichbare Geräte werden als offline gespeichert, der Fehler wird weitergereicht. Kann die
    Update-Prüfung nicht laufen (z. B. ohne Zugangsdaten), bleibt die verfügbare Version unbekannt.
    """
    if not device.ip or device.connection == "zigbee":
        raise ServiceError(f"{device.key}: Status nur für WLAN-Geräte mit bekannter IP abfragbar")
    host = device.ip
    with step(f"Status {host}"):
        try:
            current = await refresh_device(ctx, host)
        except ShellyError:
            _mark_offline(ctx, device.key)
            raise
        if current.key != device.key:
            _mark_offline(ctx, device.key)
            raise ServiceError(f"{host}: dort antwortet ein anderes Gerät ({current.key}) statt {device.key}")
        try:
            # Zugangsdaten nur aus dem Inventar-Eintrag (Realm-Prüfung über device_id)
            updates = await ctx.shelly(host, current).check_for_update()
        except ShellyError as exc:
            log.warning("Update-Prüfung %s (%s) fehlgeschlagen: %s", current.key, host, exc)
            current.fw_stable = current.fw_beta = None
        else:
            current.fw_stable, current.fw_beta = updates.get("stable", ""), updates.get("beta", "")
        return ctx.store.upsert_device(current)


async def refresh_all(ctx: Context) -> dict[str, str]:
    """Fragt den Status aller WLAN-Geräte ab (F12); liefert Fehler je Gerät."""
    errors: dict[str, str] = {}

    async def one(device: Device) -> None:
        if not device.ip or device.connection == "zigbee":
            return
        try:
            await check_status(ctx, device)
        except (ShellyError, ServiceError) as exc:
            log.warning("Aktualisieren von %s (%s) fehlgeschlagen: %s", device.key, device.ip, exc)
            errors[device.key] = str(exc)

    devices = ctx.store.list_devices()
    log.info("Aktualisiere %d Gerät(e)", len(devices))
    await asyncio.gather(*(one(d) for d in devices))
    log.info("Aktualisierung beendet, %d Fehler", len(errors))
    return errors


async def discover_from_leases(ctx: Context) -> list[Candidate]:
    """Kea-Leases nach Shelly-Geräten durchsuchen und per /shelly bestätigen."""
    leases = await ctx.opnsense().kea_leases()
    probes = []
    for lease in leases:
        ip = lease.get("address")
        if not ip:
            continue
        hostname = str(lease.get("hostname") or "")
        mac = lease_mac(lease)
        known = bool(mac and ctx.store.get_device(mac))
        if hostname.lower().startswith("shelly") or known:
            probes.append(Candidate(ip, mac, hostname, "lease", known))
    log.info("Suche über Kea-Leases: %d Lease(s), %d mögliche Shelly(s)", len(leases), len(probes))
    return await _confirm(ctx, probes)


async def _confirm(ctx: Context, candidates: list[Candidate]) -> list[Candidate]:
    async def probe(candidate: Candidate) -> Candidate | None:
        try:
            info = await ctx.shelly(candidate.ip).get_device_info()
        except ShellyError as exc:
            log.info("Kandidat %s (%s) verworfen: %s", candidate.ip, candidate.name or "?", exc)
            return None
        known = ctx.store.get_device(info.mac_colon) is not None
        return Candidate(candidate.ip, info.mac_colon, info.name or info.id, candidate.source, known)

    results = await asyncio.gather(*(probe(c) for c in candidates))
    confirmed = [c for c in results if c is not None]
    log.info("Suche: %d von %d Kandidat(en) als Shelly bestätigt", len(confirmed), len(candidates))
    return confirmed


async def discover_mdns(ctx: Context, duration: float = 3.0) -> list[Candidate]:
    """mDNS-Suche nach _shelly._tcp (Gen2+)."""
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    found: dict[str, str] = {}
    azc = AsyncZeroconf()
    names: list[str] = []

    def on_change(zeroconf, service_type, name, state_change):  # type: ignore[no-untyped-def]
        if state_change is ServiceStateChange.Added:
            names.append(name)

    browser = AsyncServiceBrowser(azc.zeroconf, ["_shelly._tcp.local."], handlers=[on_change])
    try:
        await asyncio.sleep(duration)
        for name in names:
            info = AsyncServiceInfo("_shelly._tcp.local.", name)
            if await info.async_request(azc.zeroconf, 2000):
                for addr in info.addresses:
                    if len(addr) == 4:
                        found[socket.inet_ntoa(addr)] = name.split(".")[0]
    finally:
        await browser.async_cancel()
        await azc.async_close()
    return await _confirm(ctx, [Candidate(ip, None, n, "mdns") for ip, n in found.items()])


async def sync_zigbee(ctx: Context) -> list[Device]:
    """Shelly-Geräte aus Zigbee2MQTT ins Inventar übernehmen."""
    devices = []
    for zdev in await ctx.z2m().shelly_devices():
        device = ctx.store.find_device(ieee=zdev.ieee_address) or Device(key=f"z2m:{zdev.ieee_address}")
        device.connection = "zigbee"
        device.z2m_name = zdev.friendly_name
        device.ieee = zdev.ieee_address
        device.model = device.model or zdev.model
        device.gen = device.gen or 4
        devices.append(ctx.store.upsert_device(device))
    log.info("Zigbee2MQTT: %d Shelly-Gerät(e) übernommen", len(devices))
    return devices
