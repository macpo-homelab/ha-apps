"""Start: `python -m device_manager` (lokal) bzw. als HA-App."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .config import Settings, load_settings
from .logsetup import configure_logging
from .web.app import create_app

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def warn_host_mismatch(settings: Settings) -> bool:
    """Warnt, wenn auf einer LAN-Adresse gelauscht wird, aber nur Loopback-Hosts erlaubt sind."""
    hosts = {h.lower() for h in settings.allowed_hosts}
    if hosts and settings.bind_host not in LOOPBACK and hosts <= LOOPBACK:
        logging.getLogger("device_manager").warning(
            "bind_host=%s, aber DM_ALLOWED_HOSTS erlaubt nur Loopback – Zugriffe über das LAN werden mit 403 "
            "abgelehnt. Name/IP des Rechners in DM_ALLOWED_HOSTS eintragen.",
            settings.bind_host,
        )
        return True
    return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="device-manager", description="Device Manager Web-UI starten")
    parser.add_argument("--host", help="Bind-Adresse (überschreibt DM_BIND_HOST)")
    parser.add_argument("--port", type=int, help="Port (überschreibt DM_PORT)")
    parser.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error"], help="Log-Level (überschreibt DM_LOG_LEVEL)"
    )
    args = parser.parse_args(argv)

    overrides = {k: v for k, v in {"bind_host": args.host, "port": args.port, "log_level": args.log_level}.items() if v}
    settings = load_settings(**overrides)
    configure_logging(settings)
    warn_host_mismatch(settings)
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.port,
        # Logging ist eingerichtet (Format, Datei, Level, Secret-Schwärzung) – uvicorn nichts überschreiben lassen
        log_level=None,
        log_config=None,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
