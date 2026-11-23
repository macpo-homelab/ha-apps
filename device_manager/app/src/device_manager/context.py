"""Zentrale Fabrik für Adapter – Tests ersetzen hier Transporte und Bus durch Simulatoren."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings
from .opnsense.client import OPNsenseClient
from .shelly.client import ShellyClient, derive_password
from .store import Device, Store
from .tls import verify_https
from .z2m.bus import MqttBus
from .z2m.client import Z2MError, Zigbee2Mqtt

TlsVerifier = Callable[..., Awaitable[Any]]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass
class Context:
    settings: Settings
    store: Store
    shelly_transport: httpx.AsyncBaseTransport | None = None
    opnsense_transport: httpx.AsyncBaseTransport | None = None
    mqtt_bus: MqttBus | None = None
    tls_verifier: TlsVerifier = verify_https
    sleep: Sleeper = field(default=asyncio.sleep)

    def _factory(self, transport: httpx.AsyncBaseTransport | None) -> Callable[..., httpx.AsyncClient]:
        def make(**kwargs: Any) -> httpx.AsyncClient:
            if transport is not None:
                kwargs["transport"] = transport
            return httpx.AsyncClient(**kwargs)

        return make

    def device_password(self, device: Device) -> str:
        """Passwort eines Inventar-Geräts: abgeleitet pro Gerät, bei Altbestand das Master-Passwort."""
        master = self.settings.shelly_password.get_secret_value()
        if not master or not device.mac:
            return ""
        if device.auth_mode == "shared":
            return master
        if device.auth_mode == "device":
            return derive_password(master, device.mac)
        return ""  # none / unbekannt: keine Zugangsdaten senden (erst übernehmen)

    def shelly(self, host: str, device: Device | None = None) -> ShellyClient:
        """Client für ein Gerät. Passwort und erwarteter Realm kommen aus dem Inventar (Suche über die IP).

        Unbekannte Hosts erhalten einen Client ohne Zugangsdaten – es werden keine Credentials an
        ungeprüfte Ziele gesendet.
        """
        device = device or self.store.find_device(ip=host)
        password, realm = "", None
        if device is not None and device.mac and device.device_id:
            password, realm = self.device_password(device), device.device_id
        return self.shelly_with(host, password, realm)

    def shelly_with(self, host: str, password: str, realm: str | None) -> ShellyClient:
        return ShellyClient(
            host,
            password,
            expected_realm=realm,
            http_factory=self._factory(self.shelly_transport),
            timeout=self.settings.shelly_timeout,
        )

    def opnsense(self) -> OPNsenseClient:
        s = self.settings
        verify: bool | str = s.opnsense_ca_file if s.opnsense_ca_file else s.opnsense_verify_tls
        return OPNsenseClient(
            s.opnsense_url,
            s.opnsense_api_key.get_secret_value(),
            s.opnsense_api_secret.get_secret_value(),
            verify=verify,
            http_factory=self._factory(self.opnsense_transport),
        )

    def z2m(self) -> Zigbee2Mqtt:
        if self.mqtt_bus is None:
            raise Z2MError("MQTT ist nicht konfiguriert bzw. nicht verbunden")
        return Zigbee2Mqtt(self.mqtt_bus, self.settings.z2m_base_topic)
