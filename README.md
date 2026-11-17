# metis-monitor

Monitoring-Dashboard für **metis** (Gigabyte DGX Spark, NVIDIA GB10) mit MQTT-Export an beliebig viele Broker
inkl. Home-Assistant-Discovery.

- Web-UI: `http://<metis-host>:8088`
- Flache Werte als JSON: `/api/metrics` · API-Doku: `/docs`

## Daten

| Bereich | Quelle |
|---|---|
| CPU (pro Kern, X925/A725), RAM, Swap, Load, Disk, Netz | psutil / `/proc`, `/sys` (Host) |
| Temperaturen (ACPI-Zonen, NVMe, WLAN) | `/sys/class/thermal`, `/sys/class/hwmon` |
| GPU (Auslastung, Temp, Leistung, Takt, Prozesse) | `nvidia-smi` via CDI |
| Ollama: Version, geladene/installierte Modelle | Ollama API `:11434` |
| Ollama: Anfragen, Dauer, Clients, Endpunkte (24 h) | `journalctl -u ollama` (GIN-Log) |
| Open WebUI: Nutzer, Chats, Antworten, Tokens, tok/s pro Modell und pro Nutzer (inkl. Modelle je Nutzer) | `webui.db` (read-only, Volume `open-webui_open-webui`) |
| Docker-Container | Docker-Socket (nur lesende Aufrufe) |
| Open WebUI: Anfragen pro Client inkl. API-Aufrufe (Anzahl, Dauer) | Zugriffslog des Containers über die Docker-API; Dauer aus der zugehörigen Ollama-Anfrage. API-Aufrufe werden von Open WebUI nicht gespeichert, daher ohne Tokens |
| Leistung / Energie (Tag, Woche, Monat, Jahr, Kosten) | Shelly Gen2+ RPC (Host im Web-UI einstellbar), nur lesend; Tageswerte in `data/energy.db` |
| Ausstehende apt-Updates, Sicherheitsupdates, Neustart nötig | `apt list --upgradable` gegen das Host-Root (read-only), `/run/reboot-required` |

| Dienste: Ollama, Open WebUI (HTTP), Whisper/Piper (Wyoming `describe`) mit Antwortzeit | alle 30 s |
| Docker pro Container: CPU, RAM, Disk-/Netz-IO, Neustarts, Health, OOM | Docker-API `stats` (one-shot) + `inspect` |
| Speicher-/CPU-/IO-Druck, OOM-Kills | `/proc/pressure/*`, `/proc/vmstat`, Kernel-Journal |
| Fehlgeschlagene systemd-Units | Journal (systemd-Meldungen seit Boot); D-Bus ist per AppArmor gesperrt |
| Journal-Fehler (Priorität ≤ err, 24 h), SSH-Logins/Fehlversuche (7 Tage) | `journalctl` |
| Whisper/Piper-Nutzung | TCP-Verbindungen der Container (`/proc/<pid>/net/tcp`), Loopback/Docker-Gateway ignoriert |
| Modell-Ladezeiten | Ollama-Journal (`loading model` → `model loaded`), Blob → Name über die Manifeste |
| GPU-Drosselung | `nvidia-smi clocks_event_reasons.active` |
| Ping Router/Internet | `ping` (5 Pakete, alle 30 s) |
| Energie pro KI-Antwort | Mehrverbrauch über Leerlauf (10. Perzentil 24 h) während `[created_at, +total_duration]` jeder Open-WebUI-Antwort |
| NVMe SMART (optional) | `data/smart.json`, geschrieben von einem Root-Cronjob auf dem Host (siehe unten) |

Verlauf: SQLite in `data/history.db`, 10-s-Mittelwerte, 7 Tage.

Alle Probleme (Dienst down, Container unhealthy, Unit fehlgeschlagen, OOM, hoher Speicherdruck, GPU-Drosselung
durch Temperatur/HW, Temperaturen, Disk > 90 %, NVMe-Warnungen, Paketverlust, viele SSH-Fehlversuche, Steckdose offline)
landen in einer Liste: oben rechts im Dashboard und per MQTT als `binary_sensor.metis_problem`
(Attribute: Liste der Probleme), dazu `sensor.metis_problem_text`.

### NVMe SMART (optional)

SMART-Werte brauchen Root-Rechte auf `/dev/nvme0`. Statt dem Container `SYS_ADMIN` zu geben, schreibt ein Cronjob
auf dem Host die Werte alle 30 Minuten in ein **nur für root beschreibbares** Verzeichnis. Nicht nach `data/`: das kann
der Container beschreiben, ein dort angelegter Symlink könnte sonst den Root-Cronjob umlenken. Der Container liest die
Datei read-only als `/hostsmart/smart.json`:

```bash
sudo install -d -m 755 -o root -g root /var/lib/metis-monitor
echo '*/30 * * * * root /usr/sbin/smartctl -j -a /dev/nvme0 > /var/lib/metis-monitor/smart.json.tmp; [ -s /var/lib/metis-monitor/smart.json.tmp ] && mv /var/lib/metis-monitor/smart.json.tmp /var/lib/metis-monitor/smart.json' | sudo tee /etc/cron.d/metis-smart
sudo sh -c '/usr/sbin/smartctl -j -a /dev/nvme0 > /var/lib/metis-monitor/smart.json'   # sofort einmal
```

`smartctl` meldet Warnungen über den Exit-Code, deshalb wird die Datei verschoben, sobald sie nicht leer ist.
Ein älterer Cronjob, der nach `data/smart.json` schreibt, wird durch den `tee`-Befehl oben ersetzt.

### Automatische Sicherheitsupdates (Host)

Das Dashboard installiert selbst nichts, es zeigt nur an, was ansteht und ob ein Neustart nötig ist. Die Updates
übernimmt `unattended-upgrades` direkt auf dem Host:

```bash
sudo sh ~/stacks/metis-monitor/host/setup-unattended-upgrades.sh
```

Das Skript installiert nur Ubuntu-Sicherheitsupdates (inkl. ESM). Kernel, NVIDIA-Treiber, CUDA, DKMS und
Docker/containerd sowie alles aus dem NVIDIA-Spark-Repository und den NVIDIA-PPAs bleiben ausgenommen und werden
bewusst per SSH aktualisiert. Es gibt keinen automatischen Neustart, geänderte Konfigurationsdateien bleiben erhalten.
Log: `/var/log/unattended-upgrades/unattended-upgrades.log`.

## MQTT

Broker werden im Web-UI (Abschnitt *MQTT*) angelegt; dafür ist das `ADMIN_PASSWORD` aus `.env` nötig.
Pro Broker:

- `<präfix>/state` – alle Werte als JSON (retained)
- `<präfix>/status` – `online` / `offline` (LWT)
- `<präfix>/ollama` – geladene/installierte Modelle
- `<präfix>/openwebui_users` – Nutzung pro Nutzer (Attribute von *Open WebUI Nutzer*)
- `<präfix>/openwebui_clients` – Anfragen pro Client (Attribute von *Open WebUI API-Anfragen*)
- `<präfix>/problems` – `{"problems": [...], "count": n}` (Attribute von *Problem*)
- HA-Discovery unter `<discovery_prefix>/{sensor,binary_sensor}/metis/<key>/config` → Gerät **metis**

Konfiguration liegt in `data/config.yaml` (Passwörter im Klartext, Datei ist `chmod 600`).
Ein gespeichertes Broker- oder Shelly-Passwort wird nur wiederverwendet, solange Host, Port, Benutzer und TLS gleich
bleiben. Bei einem neuen Ziel muss es neu eingegeben werden. Für Broker im LAN möglichst TLS aktivieren und dem
MQTT-Benutzer per ACL nur `<präfix>/#` und `<discovery_prefix>/#` erlauben.

## Sicherheit

- **Admin-Passwort:** `ADMIN_PASSWORD` in `.env`, mindestens 12 Zeichen. Der Platzhalter und kurze Werte werden
  abgelehnt (Bearbeiten bleibt dann deaktiviert). Fehlversuche werden pro IP gebremst.
  Hinter einem Reverse-Proxy (z. B. NPM+) `FORWARDED_ALLOW_IPS=<IP des Proxys>` in `.env` setzen, sonst teilen sich
  alle Nutzer die IP des Proxys und damit einen Fehlversuchs-Zähler.
- **Lesen ist ohne Login möglich** (LAN-Dashboard). Port 8088 sollte nur aus vertrauenswürdigen Netzen erreichbar sein.
- **Host- und Origin-Prüfung:** Anfragen mit fremdem `Host`-Header (DNS-Rebinding) sowie WebSockets und POSTs von fremden
  Webseiten werden abgewiesen. IP-Adressen und der Rechnername (`metis`, `metis.local`, `metis.lan`, `metis.fritz.box`)
  gehen immer, weitere Namen kommen kommagetrennt in `ALLOWED_HOSTS`.
- **HTTPS:** Ohne TLS gehen Admin- und Gerätepasswörter im Klartext durchs LAN. Entweder Zertifikat und Schlüssel nach
  `data/tls/` legen und `SSL_CERTFILE=/data/tls/cert.pem` und `SSL_KEYFILE=/data/tls/key.pem` in `.env` setzen, oder
  `HOST=127.0.0.1` setzen und einen HTTPS-Reverse-Proxy davorschalten.
- **Container:** keine Capabilities außer `NET_RAW` (Ping), `no-new-privileges`, read-only Root-FS, 1 GiB Speicherlimit,
  nur die benötigten Host-Pfade read-only. `data/` gehört `root:<deine Gruppe>` mit Modus `2775`, damit der Container
  ohne `DAC_OVERRIDE` schreiben kann. Einmalig einrichten:
  `docker run --rm -v "$PWD/data:/d" --network none metis-monitor sh -c 'chown 0:1000 /d && chmod 2775 /d'`
- **Docker-Socket:** Der Code ruft nur GET-Endpunkte auf, aber `:ro` am Socket schränkt die Docker-API **nicht** ein.
  Wer das weiter begrenzen will, schaltet einen filternden Socket-Proxy davor.
- **Abhängigkeiten:** `requirements.lock` enthält alle Pakete mit Hashes (`pip install --require-hashes`), das
  Basis-Image ist per Digest gepinnt. Aktualisieren mit
  `pip-compile --generate-hashes --allow-unsafe --strip-extras -o requirements.lock requirements.txt`.
- **Tests:** `docker run --rm -v "$PWD/tests:/srv/tests:ro" -e DATA_DIR=/tmp/d metis-monitor python -m unittest tests.test_security_fixes tests.test_run2_fixes`

## Betrieb

```bash
cd ~/stacks/metis-monitor
docker compose up -d --build     # Update / Neustart
docker logs -f metis-monitor
```

Deployment vom Windows-PC:

```bash
tar czf - --exclude=data --exclude=.env --exclude=.git --exclude=.impeccable . | ssh <user>@<metis-host> 'cd ~/stacks/metis-monitor && tar xzf - && docker compose up -d --build'
```
