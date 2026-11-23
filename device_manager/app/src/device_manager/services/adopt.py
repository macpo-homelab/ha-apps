"""F2 – Ein (per Shelly-App ins WLAN gebrachtes) Gerät übernehmen."""

from __future__ import annotations

from ..context import Context
from ..jobs import Progress
from ..store import Device
from .common import device_from_info, noop_progress, step
from .identity import check_target_ip, establish_auth, verify_identity


async def adopt(ctx: Context, host: str, progress: Progress = noop_progress) -> Device:
    with step(f"Übernehmen {host}"):
        with step("Ziel-IP prüfen"):
            check_target_ip(ctx, host)  # nur private IPv4 im Gerätenetz – vor jeder Anfrage
        with step("Gerät abfragen"):
            # Ohne Zugangsdaten abfragen – Credentials gehen erst nach der Identitätsprüfung an das Gerät.
            info = await ctx.shelly_with(host, "", None).get_device_info()
        progress(f"Gerät erkannt: {info.app or info.model} (Gen{info.gen}, FW {info.ver}, MAC {info.mac_colon})")
        with step("Identität prüfen"):
            lease_checked = await verify_identity(ctx, host, info)
        if lease_checked:
            progress("Identität geprüft (Geräte-ID, MAC, DHCP-Lease)")
        else:
            progress("Identität geprüft (Geräte-ID, MAC) – ohne OPNsense kein Abgleich mit der DHCP-Lease")
        with step("Passwort einrichten"):
            auth_mode = await establish_auth(ctx, host, info, progress)

        with step("Inventar speichern"):
            existing = ctx.store.get_device(info.mac_colon)
            device = device_from_info(info, host, existing)
            device.auth_mode = auth_mode
            ctx.store.upsert_device(device)
        progress(f"Gerät {device.key} im Inventar {'aktualisiert' if existing else 'aufgenommen'}")
        return device
