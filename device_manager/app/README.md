# Device Manager

Verwaltung von Shelly-Geräten (Gen2, Gen3, Gen4) mit Weboberfläche. Läuft lokal oder als Home-Assistant-App.

- **Übernehmen** von Geräten, die per Shelly-App oder Taster ins WLAN gebracht wurden: prüft, dass Geräte-ID, MAC und DHCP-Lease zusammenpassen, und setzt ein eigenes Passwort pro Gerät
- **IP-Reservierung** in OPNsense (Kea DHCP): nächste freie IP aus einem Bereich, optional sofort per Neustart anwenden;
  Beschreibung = Produktname (z. B. „Shelly Plug M Gen 3“), Unbound wird neu geladen, damit der Hostname sofort auflösbar ist
- **Firmware-Update** (stable oder beta). Startet ein Gerät nach dem Update nicht von selbst neu, bittet der Manager
  nach 5 Minuten um einen Neustart per Button – er startet nie selbst mitten im Update neu
- **Standardeinstellungen** beim Einrichten: Gerätename, Zeitzone, Standort, NTP, Datumsformat, Eco-Modus, AP und Bluetooth aus, LED aus;
  NTP-Server per Button auf alle Geräte verteilen
- **HTTPS-Zertifikat**: von der internen CA in OPNsense ausstellen und auf dem Shelly installieren (FW ≥ 2.0.0)
- **Gen4 im Zigbee-Modus**: WLAN über Zigbee2MQTT einschalten, Firmware aktualisieren, WLAN wieder ausschalten
- **Assistent** „Neues Gerät“: alle Schritte in einem Durchlauf

## Lokal starten

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Linux/macOS: .venv/bin/pip
cp .env.example .env                        # Werte eintragen
.venv/Scripts/python -m device_manager      # http://127.0.0.1:8099
```

### Passwörter verschlüsselt in der `.env` (Windows)

```bash
.venv/Scripts/python -m device_manager.envcrypt DM_SHELLY_PASSWORD
# Wert: ********   Wiederholen: ********
# DM_SHELLY_PASSWORD=dpapi:AQAAANCMnd8BFdERjHoAwE/Cl+s...
```

Die ausgegebene Zeile in die `.env` eintragen, für jedes Secret einzeln: `DM_SHELLY_PASSWORD`, `DM_WIFI_PASSWORD`,
`DM_OPNSENSE_API_KEY`, `DM_OPNSENSE_API_SECRET`, `DM_MQTT_PASSWORD`. Die Werte sind mit Windows DPAPI verschlüsselt
und nur für dein Windows-Benutzerkonto auf diesem Rechner lesbar. Eine kopierte `.env` ist woanders wertlos.
Beim Wechsel des Rechners die Werte neu verschlüsseln. In Home Assistant gilt das nicht, dort trägst du die Werte
in der App-Konfiguration ein.

### Log

Jeder Arbeitsschritt erscheint im Log mit Start, Ende und Dauer. Bei einem Fehler steht dort, in welchem Schritt er
aufgetreten ist, mit Ursache und Traceback. Die Log-Datei liegt unter `data/logs/device_manager.log`.
Mehr Details (jeder Aufruf an Shelly, OPNsense und Zigbee2MQTT) mit `DM_LOG_LEVEL=debug` oder `--log-level debug`.
Passwörter erscheinen nie im Log.

## Tests

```bash
.venv/Scripts/python -m pytest              # alle Anforderungen, ohne Hardware
.venv/Scripts/python -m pytest -m docker    # MQTT-Broker + Docker-Build (Docker nötig)
.venv/Scripts/python -m pytest -m live      # lesende Smoke-Tests gegen echte OPNsense
```

## Home-Assistant-App

Verteilt wird die App über das App-Repository [macpo-homelab/ha-apps](https://github.com/macpo-homelab/ha-apps), das in Home Assistant
unter App-Store → Repositories eingetragen ist. Home Assistant baut die App daraus selbst.

```bash
git clone https://github.com/macpo-homelab/ha-apps ../ha-apps                      # einmalig
python scripts/publish_addon.py ../ha-apps --commit   # App-Ordner + repository.yaml erzeugen, committen
git -C ../ha-apps push
```

Danach in Home Assistant App-Store → ⋮ → „Nach Updates suchen“. Nur für Tests ohne Repository:
`python scripts/build_addon.py` und `addon/device_manager` nach `/addons/device_manager` kopieren (lokale App). Konfiguration und OPNsense-Voraussetzungen stehen in [addon/device_manager/DOCS.md](addon/device_manager/DOCS.md).

## Dokumentation

- [Anforderungsanalyse](docs/01-anforderungsanalyse.md)
- [Technologieauswahl](docs/02-technologieauswahl.md)
- [Testkonzept und Traceability](docs/03-testkonzept.md)
- [Security-Review](docs/04-security-review.md)

## Lizenz

MIT, siehe [LICENSE](LICENSE). Mitgelieferte Fremdkomponenten (htmx, IBM Plex) und Markenhinweise:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Das Projekt ist unabhängig und nicht mit Shelly Group,
Nabu Casa / Home Assistant oder Deciso / OPNsense verbunden.

Gefällt dir das Projekt? Über [GitHub Sponsors](https://github.com/sponsors/macpo) kannst du die Entwicklung unterstützen.
