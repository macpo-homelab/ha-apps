"""Logging-Einrichtung: Konsole (in HA: App-Log) und rotierende Log-Datei im Datenverzeichnis.

Konfigurierte Secrets werden in jeder Logzeile – auch in Tracebacks – durch '***' ersetzt
(zusätzliche Absicherung; der Code loggt grundsätzlich keine Request-Bodies oder Passwörter).
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import sys
from pathlib import Path

from .config import Settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LOG_FILE_NAME = "device_manager.log"
MAX_BYTES = 2_000_000
BACKUP_COUNT = 5
MARKER = "_device_manager_handler"


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: list[str], fmt: str = FORMAT) -> None:
        super().__init__(fmt)
        # Längste zuerst, damit Teilstrings eines längeren Secrets dieses nicht zerstückeln
        self._secrets = sorted({s for s in secrets if len(s) >= 3}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def log_file(settings: Settings) -> Path:
    return settings.data_dir / "logs" / LOG_FILE_NAME


def configure_logging(settings: Settings, level: str | None = None) -> Path | None:
    """Richtet Root-Logging ein. Liefert den Pfad der Log-Datei (None, falls nicht anlegbar)."""
    level_name = (level or settings.log_level).upper()
    formatter = RedactingFormatter(settings.secrets())
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, MARKER, False)]:
        root.removeHandler(handler)
        handler.close()

    # Windows-Konsolen (cp1252) können nicht jedes Zeichen darstellen – ersetzen statt Logging-Fehler
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(ValueError, OSError):
            reconfigure(errors="backslashreplace")
    console = logging.StreamHandler(sys.stderr)
    handlers: list[logging.Handler] = [console]
    path: Path | None = log_file(settings)
    try:
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("Log-Datei %s nicht anlegbar: %s", path, exc)
        path = None

    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, MARKER, True)
        root.addHandler(handler)
    root.setLevel(level_name)
    # Bibliotheken nicht mit DEBUG fluten (httpx loggt sonst jede URL doppelt)
    for noisy in ("httpx", "httpcore", "paho", "zeroconf", "asyncio"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
    # uvicorn-Logs über die eigenen Handler (einheitliches Format, auch in der Datei)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True
    # Zugriffslog nur bei debug – die Job-Anzeige fragt jede Sekunde ab und würde das Log fluten
    logging.getLogger("uvicorn.access").setLevel(logging.DEBUG if root.level <= logging.DEBUG else logging.WARNING)
    logging.getLogger(__name__).info("Logging: Level %s%s", level_name, f", Datei {path}" if path else " (nur Konsole)")
    return path
