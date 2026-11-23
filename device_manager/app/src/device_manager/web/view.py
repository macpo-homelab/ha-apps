"""Aufbereitung für die Oberfläche: Gerätezustand (F12), Zertifikatsstatus, Protokolleinträge."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..store import Action, Device

CERT_WARN_DAYS = 30
MESSAGE_SHORT = 160
CAUSE_SEPARATOR = " <- Ursache: "

# Reihenfolge = Schwere; die LED zeigt die schwerste Meldung
LEVELS = ("unknown", "ok", "warn", "error")


@dataclass(frozen=True)
class CertState:
    level: str  # ok | warn | error | none
    label: str
    date: str = ""


@dataclass
class DeviceState:
    level: str = "unknown"
    label: str = "nicht abgefragt"
    notes: list[str] = field(default_factory=list)
    update: str = ""  # verfügbare Version im Standardkanal

    def raise_to(self, level: str, note: str) -> None:
        self.notes.append(note)
        if LEVELS.index(level) > LEVELS.index(self.level):
            self.level = level


def _parse(ts: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def cert_state(device: Device, today: datetime | None = None) -> CertState:
    if device.connection == "zigbee":
        return CertState("none", "–")
    if not device.cert_not_after:
        return CertState("warn", "fehlt")
    expires = _parse(device.cert_not_after)
    if expires is None:
        return CertState("none", device.cert_not_after)
    date = expires.strftime("%d.%m.%Y")
    days = (expires - (today or datetime.now(UTC))).days
    if days < 0:
        return CertState("error", f"abgelaufen am {date}", date)
    if days < CERT_WARN_DAYS:
        return CertState("warn", f"läuft in {days} Tagen ab", date)
    return CertState("ok", f"gültig bis {date}", date)


def available_update(device: Device, stage: str) -> str:
    version = device.fw_beta if stage == "beta" else device.fw_stable
    return version if version and version != device.firmware else ""


def device_state(device: Device, stage: str, today: datetime | None = None) -> DeviceState:
    """Zusammengefasster Zustand für die LED: erreichbar, Update verfügbar, Zertifikat."""
    if device.connection == "zigbee":
        return DeviceState("unknown", "Zigbee – Status nur über Zigbee2MQTT")
    if device.online is None:
        state = DeviceState("unknown", "noch nicht abgefragt")
    elif not device.online:
        state = DeviceState("error", "nicht erreichbar")
        state.notes.append("nicht erreichbar")
        return state
    else:
        state = DeviceState("ok", "erreichbar")
    state.update = available_update(device, stage)
    if state.update:
        state.raise_to("warn", f"Firmware {state.update} verfügbar")
    cert = cert_state(device, today)
    if cert.level in ("warn", "error"):
        state.raise_to(cert.level, f"Zertifikat {cert.label}")
    if state.level == "unknown" and state.notes:
        state.level = "warn"
    return state


@dataclass(frozen=True)
class LogEntry:
    ts: str
    device_key: str | None
    device_name: str
    action: str
    ok: bool
    short: str
    detail: str
    known: bool  # Gerät steht (noch) im Inventar -> verlinken


def log_entries(actions: list[Action], devices: list[Device]) -> list[LogEntry]:
    """Protokoll mit Gerätenamen statt Schlüssel; lange Fehlerketten gekürzt, Details aufklappbar."""
    names = {d.key: d.name or d.hostname or d.z2m_name or d.key for d in devices}
    entries = []
    for a in actions:
        first = a.message.split(CAUSE_SEPARATOR, 1)[0]
        short = first if len(first) <= MESSAGE_SHORT else first[: MESSAGE_SHORT - 1].rstrip() + "…"
        detail = a.message if short != a.message else ""
        key = a.device_key or ""
        name = names.get(key, key)
        entries.append(LogEntry(a.ts, a.device_key, name, a.action, a.status == "ok", short, detail, key in names))
    return entries
