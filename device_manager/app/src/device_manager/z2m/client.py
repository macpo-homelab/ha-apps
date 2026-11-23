"""Zigbee2MQTT-Zugriff für Shelly-Gen4-Geräte im Zigbee-Modus."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from dataclasses import dataclass
from typing import Any

from .bus import MqttBus

log = logging.getLogger(__name__)


class Z2MError(Exception):
    pass


@dataclass(frozen=True)
class ZigbeeDevice:
    friendly_name: str
    ieee_address: str
    model: str


class Zigbee2Mqtt:
    def __init__(self, bus: MqttBus, base_topic: str = "zigbee2mqtt", timeout: float = 10.0) -> None:
        self.bus = bus
        self.base = base_topic.rstrip("/")
        self.timeout = timeout

    async def shelly_devices(self) -> list[ZigbeeDevice]:
        """Liest die (retained) Geräteliste der Bridge und filtert Shelly-Geräte."""
        log.debug("Z2M: lese %s/bridge/devices", self.base)
        async with self.bus.subscribe(f"{self.base}/bridge/devices") as queue:
            try:
                _, payload = await asyncio.wait_for(queue.get(), self.timeout)
            except TimeoutError as exc:
                raise Z2MError(
                    f"Keine Geräteliste von Zigbee2MQTT erhalten ({self.base}/bridge/devices, {self.timeout:.0f}s) "
                    "– läuft Zigbee2MQTT und stimmt z2m_base_topic?"
                ) from exc
        devices = []
        for entry in json.loads(payload):
            definition = entry.get("definition") or {}
            if str(definition.get("vendor", "")).lower() == "shelly":
                devices.append(ZigbeeDevice(entry["friendly_name"], entry["ieee_address"], definition.get("model", "")))
        return devices

    async def get(self, name: str, attribute: str) -> Any:
        """Fragt ein Attribut per /get ab und wartet auf den Zustand mit diesem Attribut."""
        log.debug("Z2M: %s/get %s", name, attribute)
        async with self.bus.subscribe(f"{self.base}/{name}") as queue:
            await self.bus.publish(f"{self.base}/{name}/get", json.dumps({attribute: ""}))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout
            while (remaining := deadline - loop.time()) > 0:
                try:
                    _, payload = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError:
                    break
                state = json.loads(payload)
                if attribute in state:
                    return state[attribute]
        raise Z2MError(f"{name}: keine Antwort für '{attribute}' innerhalb von {self.timeout:.0f}s")

    async def set(self, name: str, payload: dict[str, Any]) -> None:
        # Nur die Schlüssel loggen – wifi_config enthält das WLAN-Passwort.
        log.debug("Z2M: %s/set %s", name, sorted(payload))
        await self.bus.publish(f"{self.base}/{name}/set", json.dumps(payload))

    async def set_wifi(self, name: str, enabled: bool, ssid: str, password: str) -> None:
        config = {"enabled": enabled, "ssid": ssid, "password": password}
        await self.set(name, {"wifi_config": config})

    async def wifi_enabled(self, name: str) -> bool:
        config = await self.get(name, "wifi_config")
        return bool(config.get("enabled")) if isinstance(config, dict) else False

    async def wait_for_ip(self, name: str, timeout: float, interval: float) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        attempts = 0
        last = "keine Antwort"
        while loop.time() < deadline:
            attempts += 1
            try:
                value = await self.get(name, "ip_address")
                ip = ipaddress.IPv4Address(str(value))
                if not ip.is_unspecified:
                    return str(ip)
                last = f"meldet {value}"
            except Z2MError as exc:
                last = str(exc)
            except ValueError:
                last = "ungültige IP-Adresse gemeldet"
            log.debug("Z2M: %s noch ohne IP (Abfrage %d: %s)", name, attempts, last)
            await asyncio.sleep(interval)
        raise Z2MError(
            f"{name}: hat innerhalb von {timeout:.0f}s keine IP-Adresse im WLAN erhalten "
            f"({attempts} Abfragen, zuletzt: {last}) – SSID/WLAN-Passwort und DHCP prüfen"
        )
