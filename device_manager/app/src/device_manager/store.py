"""SQLite-Persistenz: Geräte-Inventar und Aktionsprotokoll."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    key TEXT PRIMARY KEY,           -- MAC (aa:bb:..) oder 'z2m:<ieee>' für Zigbee-Geräte ohne bekannte MAC
    mac TEXT,
    device_id TEXT,
    name TEXT,
    model TEXT,
    app TEXT,
    gen INTEGER,
    firmware TEXT,
    ip TEXT,
    hostname TEXT,
    connection TEXT NOT NULL DEFAULT 'wifi',   -- wifi | zigbee
    z2m_name TEXT,
    ieee TEXT,
    cert_not_after TEXT,
    auth_mode TEXT,                 -- device (abgeleitetes Passwort) | shared (Master-Passwort, Altbestand) | none
    online INTEGER,                 -- letzte Statusabfrage: 1 erreichbar, 0 nicht erreichbar, NULL nie abgefragt
    checked_at TEXT,                -- Zeitpunkt der letzten Statusabfrage
    fw_stable TEXT,                 -- verfügbare Version im Kanal stable ('' = keine neuere, NULL = unbekannt)
    fw_beta TEXT,                   -- dito für beta
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    device_key TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL,           -- ok | error
    message TEXT NOT NULL
);
"""
# F12 Gerätestatus: nachträglich ergänzte Spalten
STATUS_COLUMNS = {"online": "INTEGER", "checked_at": "TEXT", "fw_stable": "TEXT", "fw_beta": "TEXT"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Device:
    key: str
    mac: str | None = None
    device_id: str | None = None
    name: str | None = None
    model: str | None = None
    app: str | None = None
    gen: int | None = None
    firmware: str | None = None
    ip: str | None = None
    hostname: str | None = None
    connection: str = "wifi"
    z2m_name: str | None = None
    ieee: str | None = None
    cert_not_after: str | None = None
    auth_mode: str | None = None
    online: int | None = None
    checked_at: str | None = None
    fw_stable: str | None = None
    fw_beta: str | None = None
    updated_at: str = field(default_factory=now)


@dataclass
class Action:
    id: int
    ts: str
    device_key: str | None
    action: str
    status: str
    message: str


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Fehlende Spalten in bestehenden Datenbanken ergänzen."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(devices)")}
        if "auth_mode" not in existing:
            self._conn.execute("ALTER TABLE devices ADD COLUMN auth_mode TEXT")
            # Bis v0.1 wurde das gemeinsame Passwort gesetzt -> Bestandsgeräte als 'shared' führen
            self._conn.execute("UPDATE devices SET auth_mode = 'shared' WHERE mac IS NOT NULL AND connection = 'wifi'")
            self._conn.commit()
        for column, sql_type in STATUS_COLUMNS.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE devices ADD COLUMN {column} {sql_type}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # Geräte -----------------------------------------------------------------
    def upsert_device(self, device: Device) -> Device:
        device.updated_at = now()
        data = asdict(device)
        cols = ", ".join(data)
        placeholders = ", ".join(f":{c}" for c in data)
        updates = ", ".join(f"{c}=excluded.{c}" for c in data if c != "key")
        with self._lock, self._conn:
            if device.ip:  # eine IP gehört immer nur einem Gerät – veraltete Zuordnungen entfernen
                self._conn.execute("UPDATE devices SET ip = NULL WHERE ip = ? AND key != ?", (device.ip, device.key))
            self._conn.execute(
                f"INSERT INTO devices ({cols}) VALUES ({placeholders}) ON CONFLICT(key) DO UPDATE SET {updates}",  # noqa: S608
                data,
            )
        return device

    def get_device(self, key: str) -> Device | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM devices WHERE key = ?", (key,)).fetchone()
        return Device(**dict(row)) if row else None

    def find_device(
        self,
        *,
        mac: str | None = None,
        ieee: str | None = None,
        ip: str | None = None,
        device_id: str | None = None,
    ) -> Device | None:
        criteria = {"mac": mac, "ieee": ieee, "ip": ip, "device_id": device_id}
        column, value = next(((c, v) for c, v in criteria.items() if v), ("mac", None))
        if value is None:
            return None
        with self._lock:
            row = self._conn.execute(f"SELECT * FROM devices WHERE {column} = ?", (value,)).fetchone()  # noqa: S608
        return Device(**dict(row)) if row else None

    def list_devices(self) -> list[Device]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM devices ORDER BY COALESCE(hostname, name, key)").fetchall()
        return [Device(**dict(r)) for r in rows]

    def delete_device(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM devices WHERE key = ?", (key,))

    def rekey_device(self, old_key: str, new_key: str) -> None:
        """Zigbee-Gerät bekommt nach dem ersten WLAN-Kontakt die MAC als Schlüssel."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM devices WHERE key = ?", (new_key,))
            self._conn.execute("UPDATE devices SET key = ? WHERE key = ?", (new_key, old_key))
            self._conn.execute("UPDATE actions SET device_key = ? WHERE device_key = ?", (new_key, old_key))

    # Protokoll ----------------------------------------------------------------
    def log_action(self, device_key: str | None, action: str, status: str, message: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO actions (ts, device_key, action, status, message) VALUES (?, ?, ?, ?, ?)",
                (now(), device_key, action, status, message),
            )

    def actions(self, device_key: str | None = None, limit: int = 200) -> list[Action]:
        query = "SELECT * FROM actions"
        params: list[Any] = []
        if device_key:
            query += " WHERE device_key = ?"
            params.append(device_key)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [Action(**dict(r)) for r in rows]
