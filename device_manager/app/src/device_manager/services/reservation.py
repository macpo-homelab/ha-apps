"""F3 – DHCP-Reservierung in OPNsense (Kea)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from ..context import Context
from ..jobs import Progress
from ..opnsense.client import OPNsenseClient, OPNsenseError, Reservation
from ..shelly.client import format_mac, product_name
from .common import ServiceError, lease_mac, noop_progress, normalize_hostname, step, validate_name, wait_for_device

DEFAULT_DESCRIPTION = "Shelly"


def description_for(ctx: Context, mac: str) -> str:
    """Beschreibung der Reservierung: Produktname mit Generation, z. B. 'Shelly Plug M Gen 3'."""
    device = ctx.store.find_device(mac=mac)
    if device and (device.app or device.model):
        return product_name(device.app or "", device.gen or 0, device.model or "")
    return DEFAULT_DESCRIPTION


@dataclass(frozen=True)
class ReservationResult:
    ip: str
    hostname: str  # DNS-Hostname (Kleinbuchstaben)
    mac: str
    uuid: str
    changed: bool
    name: str = ""  # Name mit Groß-/Kleinschreibung, so in Kea eingetragen


async def _used_ips(api: OPNsenseClient, reservations: list[Reservation], mac: str) -> set[str]:
    used = {r.ip_address for r in reservations if r.hw_address != mac and r.ip_address}
    for lease in await api.kea_leases():
        if lease.get("address") and lease_mac(lease) != mac:
            used.add(str(lease["address"]))
    return used


def _next_free(start: str, end: str, used: set[str]) -> str:
    current, last = ipaddress.IPv4Address(start), ipaddress.IPv4Address(end)
    while current <= last:
        if str(current) not in used:
            return str(current)
        current += 1
    raise ServiceError(f"Keine freie IP im Bereich {start}–{end} ({len(used)} Adressen belegt)")


async def next_free_ip(ctx: Context, mac: str = "") -> str:
    s = ctx.settings
    if not (s.ip_range_start and s.ip_range_end):
        raise ServiceError("IP-Bereich (ip_range_start/ip_range_end) ist nicht konfiguriert")
    api = ctx.opnsense()
    reservations = await api.kea_reservations()
    return _next_free(s.ip_range_start, s.ip_range_end, await _used_ips(api, reservations, mac))


async def reserve(
    ctx: Context, mac: str, name: str, ip: str | None = None, progress: Progress = noop_progress
) -> ReservationResult:
    """Reservierung mit dem Namen als Kea-Hostname (Groß-/Kleinschreibung bleibt erhalten)."""
    with step(f"IP-Reservierung {mac}"):
        return await _reserve(ctx, mac, name, ip, progress)


async def _reserve(ctx: Context, mac: str, name: str, ip: str | None, progress: Progress) -> ReservationResult:
    mac = format_mac(mac)
    name = validate_name(name)
    hostname = name.lower()
    description = description_for(ctx, mac)
    api = ctx.opnsense()
    with step("Reservierungen aus OPNsense lesen"):
        reservations = await api.kea_reservations()
    existing = next((r for r in reservations if r.hw_address == mac), None)

    with step("IP-Adresse bestimmen"):
        if ip is None:
            if existing and existing.ip_address:
                ip = existing.ip_address
            else:
                s = ctx.settings
                if not (s.ip_range_start and s.ip_range_end):
                    raise ServiceError("IP-Bereich (ip_range_start/ip_range_end) ist nicht konfiguriert")
                ip = _next_free(s.ip_range_start, s.ip_range_end, await _used_ips(api, reservations, mac))
                progress(f"Nächste freie IP: {ip}")
        else:
            try:
                ipaddress.IPv4Address(ip)
            except ValueError as exc:
                raise ServiceError(f"Ungültige IP-Adresse: {ip}") from exc
            if ip in await _used_ips(api, reservations, mac):
                raise ServiceError(f"IP {ip} ist bereits an ein anderes Gerät vergeben (Reservierung oder Lease)")

        conflict = next(
            (r for r in reservations if normalize_hostname(r.hostname) == hostname and r.hw_address != mac), None
        )
        if conflict:
            raise ServiceError(
                f"Name '{name}' ist bereits für {conflict.hw_address} reserviert ('{conflict.hostname}')"
            )

    with step("Kea-Subnetz ermitteln"):
        subnets = await api.kea_subnets()
        subnet = next(
            (s for s in subnets if ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(s.cidr)),
            None,
        )
        if subnet is None:
            known = ", ".join(s.cidr for s in subnets) or "keine"
            raise ServiceError(f"Kein Kea-Subnetz in OPNsense enthält {ip} (vorhanden: {known})")

    if existing:
        if (
            existing.ip_address == ip
            and existing.hostname == name
            and existing.subnet == subnet.uuid
            and existing.description == description
        ):
            progress(f"Reservierung existiert bereits: {mac} → {ip} ({name})")
            result = ReservationResult(ip, hostname, mac, existing.uuid, changed=False, name=name)
            _remember(ctx, result)
            return result
        with step("Reservierung ändern"):
            await api.kea_set_reservation(existing.uuid, subnet.uuid, ip, mac, name, description)
        uuid = existing.uuid
        progress(f"Reservierung geändert: {mac} → {ip} ({name})")
    else:
        with step("Reservierung anlegen"):
            uuid = await api.kea_add_reservation(subnet.uuid, ip, mac, name, description)
        progress(f"Reservierung angelegt: {mac} → {ip} ({name})")

    with step("Kea-DHCP neu konfigurieren"):
        await api.kea_reconfigure()
    progress("Kea-DHCP neu konfiguriert")
    await _reload_dns(ctx, api, hostname, progress)
    result = ReservationResult(ip, hostname, mac, uuid, changed=True, name=name)
    _remember(ctx, result)
    return result


async def apply_reservation(
    ctx: Context, current_ip: str, result: ReservationResult, progress: Progress = noop_progress
) -> None:
    """Gerät neu starten, damit es die reservierte IP bezieht; Inventar auf neue IP setzen."""
    if current_ip == result.ip:
        return
    with step(f"Neue IP {result.ip} anwenden"):
        progress(f"Neustart, damit das Gerät die reservierte IP {result.ip} bezieht")
        device = ctx.store.find_device(mac=result.mac)  # über die MAC, nicht über eine evtl. veraltete IP
        with step(f"Neustart unter {current_ip}"):
            await ctx.shelly(current_ip, device).reboot()
        with step(f"Gerät unter {result.ip} abwarten"):
            await ctx.sleep(ctx.settings.poll_interval)
            await wait_for_device(
                ctx,
                result.ip,
                timeout=ctx.settings.reboot_timeout,
                what="Neue IP:",
                condition=lambda info: info.mac_colon == result.mac,
            )
        if device:
            device.ip = result.ip
            ctx.store.upsert_device(device)
        progress(f"Gerät unter {result.ip} erreichbar")


async def _reload_dns(ctx: Context, api: OPNsenseClient, hostname: str, progress: Progress) -> None:
    """Unbound neu laden, damit der Hostname auflösbar wird. Ein Fehler hier bricht die Reservierung nicht ab."""
    if not ctx.settings.opnsense_reload_unbound:
        return
    try:
        with step("Unbound-DNS neu laden"):  # Fehler wird als fehlgeschlagener Schritt geloggt
            await api.unbound_reconfigure()
    except OPNsenseError as exc:
        progress(f"Hinweis: DNS-Name {hostname} ist evtl. noch nicht auflösbar – Unbound nicht neu geladen ({exc})")
        return
    progress("Unbound-DNS neu geladen")


def _remember(ctx: Context, result: ReservationResult) -> None:
    device = ctx.store.find_device(mac=result.mac)
    if device:
        device.hostname = result.hostname
        device.name = result.name or device.name
        ctx.store.upsert_device(device)
