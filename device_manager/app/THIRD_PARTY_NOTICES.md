# Fremdkomponenten

Der Device Manager selbst steht unter der [MIT-Lizenz](LICENSE). Folgende mitgelieferte Bestandteile stehen unter
eigenen Lizenzen:

| Komponente | Ort | Lizenz |
|---|---|---|
| [htmx](https://htmx.org) | `src/device_manager/web/static/htmx.min.js` | BSD Zero-Clause License (0BSD) |
| [IBM Plex Sans / Mono](https://github.com/IBM/plex) | `src/device_manager/web/static/fonts/` | SIL Open Font License 1.1, siehe [OFL.txt](src/device_manager/web/static/fonts/OFL.txt) |

Python-Abhängigkeiten (FastAPI, Uvicorn, HTTPX, Jinja2, Pydantic, paho-mqtt, zeroconf, cryptography u. a.) werden
nicht mitgeliefert, sondern bei der Installation aus PyPI geladen und stehen unter ihren eigenen Lizenzen.
Das Basis-Image der HA-App stammt von Home Assistant (`ghcr.io/home-assistant/*-base-python`).

## Marken

„Shelly“ ist eine Marke der Shelly Group, „Home Assistant“ der Nabu Casa, Inc., „OPNsense“ der Deciso B.V.
Dieses Projekt ist unabhängig und mit keinem dieser Unternehmen verbunden.
