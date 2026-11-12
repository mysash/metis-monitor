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
| Open WebUI: Nutzer, Chats, Antworten, Tokens, tok/s pro Modell | `webui.db` (read-only, Volume `open-webui_open-webui`) |
| Docker-Container | Docker-Socket (nur lesende Aufrufe) |
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
auf dem Host die Werte alle 30 Minuten in eine Datei:

```bash
echo '*/30 * * * * root /usr/sbin/smartctl -j -a /dev/nvme0 > /home/local/stacks/metis-monitor/data/smart.json.tmp; mv /home/local/stacks/metis-monitor/data/smart.json.tmp /home/local/stacks/metis-monitor/data/smart.json' | sudo tee /etc/cron.d/metis-smart
sudo /usr/sbin/smartctl -j -a /dev/nvme0 | sudo tee ~/stacks/metis-monitor/data/smart.json >/dev/null   # sofort einmal
```

## MQTT

Broker werden im Web-UI (Abschnitt *MQTT*) angelegt; dafür ist das `ADMIN_PASSWORD` aus `.env` nötig.
Pro Broker:

- `<präfix>/state` – alle Werte als JSON (retained)
- `<präfix>/status` – `online` / `offline` (LWT)
- `<präfix>/ollama` – geladene/installierte Modelle
- `<präfix>/problems` – `{"problems": [...], "count": n}` (Attribute von *Problem*)
- HA-Discovery unter `<discovery_prefix>/{sensor,binary_sensor}/metis/<key>/config` → Gerät **metis**

Konfiguration liegt in `data/config.yaml` (Passwörter im Klartext, Datei ist `chmod 600`).

## Betrieb

```bash
cd ~/stacks/metis-monitor
docker compose up -d --build     # Update / Neustart
docker logs -f metis-monitor
```

Deployment vom Windows-PC:

```bash
tar czf - --exclude=data --exclude=.env . | ssh <user>@<metis-host> 'cd ~/stacks/metis-monitor && tar xzf - && docker compose up -d --build'
```
