from __future__ import annotations

import logging
import re
from collections.abc import Callable

from ..context import Context
from ..jobs import Progress
from ..shelly.client import DeviceInfo, ShellyError, ShellyUnreachable, format_mac
from ..steps import step
from ..store import Device

log = logging.getLogger(__name__)

HOSTNAME_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class ServiceError(Exception):
    """Fachlicher Fehler, der dem Nutzer angezeigt wird."""


def lease_mac(lease: dict[str, object]) -> str | None:
    """MAC einer Kea-Lease normalisiert ('aa:bb:..') oder None bei ungültigem Wert."""
    try:
        return format_mac(str(lease.get("hwaddr", "")))
    except ValueError:
        return None


def normalize_hostname(value: str) -> str:
    """'Kueche-Licht.home.lan' -> 'kueche-licht' (Vergleich mit Reservierungen von außerhalb)."""
    return value.strip().lower().rstrip(".").split(".", 1)[0]


def noop_progress(_: str) -> None:
    return None


def validate_name(name: str) -> str:
    """Gerätename: wie ein Hostname, aber Großbuchstaben erlaubt und erhalten ('Kueche-Licht')."""
    name = name.strip()
    if "." in name:
        raise ServiceError(f"Ungültiger Name '{name}': bitte ohne Domain eingeben, z. B. '{name.split('.')[0]}'")
    if not HOSTNAME_RE.match(name.lower()):
        raise ServiceError(
            f"Ungültiger Name '{name}': erlaubt sind nur Buchstaben a–z/A–Z, 0–9 und Bindestrich (keine Umlaute "
            "oder Leerzeichen), nicht mit Bindestrich beginnen oder enden, max. 63 Zeichen"
        )
    return name


def validate_hostname(hostname: str) -> str:
    """Hostname für DNS/Zertifikat: der Name in Kleinbuchstaben."""
    return validate_name(hostname).lower()


def fqdn(ctx: Context, hostname: str) -> str:
    domain = ctx.settings.dns_domain.strip(".")
    return f"{hostname}.{domain}" if domain else hostname


async def wait_for_device(
    ctx: Context,
    host: str,
    *,
    timeout: float,
    condition: Callable[[DeviceInfo], bool] = lambda _: True,
    progress: Progress = noop_progress,
    what: str = "Gerät",
) -> DeviceInfo:
    """Fragt das Gerät ab, bis es erreichbar ist und `condition` erfüllt (z. B. nach Neustart/Update)."""
    client = ctx.shelly(host)
    waited = 0.0
    interval = ctx.settings.poll_interval
    attempts = 0
    last_state = "noch keine Antwort"
    log.debug("Warte auf %s (%s, max. %.0fs)", host, what, timeout)
    while waited <= timeout:
        attempts += 1
        try:
            info = await client.get_device_info()
            if condition(info):
                log.debug("%s erreichbar nach %d Abfrage(n), FW %s", host, attempts, info.ver)
                return info
            last_state = f"erreichbar, aber Bedingung nicht erfüllt (FW {info.ver}, MAC {info.mac_colon})"
        except ShellyUnreachable as exc:
            last_state = str(exc)
        log.debug("Warte auf %s – Abfrage %d: %s", host, attempts, last_state)
        await ctx.sleep(interval)
        waited += interval
    raise ServiceError(
        f"{what} {host} nach {timeout:.0f}s nicht im erwarteten Zustand ({attempts} Abfragen, zuletzt: {last_state})"
    )


def device_from_info(info: DeviceInfo, ip: str, existing: Device | None) -> Device:
    device = existing or Device(key=info.mac_colon)
    device.mac = info.mac_colon
    device.device_id = info.id
    device.name = info.name or device.name  # gespeicherten Namen behalten, solange das Gerät keinen meldet
    device.model = info.model
    device.app = info.app
    device.gen = info.gen
    device.firmware = info.ver
    device.ip = ip
    return device


__all__ = [
    "ServiceError",
    "ShellyError",
    "device_from_info",
    "fqdn",
    "lease_mac",
    "normalize_hostname",
    "noop_progress",
    "step",
    "validate_hostname",
    "validate_name",
    "wait_for_device",
]
