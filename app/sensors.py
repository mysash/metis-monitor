"""Flat metric definitions, used for MQTT state, HA discovery and history."""
from __future__ import annotations


def S(key, name, unit=None, device_class=None, state_class="measurement", icon=None,
      component="sensor", category=None, precision=None, attributes=None):
    return {
        "key": key, "name": name, "unit": unit, "device_class": device_class,
        "state_class": state_class, "icon": icon, "component": component,
        "category": category, "precision": precision, "attributes": attributes,
    }


SENSORS = [
    # System
    S("cpu_percent", "CPU Auslastung", "%", icon="mdi:cpu-64-bit", precision=1),
    S("load_1", "Load (1 min)", icon="mdi:gauge", precision=2),
    S("cpu_temp", "CPU Temperatur", "°C", "temperature", precision=1),
    S("cpu_freq_mhz", "CPU Takt", "MHz", "frequency", precision=0),
    S("mem_used_percent", "RAM Auslastung", "%", icon="mdi:memory", precision=1),
    S("mem_used_gib", "RAM belegt", "GiB", "data_size", precision=1),
    S("mem_available_gib", "RAM verfügbar", "GiB", "data_size", precision=1),
    S("swap_used_percent", "Swap Auslastung", "%", icon="mdi:swap-horizontal", precision=1),
    S("disk_used_percent", "Disk belegt", "%", icon="mdi:harddisk", precision=1),
    S("disk_free_gb", "Disk frei", "GB", "data_size", precision=0),
    S("disk_read_kbps", "Disk Lesen", "kB/s", "data_rate", precision=0),
    S("disk_write_kbps", "Disk Schreiben", "kB/s", "data_rate", precision=0),
    S("nvme_temp", "NVMe Temperatur", "°C", "temperature", precision=0),
    S("net_rx_kbps", "Netzwerk Empfang", "kB/s", "data_rate", precision=1),
    S("net_tx_kbps", "Netzwerk Senden", "kB/s", "data_rate", precision=1),
    S("boot_time", "Gestartet", None, "timestamp", None, category="diagnostic"),
    # GPU
    S("gpu_util", "GPU Auslastung", "%", icon="mdi:expansion-card", precision=0),
    S("gpu_temp", "GPU Temperatur", "°C", "temperature", precision=0),
    S("gpu_power", "GPU Leistung", "W", "power", precision=1),
    S("gpu_clock_mhz", "GPU Takt", "MHz", "frequency", precision=0),
    # Ollama
    S("ollama_up", "Ollama", None, "running", None, component="binary_sensor"),
    S("ollama_loaded_count", "Ollama geladene Modelle", icon="mdi:brain", precision=0),
    S("ollama_loaded_model", "Ollama aktives Modell", state_class=None, icon="mdi:robot", attributes="ollama"),
    S("ollama_loaded_gib", "Ollama Modell-Speicher", "GiB", "data_size", precision=1),
    S("ollama_installed_count", "Ollama installierte Modelle", icon="mdi:database", precision=0),
    S("ollama_requests_1h", "Ollama Anfragen (1h)", icon="mdi:message-processing", precision=0),
    S("ollama_requests_24h", "Ollama Anfragen (24h)", icon="mdi:message-processing", precision=0),
    S("ollama_avg_duration_1h", "Ollama Ø Antwortzeit (1h)", "s", "duration", precision=1),
    S("ollama_last_request", "Ollama letzte Anfrage", None, "timestamp", None),
    # Open WebUI
    S("openwebui_up", "Open WebUI", None, "running", None, component="binary_sensor"),
    S("openwebui_users", "Open WebUI Nutzer", icon="mdi:account-multiple", precision=0, attributes="openwebui_users"),
    S("openwebui_active_users_24h", "Open WebUI aktive Nutzer (24h)", icon="mdi:account-clock", precision=0),
    S("openwebui_chats", "Open WebUI Chats", icon="mdi:forum", precision=0),
    S("openwebui_messages_24h", "Open WebUI Antworten (24h)", icon="mdi:message-text", precision=0),
    S("openwebui_tokens_24h", "Open WebUI Tokens (24h)", icon="mdi:counter", precision=0),
    S("openwebui_tokens_total", "Open WebUI Tokens gesamt", None, None, "total_increasing", "mdi:counter", precision=0),
    S("openwebui_last_tps", "Open WebUI letzte Geschwindigkeit", "tok/s", icon="mdi:speedometer", precision=1),
    # Power (Shelly plug)
    S("power_w", "Leistung gesamt", "W", "power", precision=1),
    S("power_voltage", "Netzspannung", "V", "voltage", precision=1, category="diagnostic"),
    S("power_current", "Stromstärke", "A", "current", precision=3, category="diagnostic"),
    S("energy_today_kwh", "Energie heute", "kWh", "energy", "total_increasing", precision=3),
    S("energy_week_kwh", "Energie diese Woche", "kWh", "energy", "total_increasing", precision=2),
    S("energy_month_kwh", "Energie diesen Monat", "kWh", "energy", "total_increasing", precision=2),
    S("energy_meter_kwh", "Energiezähler", "kWh", "energy", "total_increasing", precision=3),
    S("energy_cost_today_eur", "Stromkosten heute", "EUR", "monetary", "total", precision=2),
    S("energy_cost_month_eur", "Stromkosten diesen Monat", "EUR", "monetary", "total", precision=2),
    # Updates
    S("updates_pending", "Ausstehende Updates", icon="mdi:package-up", precision=0),
    S("updates_security", "Ausstehende Sicherheitsupdates", icon="mdi:shield-alert", precision=0),
    S("reboot_required", "Neustart erforderlich", None, "update", None, component="binary_sensor"),
    # Docker
    S("containers_running", "Docker Container aktiv", icon="mdi:docker", precision=0),
    S("containers_unhealthy", "Docker Container mit Problemen", icon="mdi:docker", precision=0),
    # Health summary
    S("problem", "Problem", None, "problem", None, component="binary_sensor", attributes="problems"),
    S("problem_count", "Anzahl Probleme", icon="mdi:alert-circle-outline", precision=0),
    S("problem_text", "Probleme", state_class=None, icon="mdi:alert-circle-outline"),
    # Services
    S("whisper_up", "Whisper (STT)", None, "running", None, component="binary_sensor"),
    S("piper_up", "Piper (TTS)", None, "running", None, component="binary_sensor"),
    S("whisper_latency_ms", "Whisper Antwortzeit", "ms", "duration", precision=0, category="diagnostic"),
    S("piper_latency_ms", "Piper Antwortzeit", "ms", "duration", precision=0, category="diagnostic"),
    S("whisper_requests_24h", "Whisper Anfragen (24h)", icon="mdi:microphone", precision=0),
    S("piper_requests_24h", "Piper Anfragen (24h)", icon="mdi:account-voice", precision=0),
    S("failed_units", "Fehlgeschlagene systemd-Units", icon="mdi:alert-octagon", precision=0),
    # Pressure / OOM
    S("psi_cpu_some", "CPU-Druck (PSI 60 s)", "%", icon="mdi:gauge", precision=1),
    S("psi_mem_some", "Speicherdruck (PSI 60 s)", "%", icon="mdi:memory", precision=1),
    S("psi_mem_full", "Speicherdruck voll (PSI 60 s)", "%", icon="mdi:memory", precision=1),
    S("psi_io_some", "IO-Druck (PSI 60 s)", "%", icon="mdi:harddisk", precision=1),
    S("oom_kills", "OOM-Kills seit Boot", None, None, "total_increasing", "mdi:skull-outline", precision=0),
    S("oom_kills_24h", "OOM-Kills (24h)", icon="mdi:skull-outline", precision=0),
    # GPU throttling
    S("gpu_throttled", "GPU gedrosselt", None, None, None, component="binary_sensor", icon="mdi:speedometer-slow"),
    S("gpu_throttle_reason", "GPU Drosselgrund", state_class=None, icon="mdi:speedometer-slow", category="diagnostic"),
    # NVMe
    S("nvme_wear_percent", "NVMe Verschleiß", "%", icon="mdi:harddisk", precision=0),
    S("nvme_data_written_tb", "NVMe geschrieben", "TB", "data_size", "total_increasing", precision=2),
    S("nvme_media_errors", "NVMe Medienfehler", icon="mdi:alert", precision=0, category="diagnostic"),
    S("nvme_spare_percent", "NVMe Reserve", "%", icon="mdi:harddisk", precision=0, category="diagnostic"),
    S("nvme_power_on_hours", "NVMe Betriebsstunden", "h", "duration", "total_increasing", precision=0, category="diagnostic"),
    # Journal / SSH
    S("journal_errors_24h", "Journal Fehler (24h)", icon="mdi:text-box-remove-outline", precision=0),
    S("ssh_failed_24h", "SSH Fehlversuche (24h)", icon="mdi:shield-key-outline", precision=0),
    S("ssh_last_login", "SSH letzter Login", None, "timestamp", None),
    # Network
    S("ping_gateway_ms", "Ping Router", "ms", "duration", precision=1),
    S("ping_gateway_loss", "Paketverlust Router", "%", icon="mdi:lan-disconnect", precision=0, category="diagnostic"),
    S("ping_internet_ms", "Ping Internet", "ms", "duration", precision=1),
    S("ping_internet_loss", "Paketverlust Internet", "%", icon="mdi:lan-disconnect", precision=0),
    # AI energy / loads
    S("ollama_last_load_s", "Ollama letzte Modell-Ladezeit", "s", "duration", precision=1),
    S("openwebui_api_requests_24h", "Open WebUI API-Anfragen (24h)", icon="mdi:api", precision=0,
      attributes="openwebui_clients"),
    S("openwebui_api_requests_1h", "Open WebUI API-Anfragen (1h)", icon="mdi:api", precision=0),
    S("ai_energy_per_reply_wh", "KI Mehrverbrauch pro Antwort", "Wh", None, "measurement", "mdi:lightning-bolt", precision=2),
]

SENSOR_KEYS = [s["key"] for s in SENSORS]

# Numeric keys stored in the SQLite history (averaged per history interval)
HISTORY_KEYS = [
    "cpu_percent", "load_1", "cpu_temp", "cpu_freq_mhz", "mem_used_percent", "mem_used_gib",
    "gpu_util", "gpu_temp", "gpu_power", "gpu_clock_mhz", "nvme_temp",
    "net_rx_kbps", "net_tx_kbps", "disk_read_kbps", "disk_write_kbps", "ollama_loaded_gib", "power_w",
    "psi_cpu_some", "psi_mem_some", "psi_mem_full", "psi_io_some", "ping_gateway_ms", "ping_internet_ms",
]
