# TODO

## Offen

- [ ] **Port 8088 auf metis auf NPM+ beschränken** (Dashboard läuft per HTTPS über NPM+ im LXC auf pve01,
      direkter Zugriff auf `http://metis:8088` überträgt das Admin-Passwort im Klartext)
  - Container nutzt `network_mode: host`, ufw greift also normal für 8088
  - `sudo ufw status verbose` prüfen
  - ufw aktiv: `sudo ufw allow from <NPM+-IP> to any port 8088 proto tcp`, danach `sudo ufw deny 8088/tcp`
    (Reihenfolge mit `sudo ufw status numbered` prüfen)
  - ufw inaktiv: **nicht** blind `ufw enable`, erst alle genutzten Ports (`sudo ss -tlnp`: SSH, Open WebUI,
    Ollama, Whisper/Piper …) freigeben
  - Test: direkt `http://metis:8088` gesperrt, über NPM+ erreichbar, lokal `curl -s localhost:8088/api/metrics` ok
- [ ] **`PUBLIC_URL=https://<NPM+-Hostname>`** in `.env` auf metis setzen, dann `docker compose up -d`
      (Konfigurations-Link in Home Assistant)
- [ ] **Docker-Socket-Proxy** entscheiden: filternder Proxy (nur GET auf benötigte Endpunkte) statt direktem
      Socket-Mount

## Optional

- [ ] `sudo apt autoremove` auf metis für alte 6.17.0-1008-Kernelpakete (vorher `uname -r` prüfen)

## Erledigt

- [x] Security-Audit Run 1 + Run 2 behoben, Tests grün, deployed
- [x] `FORWARDED_ALLOW_IPS` gesetzt
- [x] unattended-upgrades (nur Sicherheitsupdates) eingerichtet
- [x] SMART-Cronjob nach `/var/lib/metis-monitor/smart.json`
