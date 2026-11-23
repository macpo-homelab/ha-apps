"""F6 – Gen4 im Zigbee-Modus: WLAN per Zigbee2MQTT aktivieren, Firmware aktualisieren, WLAN wieder deaktivieren."""

from __future__ import annotations

from dataclasses import dataclass

from ..context import Context
from ..jobs import Progress
from ..shelly.client import DeviceInfo
from ..store import Device
from ..z2m.client import Z2MError
from .common import ServiceError, device_from_info, noop_progress, step, wait_for_device
from .firmware import FirmwareResult, update_firmware
from .identity import establish_auth, verify_identity

DISABLE_ATTEMPTS = 5


@dataclass(frozen=True)
class ZigbeeUpdateResult:
    ip: str
    mac: str
    firmware: FirmwareResult
    wifi_disabled: bool


async def zigbee_firmware_update(
    ctx: Context, device_key: str, stage: str | None = None, progress: Progress = noop_progress
) -> ZigbeeUpdateResult:
    with step(f"Zigbee-Firmware-Update {device_key}"):
        return await _zigbee_update(ctx, device_key, stage, progress)


async def _zigbee_update(ctx: Context, device_key: str, stage: str | None, progress: Progress) -> ZigbeeUpdateResult:
    s = ctx.settings
    device = ctx.store.get_device(device_key)
    if not device or device.connection != "zigbee" or not device.z2m_name:
        raise ServiceError(f"{device_key} ist kein Zigbee-Gerät aus Zigbee2MQTT")
    if not s.wifi_ssid or not s.wifi_password.get_secret_value():
        raise ServiceError("WLAN-Zugangsdaten (wifi_ssid/wifi_password) sind nicht konfiguriert")

    z2m = ctx.z2m()
    name = device.z2m_name
    ssid, password = s.wifi_ssid, s.wifi_password.get_secret_value()

    result: FirmwareResult | None = None
    ip = mac = ""
    error: BaseException | None = None
    try:
        with step(f"WLAN auf '{name}' aktivieren"):
            await z2m.set_wifi(name, True, ssid, password)
        progress(f"WLAN auf '{name}' aktiviert (SSID {ssid}), warte auf IP …")
        with step("IP-Adresse über Zigbee2MQTT abwarten"):
            ip = await z2m.wait_for_ip(name, s.zigbee_wifi_timeout, s.poll_interval)
        progress(f"Gerät im WLAN mit IP {ip}")
        with step(f"Gerät unter {ip} erreichen"):
            info = await wait_for_device(ctx, ip, timeout=s.reboot_timeout, what="WLAN-Verbindung:")
        with step("Identität prüfen"):
            await verify_identity(ctx, ip, info)
            if device.mac and device.mac != info.mac_colon:
                raise ServiceError(f"{ip} meldet MAC {info.mac_colon}, erwartet {device.mac}")
        mac = info.mac_colon
        # Immer gegen den tatsächlichen Gerätezustand prüfen (z. B. nach Werksreset wieder Passwort setzen)
        with step("Passwort einrichten"):
            auth_mode = await establish_auth(ctx, ip, info, progress)
        device = _merge(ctx, device_key, info, ip, auth_mode)
        result = await update_firmware(ctx, ip, stage, progress, device)
    except BaseException as exc:
        error = exc
        raise
    finally:
        if mac:
            _forget_ip(ctx, mac)
        with step(f"WLAN auf '{name}' deaktivieren"):
            disabled = await _disable_wifi(ctx, name, ssid, password, progress)
            if not disabled and error is None:
                raise ServiceError(f"WLAN auf '{name}' konnte nicht deaktiviert werden – bitte manuell prüfen")
    assert result is not None
    return ZigbeeUpdateResult(ip, mac, result, wifi_disabled=disabled)


def _merge(ctx: Context, device_key: str, info: DeviceInfo, ip: str, auth_mode: str) -> Device:
    """Zigbee-Eintrag mit MAC/Infos anreichern; Schlüssel wird die MAC. IP nur während des Jobs gesetzt."""
    if device_key != info.mac_colon:
        ctx.store.rekey_device(device_key, info.mac_colon)
    device = device_from_info(info, ip, ctx.store.get_device(info.mac_colon))
    device.connection = "zigbee"
    device.auth_mode = auth_mode
    return ctx.store.upsert_device(device)


def _forget_ip(ctx: Context, mac: str) -> None:
    """Im Normalbetrieb ist das Zigbee-Gerät nicht per WLAN erreichbar."""
    device = ctx.store.find_device(mac=mac)
    if device and device.ip:
        device.ip = None
        ctx.store.upsert_device(device)


async def _disable_wifi(ctx: Context, name: str, ssid: str, password: str, progress: Progress) -> bool:
    z2m = ctx.z2m()
    for attempt in range(1, DISABLE_ATTEMPTS + 1):
        try:
            await z2m.set_wifi(name, False, ssid, password)
            if not await z2m.wifi_enabled(name):
                progress("WLAN wieder deaktiviert")
                return True
        except Z2MError as exc:
            progress(f"WLAN deaktivieren, Versuch {attempt}: {exc}")
        await ctx.sleep(ctx.settings.poll_interval)
    progress("FEHLER: WLAN konnte nicht deaktiviert werden")
    return False
