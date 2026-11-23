"""F4 – Firmware-Update.

Ablauf nach `Shelly.Update`: Die Shelly-API bietet keine abfragbare Information, ob ein Update noch läuft.
Deshalb wird im Intervall `firmware_poll_interval` die Version, die Uptime und `restart_required` abgefragt:
- neue Version gemeldet → fertig,
- Uptime kleiner als zuvor bei unveränderter Version → das Gerät ist mit der alten Firmware neu gestartet → Fehler,
- kein Neustart nach `firmware_reboot_wait` → der Anwender wird um einen Neustart gebeten (Button im Job).
  Der Manager startet nie selbst neu, damit das Gerät nicht mitten im Schreiben der Firmware unterbrochen wird.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..context import Context
from ..jobs import Progress, clear_user_action, request_user_action
from ..shelly.client import DeviceInfo, ShellyClient, ShellyError, ShellyUnreachable
from ..store import Device
from .common import ServiceError, device_from_info, noop_progress, step

log = logging.getLogger(__name__)

STAGES = ("stable", "beta")
PRERELEASE = re.compile(r"(beta|rc|alpha)", re.IGNORECASE)
PROGRESS_EVERY = 60.0  # s – Zwischenstand im Job höchstens einmal pro Minute


@dataclass(frozen=True)
class FirmwareResult:
    old_version: str
    new_version: str
    updated: bool


async def available_updates(ctx: Context, host: str) -> dict[str, str]:
    return await ctx.shelly(host).check_for_update()


async def update_firmware(
    ctx: Context,
    host: str,
    stage: str | None = None,
    progress: Progress = noop_progress,
    device: Device | None = None,
) -> FirmwareResult:
    stage = stage or ctx.settings.default_firmware_stage
    with step(f"Firmware-Update {host} ({stage})"):
        if stage not in STAGES:
            raise ServiceError(f"Unbekannter Firmware-Kanal: {stage}")
        client = ctx.shelly(host, device)
        with step("Aktuelle Firmware abfragen"):
            info = await client.get_device_info()
        progress(f"Aktuelle Firmware: {info.ver}")
        with step("Verfügbare Updates prüfen"):
            available = await client.check_for_update()
        target = available.get(stage)
        if not target:
            progress(f"Keine neuere Firmware im Kanal '{stage}' verfügbar")
            _store(ctx, host, info)
            return FirmwareResult(info.ver, info.ver, updated=False)
        if stage == "stable" and PRERELEASE.search(target):
            progress(f"Hinweis: Das Gerät bietet im Kanal 'stable' die Vorabversion {target} an – wird installiert")

        uptime_before = await _uptime(client)
        progress(f"Starte Update auf {target} ({stage})")
        with step(f"Update auf {target} anstoßen"):
            await client.update(stage)
        progress("Update läuft – warte auf Neustart mit neuer Firmware …")
        with step("Update und Neustart abwarten"):
            new_info = await _await_new_firmware(ctx, client, info, target, uptime_before, progress)
        if new_info.ver != target:
            progress(f"Hinweis: erwartet {target}, Gerät meldet {new_info.ver}")
        progress(f"Firmware aktualisiert: {info.ver} → {new_info.ver}")
        _store(ctx, host, new_info)
        return FirmwareResult(info.ver, new_info.ver, updated=True)


async def _uptime(client: ShellyClient) -> float | None:
    try:
        value = (await client.sys_status()).get("uptime")
    except ShellyError as exc:
        log.debug("%s: Sys.GetStatus nicht abrufbar (%s)", client.host, exc)
        return None
    return float(value) if isinstance(value, int | float) else None


async def _await_new_firmware(
    ctx: Context,
    client: ShellyClient,
    before: DeviceInfo,
    target: str,
    last_uptime: float | None,
    progress: Progress,
) -> DeviceInfo:
    s = ctx.settings
    host = client.host
    interval = s.firmware_poll_interval
    deadline = s.firmware_reboot_wait + s.firmware_timeout
    waited = 0.0
    asked = False
    restart_hint = False
    next_report = PROGRESS_EVERY
    state = "noch keine Antwort"
    try:
        while waited < deadline:
            await ctx.sleep(interval)
            waited += interval
            try:
                info = await client.get_device_info()
            except ShellyUnreachable:
                state = "nicht erreichbar (Neustart läuft vermutlich)"
                log.debug("Update %s nach %.0fs: %s", host, waited, state)
                continue
            if info.ver != before.ver:
                log.info("Update %s: neue Firmware %s nach %.0fs", host, info.ver, waited)
                return info

            status = await _status(client)
            uptime = status.get("uptime")
            restart_required = bool(status.get("restart_required"))
            if isinstance(uptime, int | float):
                if last_uptime is not None and uptime < last_uptime:
                    raise ServiceError(
                        f"{host} ist neu gestartet (Uptime {uptime:.0f}s), meldet aber weiterhin Firmware {before.ver} "
                        f"– das Update auf {target} wurde nicht übernommen. Bitte erneut versuchen oder im "
                        "Shelly-Webinterface prüfen."
                    )
                last_uptime = float(uptime)
            state = f"erreichbar, FW {info.ver}, Uptime {uptime if uptime is not None else '?'}s" + (
                ", restart_required" if restart_required else ""
            )
            log.debug("Update %s nach %.0fs: %s", host, waited, state)

            if restart_required and not restart_hint:
                restart_hint = True
                progress("Gerät meldet 'restart_required' – Update vermutlich installiert, Neustart steht aus")
            if waited >= next_report and not asked:
                next_report += PROGRESS_EVERY
                progress(f"Update läuft noch ({waited / 60:.0f} min, {state})")
            if waited >= s.firmware_reboot_wait and not asked:
                asked = True
                _ask_for_reboot(client, before, waited, restart_required, progress)
    finally:
        clear_user_action()
    reason = "auch nach der Aufforderung nicht neu gestartet" if asked else "nicht mit neuer Firmware zurückgekehrt"
    raise ServiceError(
        f"Firmware-Update: {host} nach {waited:.0f}s {reason} (zuletzt: {state}). "
        f"Bitte im Shelly-Webinterface prüfen, ob {target} installiert ist."
    )


async def _status(client: ShellyClient) -> dict[str, object]:
    try:
        return await client.sys_status()
    except ShellyError as exc:
        log.debug("%s: Sys.GetStatus fehlgeschlagen (%s)", client.host, exc)
        return {}


def _ask_for_reboot(
    client: ShellyClient, before: DeviceInfo, waited: float, restart_required: bool, progress: Progress
) -> None:
    hint = (
        "Das Gerät meldet 'restart_required', das Update ist vermutlich abgeschlossen."
        if restart_required
        else "Bitte vorher im Shelly-Webinterface prüfen, dass das Update abgeschlossen ist."
    )
    message = (
        f"{client.host} hat sich {waited / 60:.0f} Minuten nach dem Update nicht neu gestartet "
        f"(Firmware weiterhin {before.ver}). {hint} Danach das Gerät neu starten – per Button oder am Gerät."
    )
    progress(f"AKTION ERFORDERLICH: {message}")
    request_user_action(message, "Gerät jetzt neu starten", _reboot_action(client))


def _reboot_action(client: ShellyClient) -> Callable[[], Awaitable[str]]:
    async def run() -> str:
        await client.reboot()
        return f"Neustart von {client.host} durch Anwender ausgelöst – warte auf neue Firmware …"

    return run


def _store(ctx: Context, host: str, info) -> None:  # type: ignore[no-untyped-def]
    existing = ctx.store.find_device(mac=info.mac_colon)
    if existing and existing.connection == "zigbee":
        existing.firmware = info.ver
        ctx.store.upsert_device(existing)
    else:
        ctx.store.upsert_device(device_from_info(info, host, existing))
