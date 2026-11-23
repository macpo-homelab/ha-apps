"""F11 – Standardeinstellungen für neue Geräte und Verteilen des NTP-Servers.

Gesetzt werden (Shelly-API, siehe CLAUDE.md „Externe APIs“):
- Sys.SetConfig: device.name = Name, device.eco_mode = true, location.tz/lat/lon, sntp.server,
  ui_data.df = Datumsformat (nicht dokumentiert; so speichert es die Weboberfläche, am Plug M Gen3 ausgelesen)
- WiFi.SetConfig: ap.pass = Geräte-Passwort, ap.enable = false
- BLE.SetConfig: ab FW 2.0.0 nur rpc.enable = false (Bluetooth-Scan aktiviert die Firmware bei Bedarf selbst),
  davor enable = false
- PLUGS_UI.SetConfig: leds.mode = "off" (dokumentiert für Plug M Gen3; andere Geräte: Hinweis)
Meldet eine Änderung restart_required, wird das Gerät neu gestartet (keine Firmware im Spiel).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..context import Context
from ..jobs import Progress
from ..shelly.client import (
    ShellyAuthError,
    ShellyClient,
    ShellyError,
    ShellyUnreachable,
    parse_version,
)
from ..store import Device
from .common import ServiceError, noop_progress, step, validate_name, wait_for_device

log = logging.getLogger(__name__)

BLE_ENABLE_REMOVED = (2, 0, 0)


@dataclass
class DefaultsResult:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    rebooted: bool = False


async def apply_defaults(
    ctx: Context, host: str, name: str, device: Device, progress: Progress = noop_progress
) -> DefaultsResult:
    """Setzt die Standardeinstellungen auf einem übernommenen Gerät (Inventar-Eintrag erforderlich)."""
    name = validate_name(name)
    with step(f"Standardeinstellungen {host}"):
        if not device.mac or not device.auth_mode:
            raise ServiceError(f"{host} ist kein übernommenes Gerät – zuerst übernehmen")
        s = ctx.settings
        client = ctx.shelly(host, device)
        result = DefaultsResult()
        restart = False

        with step("Name, Zeitzone, Standort, NTP, Datumsformat und Eco-Modus"):
            # ui_data enthält weitere Werte der Weboberfläche (z. B. device_revision) – nur 'df' ändern
            current = await client.rpc("Sys.GetConfig") or {}
            ui_data = dict(current.get("ui_data") or {})
            ui_data["df"] = s.shelly_date_format
            location: dict[str, object] = {"tz": s.shelly_timezone}
            if s.shelly_latitude is not None and s.shelly_longitude is not None:
                location |= {"lat": s.shelly_latitude, "lon": s.shelly_longitude}
            restart |= await client.set_config(
                "Sys",
                {
                    "device": {"name": name, "eco_mode": True},
                    "location": location,
                    "sntp": {"server": s.shelly_ntp_server},
                    "ui_data": ui_data,
                },
            )
        place = f"{location['lat']}/{location['lon']}" if "lat" in location else "nicht gesetzt"
        result.applied.append(
            f"Name {name}, Zeitzone {s.shelly_timezone}, Standort {place}, "
            f"Datumsformat {s.shelly_date_format}, "
            f"NTP {s.shelly_ntp_server}, Eco-Modus an"
        )

        with step("Access Point absichern und deaktivieren"):
            password = ctx.device_password(device) if device.auth_mode == "device" else ""
            ap: dict[str, object] = {"enable": False}
            if password:
                ap["pass"] = password
            restart |= await client.set_config("WiFi", {"ap": ap})
        result.applied.append("Access Point deaktiviert" + (" (mit Geräte-Passwort)" if password else ""))
        if not password:
            result.skipped.append("AP-Passwort: kein gerätespezifisches Passwort vorhanden")

        info = await client.get_device_info()
        legacy_ble = parse_version(info.ver) < BLE_ENABLE_REMOVED
        with step("Bluetooth deaktivieren"):
            ble: dict[str, object] = {"rpc": {"enable": False}}
            if legacy_ble:
                ble["enable"] = False
            ble_restart = await _optional(client, "BLE", ble, "Bluetooth", result)
        if ble_restart is not None:
            restart |= ble_restart
            result.applied.append(
                "Bluetooth deaktiviert"
                if legacy_ble
                else "Bluetooth-RPC deaktiviert (Scan steuert die Firmware selbst)"
            )

        with step("LED-Anzeige ausschalten"):
            led_restart = await _optional(client, "PLUGS_UI", {"leds": {"mode": "off"}}, "LED-Anzeige", result)
        if led_restart is not None:
            restart |= led_restart
            result.applied.append("LED-Anzeige aus")

        if restart:
            with step("Neustart für die Einstellungen"):
                await client.reboot()
                await ctx.sleep(s.poll_interval)
                await wait_for_device(ctx, host, timeout=s.reboot_timeout, what="Neustart:")
            result.rebooted = True

    progress("Standardeinstellungen gesetzt: " + "; ".join(result.applied))
    for item in result.skipped:
        progress(f"Hinweis: {item}")
    if result.rebooted:
        progress("Gerät für die Einstellungen neu gestartet")
    device.name = name
    ctx.store.upsert_device(device)
    return result


async def _optional(
    client: ShellyClient, component: str, config: dict[str, object], label: str, result: DefaultsResult
) -> bool | None:
    """SetConfig für Komponenten, die nicht jedes Gerät hat. None = übersprungen."""
    try:
        return await client.set_config(component, config)
    except (ShellyAuthError, ShellyUnreachable):
        raise
    except ShellyError as exc:
        log.info("%s: %s nicht gesetzt (%s)", client.host, label, exc)
        result.skipped.append(f"{label}: vom Gerät nicht unterstützt ({exc})")
        return None


async def distribute_ntp(ctx: Context, progress: Progress = noop_progress) -> dict[str, str]:
    """NTP-Server aus den Einstellungen auf alle per WLAN erreichbaren Inventar-Geräte schreiben."""
    server = ctx.settings.shelly_ntp_server
    if not server:
        raise ServiceError("Kein NTP-Server konfiguriert (DM_SHELLY_NTP_SERVER)")
    results: dict[str, str] = {}
    with step(f"NTP-Server {server} verteilen"):
        for device in ctx.store.list_devices():
            label = device.name or device.hostname or device.key
            if device.connection == "zigbee" or not device.ip:
                results[device.key] = "übersprungen (nicht per WLAN erreichbar)"
                progress(f"{label}: übersprungen – nicht per WLAN erreichbar")
                continue
            try:
                with step(f"{label} ({device.ip})"):
                    await ctx.shelly(device.ip, device).set_config("Sys", {"sntp": {"server": server}})
            except ShellyError as exc:
                results[device.key] = f"Fehler: {exc}"
                progress(f"{label}: Fehler – {exc}")
                continue
            results[device.key] = "ok"
            progress(f"{label}: NTP-Server {server} gesetzt")
    failed = {k: v for k, v in results.items() if v.startswith("Fehler")}
    done = sum(1 for v in results.values() if v == "ok")
    if failed:
        raise ServiceError(
            f"NTP-Server {server} auf {done} Gerät(en) gesetzt, {len(failed)} fehlgeschlagen: "
            + "; ".join(f"{k}: {v.removeprefix('Fehler: ')}" for k, v in failed.items())
        )
    progress(f"NTP-Server {server} auf {done} Gerät(en) gesetzt")
    return results
