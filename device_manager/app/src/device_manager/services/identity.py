"""Identitätsprüfung von Geräten und gerätespezifische Passwörter.

Bevor Zugangsdaten an einen Host gehen (SetAuth, Digest-Antworten), wird geprüft, dass er wirklich das
Shelly ist, als das er sich ausgibt:
- Ziel ist eine IPv4-Adresse im Gerätenetz (konfiguriert oder privat),
- die Geräte-ID (= Digest-Realm) endet auf die gemeldete MAC,
- die ID gehört nicht schon einem anderen Gerät im Inventar,
- die MAC stimmt mit der Kea-Lease bzw. -Reservierung dieser IP in OPNsense überein.
"""

from __future__ import annotations

import ipaddress
import logging

from ..context import Context
from ..jobs import Progress
from ..shelly.client import DeviceInfo, ShellyAuthError, ShellyError, derive_password, id_matches_mac
from ..store import Device
from .common import ServiceError, lease_mac, noop_progress, step

log = logging.getLogger(__name__)


def check_target_ip(ctx: Context, host: str) -> ipaddress.IPv4Address:
    try:
        ip = ipaddress.IPv4Address(host)
    except ValueError as exc:
        raise ServiceError(f"'{host}' ist keine IPv4-Adresse – Geräte werden nur per IP übernommen") from exc
    networks = [ipaddress.IPv4Network(n, strict=False) for n in ctx.settings.device_networks]
    if networks:
        if not any(ip in net for net in networks):
            raise ServiceError(f"{ip} liegt nicht in den Gerätenetzen {', '.join(map(str, networks))}")
    elif not ip.is_private or ip.is_loopback or ip.is_unspecified:
        raise ServiceError(f"{ip} ist keine private LAN-Adresse")
    return ip


async def verify_identity(ctx: Context, host: str, info: DeviceInfo) -> bool:
    """Prüft die Identität. Liefert True, wenn zusätzlich die DHCP-Lease/Reservierung abgeglichen wurde."""
    check_target_ip(ctx, host)
    if not id_matches_mac(info.id, info.mac):
        raise ServiceError(f"{host}: Geräte-ID '{info.id}' passt nicht zur MAC {info.mac} – Übernahme abgebrochen")
    other = ctx.store.find_device(device_id=info.id)
    if other is not None and other.mac != info.mac_colon:
        raise ServiceError(f"{host}: Geräte-ID '{info.id}' gehört bereits zu {other.key}")
    log.debug("%s: Geräte-ID %s passt zur MAC %s, nicht doppelt im Inventar", host, info.id, info.mac_colon)
    if ctx.settings.opnsense_url:
        api = ctx.opnsense()
        lease_macs = {lease_mac(lease) for lease in await api.kea_leases() if lease.get("address") == host}
        reserved_macs = {r.hw_address for r in await api.kea_reservations() if r.ip_address == host}
        log.debug(
            "%s: Kea-Lease-MACs %s, Reservierungs-MACs %s", host, sorted(map(str, lease_macs)), sorted(reserved_macs)
        )
        if info.mac_colon not in lease_macs | reserved_macs:
            found = ", ".join(sorted(str(m) for m in lease_macs | reserved_macs if m)) or "keine Lease/Reservierung"
            raise ServiceError(
                f"{host}: gemeldete MAC {info.mac_colon} passt nicht zur DHCP-Lease/Reservierung in OPNsense "
                f"(dort: {found}) – Übernahme abgebrochen"
            )
        return True
    return False


async def verify_inventory_device(ctx: Context, host: str, device: Device) -> DeviceInfo:
    """Prüft, dass unter `host` genau das Inventar-Gerät antwortet (MAC, ID, ID passt zur MAC)."""
    if not device.mac or not device.device_id:
        raise ServiceError(f"{device.key}: MAC oder Geräte-ID unbekannt – Gerät zuerst übernehmen")
    info = await ctx.shelly_with(host, "", None).get_device_info()
    if info.mac_colon != device.mac or info.id != device.device_id or not id_matches_mac(info.id, info.mac):
        raise ServiceError(
            f"{host} meldet ein anderes Gerät ({info.id}, MAC {info.mac_colon}) als im Inventar "
            f"({device.device_id}, MAC {device.mac}) – IP-Adresse geändert?"
        )
    return info


async def establish_auth(ctx: Context, host: str, info: DeviceInfo, progress: Progress = noop_progress) -> str:
    """Setzt das gerätespezifische Passwort bzw. prüft vorhandene Zugangsdaten. Liefert den auth_mode."""
    master = ctx.settings.shelly_password.get_secret_value()
    if not master:
        if info.auth_en:
            raise ServiceError("Gerät ist passwortgeschützt, aber kein Master-Passwort konfiguriert")
        progress("Warnung: kein Master-Passwort konfiguriert – Gerät bleibt ohne Passwort")
        return "none"
    derived = derive_password(master, info.mac)
    if not info.auth_en:
        log.debug("%s: Gerät ohne Passwort – setze gerätespezifisches Passwort", host)
        await ctx.shelly_with(host, "", info.id).set_auth(derived, info.id)
        progress("Gerätespezifisches Passwort gesetzt")
        return "device"
    for mode, password in (("device", derived), ("shared", master)):
        try:
            await ctx.shelly_with(host, password, info.id).check_auth()
        except ShellyAuthError as exc:
            log.debug("%s: Passwort-Variante '%s' abgelehnt (%s)", host, mode, exc)
            continue
        progress("Passwort geprüft" + (" (gemeinsames Passwort – Umstellung empfohlen)" if mode == "shared" else ""))
        return mode
    raise ServiceError(f"{host} ist mit einem anderen Passwort geschützt – bitte am Gerät zurücksetzen oder angleichen")


async def rotate_to_device_password(ctx: Context, device: Device, progress: Progress = noop_progress) -> Device:
    """Altgerät mit gemeinsamem Passwort auf das gerätespezifische Passwort umstellen."""
    with step(f"Passwort umstellen {device.key}"):
        if device.auth_mode != "shared":
            raise ServiceError("Gerät verwendet bereits ein gerätespezifisches Passwort")
        if not device.ip:
            raise ServiceError("IP-Adresse unbekannt")
        with step("Gerät prüfen"):
            await verify_inventory_device(ctx, device.ip, device)
        assert device.mac and device.device_id
        master = ctx.settings.shelly_password.get_secret_value()
        derived = derive_password(master, device.mac)
        with step("Neues Passwort setzen"):
            try:
                await ctx.shelly_with(device.ip, master, device.device_id).set_auth(derived, device.device_id)
            except ShellyError as exc:
                # Antwort evtl. verloren, obwohl das Gerät das Passwort übernommen hat -> unten prüfen
                progress(f"Hinweis: SetAuth ohne Bestätigung ({exc}) – prüfe neues Passwort")
        with step("Neues Passwort prüfen"):
            try:
                await ctx.shelly_with(device.ip, derived, device.device_id).check_auth()
            except ShellyAuthError as exc:
                raise ServiceError(
                    "Umstellung fehlgeschlagen – Gerät verwendet weiterhin das gemeinsame Passwort"
                ) from exc
        device.auth_mode = "device"
        progress("Gerätespezifisches Passwort gesetzt und geprüft")
        return ctx.store.upsert_device(device)
