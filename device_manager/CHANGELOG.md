# Änderungen

## 0.4.1

- Der Quellcode ist jetzt öffentlich: https://github.com/macpo-homelab/device-manager. Fehler und Ideen gerne
  als Issue, Sicherheitslücken vertraulich über „Report a vulnerability“ (siehe `SECURITY.md`).

## 0.4.0

- **Neuer Name: Device Manager.** Die App bekommt künftig auch Funktionen, die nicht nur Shelly-Geräte betreffen
  (z. B. ein Gerät austauschen). Slug der App: `device_manager`.
- Wer die App bisher als lokale App „Shelly Manager“ installiert hatte: Device Manager neu installieren, Optionen
  übernehmen und die alte App deinstallieren. Das Inventar der alten App wird dabei nicht übernommen.
- Lokaler Betrieb: Die Einstellungen in der `.env` heißen jetzt `DM_…` statt `SM_…`. Verschlüsselte Werte
  (`dpapi:…`) bleiben gültig, nur der Name ändert sich. Die bisherige Datenbank wird automatisch übernommen.
- Bereits ausgestellte Zertifikate („Shelly-Manager …“ in OPNsense) werden weiter erkannt und beim Erneuern ersetzt.

## 0.3.2

- **Neues Zuhause auf GitHub:** Das App-Repository ist jetzt `https://github.com/macpo-homelab/ha-apps`.
- **Standort optional:** Breiten- und Längengrad sind nicht mehr vorbelegt. Bleiben sie leer, setzt der Assistent nur
  die Zeitzone und lässt den Standort am Gerät unverändert.
- Lizenzhinweise für mitgelieferte Schriften und htmx ergänzt.

## 0.3.1

- **Installation über den App-Store:** Der Shelly Manager kommt jetzt aus einem eigenen App-Repository.
  Einmal unter Repositories eintragen, danach meldet Home Assistant neue Versionen samt Änderungen selbst.
  Das manuelle Kopieren nach `/addons` entfällt.

## 0.3.0

- **Neue Oberfläche:** Ruhigeres, übersichtlicheres Design mit heller und dunkler Darstellung (nach Systemeinstellung),
  eigener Schrift (IBM Plex) und klarer Hierarchie: eine Hauptaktion je Bereich, Wartungsaktionen für alle Geräte
  abgesetzt. Läuft eine Aktion, ist ihr Button gesperrt und zeigt „…“.
- **Gerätestatus:** „Status aller Geräte abfragen“ prüft Erreichbarkeit, verfügbare Firmware und Zertifikat und
  speichert das Ergebnis. Eine LED je Gerät zeigt auf einen Blick, wo etwas zu tun ist.
- **Geräteseite:** Steckbrief links, Aufgaben rechts. Ein gemeinsames Namensfeld für IP-Reservierung und Zertifikat.
  Gibt es keine neuere Firmware, steht das als Text da statt als gesperrter Button. Passwort mit „Kopieren“.
- **Assistent:** Die fünf Schritte sind oben sichtbar; abgewählte Schritte werden ausgegraut, der laufende Schritt
  wird hervorgehoben. Das Formular ist neu geordnet.
- **Protokoll:** Zeiten in lokaler Zeit, Gerätename statt MAC (verlinkt), lange Fehlerketten gekürzt mit „Details“,
  Protokoll je Gerät über die Geräteseite.
- Gefundene Geräte lassen sich direkt mit dem Assistenten einrichten.

## 0.2.0

- **Standardeinstellungen für neue Geräte:** Der Assistent setzt nach dem Firmware-Update Gerätename, Zeitzone,
  Standort, NTP-Server, Datumsformat und Eco-Modus. Er sichert den Access Point mit dem Geräte-Passwort und
  schaltet ihn ab, schaltet Bluetooth (ab FW 2.0.0 BLE-RPC) und die LED-Anzeige (Plug M Gen 3) aus.
  Der Schritt lässt sich abwählen.
- **Name statt Hostname:** Der Name darf Großbuchstaben enthalten (z. B. „Kueche-Licht“). Er wird gespeichert und
  als Gerätename und in der DHCP-Reservierung verwendet. DNS-Name und Zertifikat nutzen ihn in Kleinbuchstaben.
- **NTP-Server verteilen:** Neuer Button in der Geräteliste, der den eingestellten NTP-Server auf alle Geräte
  schreibt.
- Neue Optionen: `shelly_ntp_server`, `shelly_timezone`, `shelly_date_format`, `shelly_latitude`,
  `shelly_longitude`.

## 0.1.1

- **Geräteseite:** „Passwort anzeigen“ steht in der Passwortzeile, das Passwort erscheint direkt dort.
- **Firmware:** Installierte und verfügbare Versionen je Kanal werden angezeigt. Der Button nennt die Zielversion.
- „Sofort anwenden“ statt „Sofort anwenden (Neustart)“ bei der IP-Reservierung.

## 0.1.0

- **Geräte verwalten:** Gen2/3/4-Shellys übernehmen und dabei Geräte-ID, MAC und DHCP-Lease prüfen; Passwort pro Gerät.
- **IP-Reservierung** in OPNsense (Kea). Beschreibung = Produktname (z. B. „Shelly Plug M Gen 3“). Unbound wird
  neu geladen, damit der DNS-Name sofort auflösbar ist.
- **Firmware-Update** (stable/beta). Startet ein Gerät danach nicht von selbst neu, bittet die App nach 5 Minuten
  um einen Neustart per Button. Sie startet nie selbst mitten im Update neu.
- **HTTPS-Zertifikat** von der OPNsense-CA ausstellen und installieren.
- **Gen4 im Zigbee-Modus:** WLAN über Zigbee2MQTT ein, Firmware aktualisieren, WLAN aus.
- **Assistent** „Neues Gerät“ für alle Schritte in einem Durchlauf.
- **Protokoll:** Jeder Arbeitsschritt wird protokolliert. Fehler zeigen Schritt, Ursache und Details. Log-Level
  einstellbar, Passwörter werden nie geloggt.
- **Sicherheit:** Schutz vor CSRF und DNS-Rebinding. Zugangsdaten gehen nur an geprüfte Geräte.
