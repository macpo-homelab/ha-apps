# Device Manager

Verwaltet Shelly-Geräte (Gen2, Gen3, Gen4) im Heimnetz:

- Geräte übernehmen, die per Shelly-App oder Taster ins WLAN gebracht wurden. Dabei wird geprüft, dass Geräte-ID, MAC und DHCP-Lease zusammenpassen, und ein gerätespezifisches Passwort gesetzt (abgeleitet aus dem Master-Passwort).
- Feste IP-Adresse als Kea-DHCP-Reservierung in OPNsense anlegen (nächste freie IP aus dem konfigurierten Bereich).
- Firmware aktualisieren (Kanal stable oder beta).
- HTTPS-Zertifikat von der internen CA in OPNsense ausstellen und auf dem Shelly installieren (Firmware ≥ 2.0.0, Voraussetzung: Kea-Reservierung des Geräts, der Hostname muss zur Reservierung passen).
- Gen4-Geräte im Zigbee-Modus: WLAN über Zigbee2MQTT einschalten, Firmware aktualisieren, WLAN wieder ausschalten.

## Installation

[![App-Repository hinzufügen](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmacpo-homelab%2Fha-apps)

Oder von Hand:

1. In Home Assistant: Einstellungen → Apps → App-Store → ⋮ (oben rechts) → Repositories.
2. Die URL `https://github.com/macpo-homelab/ha-apps` eintragen und hinzufügen.
3. „Device Manager“ im App-Store öffnen, installieren (Home Assistant baut die App dabei, das dauert einige Minuten),
   Optionen im Reiter „Konfiguration“ ausfüllen und starten. „In Seitenleiste anzeigen“ blendet die Oberfläche ein.

Neue Versionen erscheinen unter Einstellungen → Updates. Ist ein Update noch nicht sichtbar:
App-Store → ⋮ → Nach Updates suchen.

## Voraussetzungen in OPNsense

1. Einen API-Benutzer mit Key und Secret anlegen (System → Access → Users). Er braucht die Rechte
   „Services: Kea DHCP“, „Services: Unbound DNS“, „System: Certificate Manager“ und „Lobby: Dashboard“.
2. Eine interne CA anlegen (System → Trust → Authorities). Deren Name kommt in `cert_ca_name`.
3. Kea DHCPv4 mit einem Subnetz einrichten, das den IP-Bereich enthält. Damit der Hostname per DNS auflösbar ist, in
   Unbound die Registrierung der DHCP-Reservierungen aktivieren. Die App lädt Unbound nach jeder Reservierung neu
   (`opnsense_reload_unbound`).
4. Ist das Webzertifikat der OPNsense von einer internen CA signiert: das CA-Zertifikat herunterladen
   (System → Trust → Authorities → Download, nur das Zertifikat, nicht den Schlüssel), z. B. per Samba- oder
   File-Editor-App nach `/ssl/opnsense-ca.pem` kopieren und `opnsense_ca_file: /ssl/opnsense-ca.pem` eintragen.

## MQTT

Ist `mqtt_host` leer, nutzt die App den MQTT-Dienst von Home Assistant (z. B. das Mosquitto-Add-on).

## Log

Das App-Log (Reiter „Log“) zeigt jeden Arbeitsschritt mit Start, Ende und Dauer. Bei einem Fehler steht dort,
in welchem Schritt er aufgetreten ist, mit Ursache und Traceback. Mit der Option `log_level: debug` kommt jeder
Aufruf an Shelly, OPNsense und Zigbee2MQTT dazu (ohne Passwörter). Zusätzlich schreibt die App
`/data/logs/device_manager.log`.

## Sicherheit

Die App läuft im Host-Netzwerk, damit sie Shellys per mDNS findet. Zugriffe auf die Weboberfläche
werden nur über den Home-Assistant-Ingress (172.30.32.2) angenommen.
