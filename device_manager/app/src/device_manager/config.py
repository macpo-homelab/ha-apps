"""Konfiguration: lokal über Umgebungsvariablen/.env (Präfix DM_), in Home Assistant über /data/options.json."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from . import envcrypt

HA_OPTIONS_FILE = Path("/data/options.json")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DM_", env_file=".env", extra="ignore")

    # Laufzeit
    data_dir: Path = Path("./data")
    bind_host: str = "127.0.0.1"
    port: int = 8099
    # Log-Level für Konsole und Log-Datei (<data_dir>/logs/device_manager.log). debug = jeder API-Aufruf.
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # Nur diese Client-IPs dürfen zugreifen (HA-Ingress: 172.30.32.2). Leer = alle.
    allowed_clients: list[str] = []
    # Erlaubte Host-Header (Schutz vor DNS-Rebinding und Cross-Site-Requests im lokalen Betrieb).
    # Leer = keine Prüfung (HA: Zugriff nur über Ingress).
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "::1"]

    # Shelly: Master-Passwort; jedes Gerät erhält ein daraus abgeleitetes eigenes Passwort (HMAC mit MAC)
    shelly_password: SecretStr = SecretStr("")
    # Gerätenetze (CIDR), in denen Geräte übernommen werden dürfen. Leer = alle privaten Adressen.
    device_networks: list[str] = []
    shelly_timeout: float = 10.0
    poll_interval: float = 3.0
    reboot_timeout: float = 180.0
    # Firmware-Update: wartet nach dem Neustart-Zeitpunkt (bzw. nach der Aufforderung an den Anwender) noch so lange
    firmware_timeout: float = 600.0
    # Abfrageintervall während des Updates (Version, Uptime, restart_required)
    firmware_poll_interval: float = 10.0
    # Startet das Gerät nach dieser Zeit nicht von selbst neu, wird der Anwender um einen Neustart gebeten
    firmware_reboot_wait: float = 300.0
    default_firmware_stage: Literal["stable", "beta"] = "stable"
    # Standardeinstellungen für neue Geräte (F11); der NTP-Server lässt sich auf alle Geräte verteilen
    shelly_ntp_server: str = "time.cloudflare.com"
    shelly_timezone: str = "Europe/Berlin"
    # Datumsformat der Shelly-Weboberfläche (Sys-Konfiguration ui_data.df)
    shelly_date_format: str = "YYYY-MM-DD"
    # Standort für Sonnenauf-/-untergang; leer = nicht setzen (das Gerät behält seinen Standort)
    shelly_latitude: float | None = Field(None, ge=-90, le=90)
    shelly_longitude: float | None = Field(None, ge=-180, le=180)

    # WLAN (Gen4 über Zigbee)
    wifi_ssid: str = ""
    wifi_password: SecretStr = SecretStr("")

    # OPNsense
    opnsense_url: str = ""
    opnsense_api_key: SecretStr = SecretStr("")
    opnsense_api_secret: SecretStr = SecretStr("")
    opnsense_verify_tls: bool = True
    opnsense_ca_file: str = ""
    # Nach jeder geänderten Reservierung Unbound neu laden, damit der Hostname per DNS auflösbar wird
    opnsense_reload_unbound: bool = True

    # IP-Reservierung
    ip_range_start: str = ""
    ip_range_end: str = ""

    # Zertifikate
    dns_domain: str = ""
    cert_ca_name: str = ""
    cert_lifetime_days: int = 397
    # firewall: Schlüssel bleibt in OPNsense gespeichert (wiederholbar);
    # local: Schlüssel wird nur einmal ausgeliefert und nicht in OPNsense gespeichert.
    cert_key_location: Literal["firewall", "local"] = "firewall"

    # MQTT / Zigbee2MQTT
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: SecretStr = SecretStr("")
    z2m_base_topic: str = "zigbee2mqtt"
    zigbee_wifi_timeout: float = 180.0

    @field_validator("ip_range_start", "ip_range_end")
    @classmethod
    def _valid_ip(cls, v: str) -> str:
        if v:
            ipaddress.IPv4Address(v)
        return v

    @field_validator(
        "shelly_password", "wifi_password", "opnsense_api_key", "opnsense_api_secret", "mqtt_password", mode="before"
    )
    @classmethod
    def _decrypt_secret(cls, v: Any) -> Any:
        """'dpapi:…'-Werte aus der .env entschlüsseln (Windows DPAPI, an Benutzerkonto gebunden)."""
        if isinstance(v, str) and v.startswith(envcrypt.PREFIX):
            return envcrypt.unprotect(v)
        return v

    @field_validator("shelly_latitude", "shelly_longitude", mode="before")
    @classmethod
    def _empty_is_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("device_networks")
    @classmethod
    def _valid_networks(cls, v: list[str]) -> list[str]:
        for net in v:
            ipaddress.IPv4Network(net, strict=False)
        return v

    @field_validator("opnsense_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _range_order(self) -> Settings:
        if (
            self.ip_range_start
            and self.ip_range_end
            and ipaddress.IPv4Address(self.ip_range_start) > ipaddress.IPv4Address(self.ip_range_end)
        ):
            raise ValueError("ip_range_start muss <= ip_range_end sein")
        return self

    # Hilfsfunktionen -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        path = self.data_dir / "device_manager.sqlite3"
        legacy = self.data_dir / "shelly_manager.sqlite3"  # Name bis 0.3.x
        if not path.exists() and legacy.exists():
            for suffix in ("", "-wal", "-shm"):
                old = legacy.with_name(legacy.name + suffix)
                if old.exists():
                    old.rename(path.with_name(path.name + suffix))
        return path

    def secrets(self) -> list[str]:
        """Alle geheimen Werte – z. B. für Tests, dass nichts davon geloggt wird."""
        values = [
            self.shelly_password,
            self.wifi_password,
            self.opnsense_api_key,
            self.opnsense_api_secret,
            self.mqtt_password,
        ]
        return [v.get_secret_value() for v in values if v.get_secret_value()]


def load_settings(options_file: Path | None = None, **overrides: Any) -> Settings:
    """Lädt Einstellungen. Existiert die HA-Options-Datei, haben deren Werte Vorrang vor Umgebungsvariablen."""
    options_file = options_file or Path(os.environ.get("DM_OPTIONS_FILE", HA_OPTIONS_FILE))
    values: dict[str, Any] = {}
    if options_file.is_file():
        raw = json.loads(options_file.read_text(encoding="utf-8"))
        values.update({k: v for k, v in raw.items() if v not in (None, "")})
        values.setdefault("data_dir", str(options_file.parent))
        values.setdefault("bind_host", "0.0.0.0")  # noqa: S104 - in HA hinter Ingress
        values.setdefault("allowed_clients", ["172.30.32.2"])
        # Host-Header setzt der Ingress-Proxy; Schutz erfolgt über allowed_clients + HA-Session
        values.setdefault("allowed_hosts", [])
    values.update(overrides)
    return Settings(**values)
