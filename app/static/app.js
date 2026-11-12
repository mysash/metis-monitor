"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const GiB = 1024 ** 3;

const fmt = {
  n: (v, d = 0) => (v == null || isNaN(v) ? "–" : Number(v).toLocaleString("de-DE", { minimumFractionDigits: d, maximumFractionDigits: d })),
  bytes(v, d = 1) {
    if (v == null) return "–";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return `${fmt.n(v, i ? d : 0)} ${u[i]}`;
  },
  gib: (v, d = 1) => (v == null ? "–" : `${fmt.n(v / GiB, d)} GiB`),
  rate(bps) {
    if (bps == null) return "–";
    if (bps >= 1e6) return `${fmt.n(bps / 1e6, 1)} MB/s`;
    if (bps >= 1e3) return `${fmt.n(bps / 1e3, 1)} kB/s`;
    return `${fmt.n(bps, 0)} B/s`;
  },
  dur(s) {
    if (s == null) return "–";
    s = Math.max(0, Math.floor(s));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return `${d} T ${h} h`;
    if (h) return `${h} h ${m} min`;
    if (m) return `${m} min`;
    return `${s} s`;
  },
  secs: (s) => (s == null ? "–" : s < 1 ? `${fmt.n(s * 1000, 0)} ms` : `${fmt.n(s, s < 10 ? 1 : 0)} s`),
  ago(ts) {
    if (!ts) return "–";
    const s = Date.now() / 1000 - ts;
    if (s < 60) return "gerade eben";
    return `vor ${fmt.dur(s)}`;
  },
  time: (ts) => new Date(ts * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
  tokens(v) {
    if (v == null) return "–";
    if (v >= 1e6) return `${fmt.n(v / 1e6, 1)} M`;
    if (v >= 1e4) return `${fmt.n(v / 1e3, 0)} k`;
    return fmt.n(v);
  },
};

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const state = { snap: null, range: "1h", hist: null, enRange: "24h", enHist: null, brokers: null, sensors: [], editing: null };

/* ------------------------------------------------------------------ charts */
const CHARTS = [
  { id: "ch-util", title: "Auslastung", unit: "%", max: 100, series: [["cpu_percent", "CPU", "--c-cpu"], ["gpu_util", "GPU", "--c-gpu"], ["mem_used_percent", "RAM", "--c-mem"]] },
  { id: "ch-temp", title: "Temperaturen", unit: "°C", min: 20, series: [["cpu_temp", "CPU", "--c-temp"], ["gpu_temp", "GPU", "--c-gpu"], ["nvme_temp", "NVMe", "--c-nvme"]] },
  { id: "ch-power", title: "Leistung", unit: "W", series: [["power_w", "Gesamt (Steckdose)", "--c-temp"], ["gpu_power", "GPU", "--c-gpu"]] },
  { id: "ch-net", title: "Netzwerk", unit: "kB/s", series: [["net_rx_kbps", "Empfang", "--c-rx"], ["net_tx_kbps", "Senden", "--c-tx"]] },
  { id: "ch-psi", title: "Druck (PSI, 60 s)", unit: "%", series: [["psi_cpu_some", "CPU", "--c-cpu"], ["psi_mem_some", "Speicher", "--c-mem"], ["psi_io_some", "IO", "--c-nvme"]] },
  { id: "ch-ping", title: "Latenz", unit: "ms", series: [["ping_gateway_ms", "Router", "--c-rx"], ["ping_internet_ms", "Internet", "--c-tx"]] },
];
const EN_CHART = { id: "ch-energy", title: "Leistungsaufnahme", unit: "W", src: "en", area: true, h: 220, series: [["power_w", "Gesamt (Steckdose)", "--c-temp"], ["gpu_power", "GPU", "--c-gpu"]] };
const HIST_KEYS = [...new Set(CHARTS.flatMap((c) => c.series.map((s) => s[0])))];
const EN_KEYS = EN_CHART.series.map((s) => s[0]);
const RANGE_S = { "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800 };

function niceMax(v) {
  if (!v || v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v)); const n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}

function drawChart(cfg) {
  const fig = document.getElementById(cfg.id);
  const en = cfg.src === "en", h = en ? state.enHist : state.hist; if (!h || !fig) return;
  const W = Math.max(280, fig.clientWidth), H = cfg.h || 170, L = 38, R = 6, T = 8, B = 20;
  const ts = h.ts, now = Date.now() / 1000, span = RANGE_S[en ? state.enRange : state.range];
  const x0 = now - span, x1 = now;
  let vmax = 0, vmin = Infinity;
  for (const [k] of cfg.series) for (const v of h.series[k] || []) if (v != null) { vmax = Math.max(vmax, v); vmin = Math.min(vmin, v); }
  const ymin = cfg.min != null ? Math.min(cfg.min, Math.floor(vmin / 10) * 10 || 0) : 0;
  const ymax = cfg.max ?? niceMax(Math.max(vmax * 1.1, ymin + 1));
  const X = (t) => L + ((t - x0) / (x1 - x0)) * (W - L - R);
  const Y = (v) => T + (1 - (v - ymin) / (ymax - ymin)) * (H - T - B);
  const gap = Math.max(h.bucket * 3, 30);

  let grid = "", axis = "";
  for (let i = 0; i <= 4; i++) {
    const v = ymin + ((ymax - ymin) * i) / 4, y = Y(v);
    grid += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/>`;
    axis += `<text x="${L - 6}" y="${y + 3.5}" text-anchor="end">${fmt.n(v, v < 10 && ymax <= 10 ? 1 : 0)}</text>`;
  }
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const t = x0 + (span * i) / ticks, x = X(t);
    const d = new Date(t * 1000);
    const lab = span > 86400 ? d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit" }) : d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    axis += `<text x="${x}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${lab}</text>`;
  }
  let paths = "";
  const last = {};
  for (const [k, , c] of cfg.series) {
    const vals = h.series[k] || []; let d = "", area = "", prevT = null, segX0 = 0, lastX = 0;
    const close = () => { if (prevT != null) area += `L${lastX},${Y(ymin)}L${segX0},${Y(ymin)}Z`; };
    vals.forEach((v, i) => {
      if (v == null) { close(); prevT = null; return; }
      const x = X(ts[i]).toFixed(1), y = Y(Math.min(Math.max(v, ymin), ymax)).toFixed(1);
      if (prevT == null || ts[i] - prevT > gap) { close(); d += `M${x},${y}`; area += `M${x},${y}`; segX0 = x; }
      else { d += `L${x},${y}`; area += `L${x},${y}`; }
      prevT = ts[i]; lastX = x; last[k] = v;
    });
    close();
    if (cfg.area) paths += `<path d="${area}" fill="${css(c)}" fill-opacity="0.12" stroke="none"/>`;
    paths += `<path d="${d}" fill="none" stroke="${css(c)}" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  const cur = state.snap?.metrics || {};
  const legend = cfg.series.map(([k, n, c]) => `<span><i style="background:${css(c)}"></i>${esc(n)} <b>${fmt.n(cur[k] ?? last[k], cfg.unit === "%" || cfg.unit === "°C" ? 0 : 1)}</b></span>`).join("");
  fig.innerHTML = `<div class="c-head"><span class="c-title">${esc(cfg.title)} <span class="muted">${esc(cfg.unit)}</span></span><div class="legend">${legend}</div></div>
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><g class="grid">${grid}</g><g class="axis">${axis}</g>${paths}
    <g class="hover" style="display:none"><line y1="${T}" y2="${H - B}"/></g><rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/></svg>`;
  const svg = $("svg", fig), hov = $(".hover", svg), tip = $("#tip");
  $("rect", svg).addEventListener("mousemove", (ev) => {
    if (!ts.length) return;
    const r = svg.getBoundingClientRect();
    const t = x0 + ((ev.clientX - r.left) * (W / r.width) - L) / (W - L - R) * (x1 - x0);
    let bi = 0, bd = Infinity;
    ts.forEach((tt, i) => { const dd = Math.abs(tt - t); if (dd < bd) { bd = dd; bi = i; } });
    if (bd > gap) { hov.style.display = "none"; tip.style.display = "none"; return; }
    const x = X(ts[bi]);
    hov.style.display = ""; $("line", hov).setAttribute("x1", x); $("line", hov).setAttribute("x2", x);
    const d = new Date(ts[bi] * 1000);
    tip.innerHTML = `<div class="t">${d.toLocaleString("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit", second: span <= 3600 ? "2-digit" : undefined })}</div>` +
      cfg.series.map(([k, n, c]) => `<div><i style="background:${css(c)}"></i>${esc(n)}: ${fmt.n(h.series[k]?.[bi], 1)}</div>`).join("");
    tip.style.display = "block";
    tip.style.left = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = ev.clientY + 14 + "px";
  });
  $("rect", svg).addEventListener("mouseleave", () => { hov.style.display = "none"; tip.style.display = "none"; });
}
const drawCharts = () => { CHARTS.forEach(drawChart); drawChart(EN_CHART); renderEnStats(); };

// energy consumed in the selected range: integrate the averaged power samples (gaps are not bridged)
function renderEnStats() {
  const h = state.enHist, el = $("#en-hist-stats"); if (!el) return;
  const p = h?.series?.power_w || [];
  let wh = 0, n = 0, sum = 0, max = null, min = null;
  const gap = Math.max((h?.bucket || 10) * 3, 30);
  p.forEach((v, i) => {
    if (v == null) return;
    n++; sum += v; max = max == null ? v : Math.max(max, v); min = min == null ? v : Math.min(min, v);
    const dt = i + 1 < h.ts.length && p[i + 1] != null ? h.ts[i + 1] - h.ts[i] : 0;
    if (dt > 0 && dt <= gap) wh += ((v + p[i + 1]) / 2) * dt / 3600;
  });
  if (!n) { el.innerHTML = `<div class="empty">Noch keine Messwerte im Zeitraum.</div>`; return; }
  const price = state.snap?.power?.price_eur_kwh;
  el.innerHTML = kv("Verbrauch im Zeitraum", `${fmt.n(wh / 1000, wh < 1000 ? 3 : 2)}<small>kWh</small>${price != null ? `<small>${fmt.n((wh / 1000) * price, 2)} €</small>` : ""}`) +
    kv("Durchschnitt", fmt.n(sum / n, 1), "W") + kv("Minimum", fmt.n(min, 1), "W") + kv("Maximum", fmt.n(max, 1), "W");
}

async function loadHistory() {
  try {
    const r = await fetch(`/api/history?range=${state.range}&keys=${HIST_KEYS.join(",")}`);
    state.hist = await r.json();
    drawCharts();
  } catch (e) { console.warn(e); }
}

async function loadEnHistory() {
  try {
    const r = await fetch(`/api/history?range=${state.enRange}&keys=${EN_KEYS.join(",")}`);
    state.enHist = await r.json();
    drawChart(EN_CHART); renderEnStats();
  } catch (e) { console.warn(e); }
}

function pushLive(h, keys, m, ts) {
  h.ts.push(ts); for (const k of keys) (h.series[k] ||= []).push(m[k] ?? null);
  while (h.ts.length && h.ts[0] < ts - 3600) { h.ts.shift(); for (const k of keys) h.series[k].shift(); }
  h.bucket = Math.min(h.bucket, 10);
}
function appendLive(m, ts) {
  // 1h view: append every live sample for a smooth, real-time chart
  if (state.hist && state.range === "1h") pushLive(state.hist, HIST_KEYS, m, ts);
  if (state.enHist && state.enRange === "1h") pushLive(state.enHist, EN_KEYS, m, ts);
}

/* ------------------------------------------------------------------ render */
const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
const setW = (id, pct) => { const el = document.getElementById(id); if (el) el.style.width = `${Math.max(0, Math.min(100, pct || 0))}%`; };
const kv = (label, val, unit = "") => `<div class="kv"><span>${esc(label)}</span><b>${val}${unit ? `<small>${esc(unit)}</small>` : ""}</b></div>`;
const bars = (id, arr, label) => {
  const max = Math.max(1, ...arr);
  document.getElementById(id).innerHTML = arr.map((v, i) => `<i style="height:${(v / max) * 100}%" title="${23 - i === 0 ? "letzte Stunde" : `vor ${23 - i} h`}: ${v} ${label}"></i>`).join("");
};

function render(s) {
  state.snap = s;
  const sys = s.system, gpu = s.gpu, m = s.metrics, ol = s.ollama, ow = s.openwebui;

  setText("hostname", sys.hostname);
  setText("hostinfo", `${gpu.name || "GPU"} · ${sys.cpu_model} · ${sys.os}`);
  document.title = `${sys.hostname} · ${fmt.n(m.cpu_percent)}% CPU · ${fmt.n(m.gpu_util)}% GPU`;
  setText("uptime", fmt.dur(sys.uptime));
  setText("ip", sys.net.ip || "–");
  setText("ver", `v${s.version}`);

  // tiles
  setText("t-cpu", fmt.n(m.cpu_percent, m.cpu_percent < 10 ? 1 : 0)); setW("m-cpu", m.cpu_percent);
  setText("t-cpu-sub", `Load ${fmt.n(sys.cpu.load[0], 2)} · ${fmt.n(m.cpu_freq_mhz)} MHz`);
  setText("t-gpu", gpu.available ? fmt.n(gpu.util) : "n/a"); setW("m-gpu", gpu.util);
  setText("t-gpu-sub", gpu.available ? `${fmt.n(gpu.clock_mhz)} MHz · ${gpu.pstate}` : "keine Daten");
  setText("t-mem", fmt.n(m.mem_used_gib, 1)); setW("m-mem", m.mem_used_percent);
  setText("t-mem-sub", `von ${fmt.n(sys.memory.total / GiB, 0)} GiB · ${fmt.n(m.mem_used_percent, 0)}%`);
  const pw = s.power || {};
  if (pw.up) {
    setText("t-pwr-label", "Leistung gesamt");
    setText("t-pwr", fmt.n(pw.power, 1)); setW("m-pwr", ((pw.power || 0) / 240) * 100);
    setText("t-pwr-sub", gpu.power != null ? `davon GPU ${fmt.n(gpu.power, 1)} W` : "");
  } else {
    setText("t-pwr-label", "GPU Leistung");
    setText("t-pwr", fmt.n(gpu.power, 1)); setW("m-pwr", ((gpu.power || 0) / 140) * 100);
    setText("t-pwr-sub", pw.configured ? "Steckdose offline" : "keine Steckdose");
  }
  setText("t-en", pw.today_kwh != null ? fmt.n(pw.today_kwh, 2) : "–");
  setText("t-en-sub", pw.price_eur_kwh != null && pw.today_kwh != null ? `${fmt.n(pw.today_kwh * pw.price_eur_kwh, 2)} €` : "");
  setText("t-en-foot", pw.month_kwh != null ? `Monat ${fmt.n(pw.month_kwh, 1)} kWh${pw.month_partial ? "*" : ""}` : pw.configured ? "" : "Steckdose einrichten");
  const up = s.updates || {};
  const ut = $('.tile[data-k="updates"]');
  ut.classList.toggle("has-sec", (up.security || 0) > 0);
  ut.classList.toggle("ok", up.available && !up.count);
  setText("t-up", up.available ? (up.count ? fmt.n(up.count) : "✓") : "–");
  setText("t-up-unit", up.available && !up.count ? "aktuell" : "ausstehend");
  setText("t-up-sub", up.security ? `${up.security} Sicherheit` : "");
  setText("t-up-foot", up.reboot_required ? "⟳ Neustart erforderlich" : up.lists_updated ? `Stand ${fmt.ago(up.lists_updated)}` : (up.error || "prüfe…"));
  setText("t-tcpu", fmt.n(m.cpu_temp, 0)); setText("t-tgpu", fmt.n(m.gpu_temp, 0)); setText("t-tnvme", fmt.n(m.nvme_temp, 0));
  setW("m-temp", (Math.max(m.cpu_temp || 0, m.gpu_temp || 0) / 100) * 100);
  const loaded = ol.loaded || [];
  setText("t-ai", !ol.up ? "offline" : loaded.length ? loaded.map((x) => x.name).join(", ") : "kein Modell geladen");
  setText("t-ai-sub", ol.version ? `v${ol.version}` : "");
  const rq = ol.requests || {};
  setText("t-ai-foot", `${fmt.n(rq.requests_1h)} Anfr./h · ${fmt.n(rq.requests_24h)} /24h`);

  renderEnergy(pw);
  renderUpdates(up);
  renderOllama(ol);
  renderOwui(ow);
  renderSystem(s);
  for (const fn of [renderHealth, renderServices, renderPsi, renderNvme, renderPing, renderJournal, renderSsh, renderVoice, renderAiEnergy]) {
    try { fn(s); } catch (e) { console.error(fn.name, e); }
  }
  renderMqttStatus(s.mqtt || {});
  appendLive(m, s.ts);
  drawCharts();
}

function renderEnergy(pw) {
  const st = $("#en-status");
  if (!pw.configured) {
    st.textContent = "nicht eingerichtet"; st.className = "pill";
    $("#en-kpis").innerHTML = `<div class="empty">Keine Steckdose konfiguriert. Über „Einstellungen“ einen Shelly-Plug eintragen.</div>`;
    $("#en-daily").innerHTML = ""; $("#en-note").textContent = ""; setText("en-plug", "");
    return;
  }
  st.textContent = pw.up ? `online · ${pw.host}` : "offline"; st.className = `pill ${pw.up ? "ok" : "bad"}`;
  setText("en-plug", pw.name ? `· ${pw.name}` : "");
  const price = pw.price_eur_kwh;
  const eur = (k) => (price != null && k != null ? `<small>${fmt.n(k * price, 2)} €</small>` : "");
  const star = (p) => (p ? "*" : "");
  $("#en-kpis").innerHTML =
    kv("Jetzt", pw.up ? fmt.n(pw.power, 1) : "–", "W") +
    kv("Heute", `${fmt.n(pw.today_kwh, 2)}<small>kWh</small>${eur(pw.today_kwh)}`) +
    kv("Gestern", `${fmt.n(pw.yesterday_kwh, 2)}<small>kWh</small>${eur(pw.yesterday_kwh)}`) +
    kv(`Woche${star(pw.week_partial)}`, `${fmt.n(pw.week_kwh, 1)}<small>kWh</small>${eur(pw.week_kwh)}`) +
    kv(`Monat${star(pw.month_partial)}`, `${fmt.n(pw.month_kwh, 1)}<small>kWh</small>${eur(pw.month_kwh)}`) +
    kv(`Jahr${star(pw.year_partial)}`, `${fmt.n(pw.year_kwh, 1)}<small>kWh</small>${eur(pw.year_kwh)}`) +
    kv("Spannung", pw.up ? fmt.n(pw.voltage, 1) : "–", "V") + kv("Strom", pw.up ? fmt.n(pw.current, 2) : "–", "A");
  const daily = pw.daily || [];
  const max = Math.max(0.001, ...daily.map((d) => d.kwh || 0));
  $("#en-daily").innerHTML = daily.map((d, i) => {
    const lab = new Date(d.day + "T12:00").toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
    return `<i class="${i === daily.length - 1 ? "today" : ""} ${d.kwh == null ? "none" : ""}" style="height:${d.kwh == null ? 2 : Math.max(2, (d.kwh / max) * 100)}%" title="${lab}: ${d.kwh == null ? "keine Daten" : `${fmt.n(d.kwh, 2)} kWh${price != null ? ` · ${fmt.n(d.kwh * price, 2)} €` : ""}`}"></i>`;
  }).join("");
  const notes = [];
  if (pw.since) notes.push(`Gemessen seit ${new Date(pw.since + "T12:00").toLocaleDateString("de-DE")}${pw.week_partial || pw.month_partial ? " (* Zeitraum noch unvollständig)" : ""}.`);
  if (price == null) notes.push("Für Kosten unter „Einstellungen“ den Strompreis eintragen.");
  else notes.push(`Strompreis ${fmt.n(price, 4)} €/kWh.`);
  if (!pw.up && pw.error) notes.push(`Fehler: ${pw.error}`);
  $("#en-note").textContent = notes.join(" ");
}

function renderUpdates(up) {
  const st = $("#up-status");
  if (!up.available) { st.textContent = up.error ? "Fehler" : "prüfe…"; st.className = `pill ${up.error ? "bad" : ""}`; $("#up-note").textContent = up.error || ""; return; }
  st.textContent = up.count ? `${up.count} ausstehend${up.security ? ` · ${up.security} Sicherheit` : ""}` : "aktuell";
  st.className = `pill ${up.count ? (up.security ? "bad" : "") : "ok"}`;
  $("#up-reboot").innerHTML = up.reboot_required ? `<div class="reboot">⟳ Neustart erforderlich${up.reboot_pkgs?.length ? ` (${esc(up.reboot_pkgs.join(", "))})` : ""}</div>` : "";
  $("#up-list").innerHTML = (up.packages || []).length ? up.packages.map((p) =>
    `<div class="item"><span class="name" title="${esc(p.suite)}">${p.security ? `<span class="st bad" title="Sicherheitsupdate"></span>` : `<span class="st"></span>`}${esc(p.name)}</span><span class="meta">${esc(p.old)} → ${esc(p.new)}</span></div>`).join("")
    : `<div class="empty">Alle Pakete sind aktuell.</div>`;
  $("#up-note").textContent = `Paketlisten aktualisiert ${fmt.ago(up.lists_updated)} (apt-daily). Installieren auf metis mit: sudo apt upgrade`;
}

function renderOllama(ol) {
  const st = $("#ol-status");
  st.textContent = ol.up ? `online · v${ol.version}` : "offline"; st.className = `pill ${ol.up ? "ok" : "bad"}`;
  const rq = ol.requests || {};
  const loadedBytes = (ol.loaded || []).reduce((a, x) => a + (x.size_vram || x.size || 0), 0);
  $("#ol-kpis").innerHTML =
    kv("Anfragen 1 h", fmt.n(rq.requests_1h)) + kv("Anfragen 24 h", fmt.n(rq.requests_24h)) +
    kv("pro Minute", fmt.n(rq.requests_per_min, 1)) + kv("Ø Dauer 1 h", fmt.secs(rq.avg_duration_1h)) +
    kv("Modell-RAM", fmt.n(loadedBytes / GiB, 1), "GiB") + kv("Fehler 24 h", fmt.n(rq.errors_24h));

  $("#ol-loaded").innerHTML = (ol.loaded || []).length ? ol.loaded.map((x) => {
    const exp = x.expires_at ? (new Date(x.expires_at) - Date.now()) / 1000 : null;
    return `<div class="item model-card"><div style="min-width:0"><div class="name">${esc(x.name)}</div><div class="tags">
      ${x.parameter_size ? `<span class="tag">${esc(x.parameter_size)}</span>` : ""}${x.quantization ? `<span class="tag">${esc(x.quantization)}</span>` : ""}
      ${x.context_length ? `<span class="tag">ctx ${fmt.n(x.context_length)}</span>` : ""}</div></div>
      <div class="meta">${fmt.gib(x.size_vram || x.size)}<br>${exp == null ? "" : exp > 1e7 ? "dauerhaft" : exp > 0 ? `entlädt in ${fmt.dur(exp)}` : "entlädt…"}</div></div>`;
  }).join("") : `<div class="empty">Kein Modell im Speicher.</div>`;

  bars("ol-hourly", rq.hourly || new Array(24).fill(0), "Anfragen");
  const cmax = Math.max(1, ...(rq.clients || []).map((c) => c.count));
  $("#ol-clients").innerHTML = (rq.clients || []).length ? rq.clients.map((c) =>
    `<div class="item"><span class="name" title="${esc(c.ip)}">${esc(c.name || c.ip)}${c.name && c.name !== c.ip ? ` <span class="muted mono">${esc(c.ip)}</span>` : ""}</span><span class="bar"><i style="width:${(c.count / cmax) * 100}%"></i></span><span class="meta">${fmt.n(c.count)}</span></div>`).join("")
    : `<div class="empty">${rq.available === false ? esc(rq.error || "Log nicht verfügbar") : "Noch keine Anfragen."}</div>`;
  $("#ol-paths").innerHTML = (rq.paths || []).length ? rq.paths.map((p) => `<div class="item"><span class="name mono">${esc(p.path)}</span><span class="meta">${fmt.n(p.count)}</span></div>`).join("") : `<div class="empty">–</div>`;

  $("#ol-recent").innerHTML = (rq.recent || []).length
    ? `<tr><th>Zeit</th><th>Client</th><th>Endpunkt</th><th class="num">Status</th><th class="num">Dauer</th></tr>` +
      rq.recent.map((r) => `<tr><td class="mono sm">${fmt.time(r.ts)}</td><td>${esc(r.client_name || r.client)}</td><td class="mono sm">${esc(r.method)} ${esc(r.path)}</td>
        <td class="num mono ${r.status < 400 ? "code-ok" : "code-bad"}">${r.status}</td><td class="num">${fmt.secs(r.duration)}</td></tr>`).join("")
    : `<tr><td class="empty">Keine Anfragen in den letzten 24 h.</td></tr>`;

  const loads = ol.loads || [];
  const lmax = Math.max(1, ...loads.map((l) => l.seconds || 0));
  $("#ol-loads").innerHTML = loads.length ? loads.slice(0, 8).map((l) =>
    `<div class="item"><span class="name">${esc(l.model)}</span><span class="bar"><i style="width:${(l.seconds / lmax) * 100}%"></i></span><span class="meta">${fmt.secs(l.seconds)} · ${fmt.ago(l.ts)}</span></div>`).join("")
    : `<div class="empty">Keine Ladevorgänge im Log.</div>`;

  const loadedNames = new Set((ol.loaded || []).map((x) => x.name));
  $("#ol-installed").innerHTML = `<tr><th>Modell</th><th>Parameter</th><th>Quant.</th><th class="num">Kontext</th><th class="num">Größe</th></tr>` +
    (ol.installed || []).map((x) => `<tr><td><span class="st ${loadedNames.has(x.name) ? "ok" : ""}"></span>${esc(x.name)}
      ${(x.capabilities || []).filter((c) => c !== "completion").map((c) => `<span class="tag">${esc(c)}</span>`).join(" ")}</td>
      <td class="sm">${esc(x.parameter_size || "")}</td><td class="sm mono">${esc(x.quantization || "")}</td><td class="num sm">${fmt.n(x.context_length)}</td><td class="num">${fmt.bytes(x.size)}</td></tr>`).join("") +
    `<tr><td class="sm">${(ol.installed || []).length} Modelle</td><td></td><td></td><td></td><td class="num sm">${fmt.bytes(ol.installed_size)}</td></tr>`;
}

function renderOwui(ow) {
  const st = $("#ow-status");
  st.textContent = ow.up ? `online${ow.version ? ` · v${ow.version}` : ""}` : "offline"; st.className = `pill ${ow.up ? "ok" : "bad"}`;
  if (ow.db_available === false) {
    $("#ow-kpis").innerHTML = `<div class="empty">Datenbank nicht lesbar: ${esc(ow.db_error)}</div>`;
    return;
  }
  $("#ow-kpis").innerHTML =
    kv("Nutzer", `${fmt.n(ow.active_users_24h)}<small>/ ${fmt.n(ow.users)} aktiv</small>`) + kv("Chats", fmt.n(ow.chats)) +
    kv("Antworten 24 h", fmt.n(ow.messages_24h)) + kv("Tokens 24 h", fmt.tokens(ow.tokens_24h)) +
    kv("Antworten gesamt", fmt.n(ow.messages_total)) + kv("Tokens gesamt", fmt.tokens(ow.tokens_total));
  bars("ow-hourly", ow.hourly || new Array(24).fill(0), "Antworten");
  $("#ow-models").innerHTML = (ow.per_model || []).length
    ? `<tr><th>Modell</th><th class="num">Antworten</th><th class="num">Tokens in</th><th class="num">Tokens out</th><th class="num">Ø tok/s</th><th class="num">zuletzt</th></tr>` +
      ow.per_model.map((x) => `<tr><td>${esc(x.model)}</td><td class="num">${fmt.n(x.messages)}</td><td class="num">${fmt.tokens(x.in_tokens)}</td>
        <td class="num">${fmt.tokens(x.out_tokens)}</td><td class="num">${fmt.n(x.avg_tps, 1)}</td><td class="num sm">${fmt.ago(x.last)}</td></tr>`).join("")
    : `<tr><td class="empty">Noch keine Antworten.</td></tr>`;
  renderOwUsers(ow.per_user || []);
  const lr = ow.last_response;
  $("#ow-last").textContent = lr ? `Letzte Antwort ${fmt.ago(lr.ts)} von ${lr.model}${lr.tps ? ` mit ${fmt.n(lr.tps, 1)} tok/s` : ""}.` : "";
}

function renderOwUsers(users) {
  if (!users.length) { $("#ow-users").innerHTML = `<tr><td class="empty">Keine Nutzer.</td></tr>`; return; }
  const total = users.reduce((a, u) => a + u.tokens, 0) || 1;
  $("#ow-users").innerHTML =
    `<tr><th>Nutzer / Modell</th><th class="num" title="Antworten gesamt">Antw.</th><th class="num" title="Tokens Eingabe (Prompt + Kontext)">Tok. in</th><th class="num" title="Tokens Ausgabe">Tok. out</th><th class="num" title="Tokens in den letzten 24 h">24 h</th><th class="num">zuletzt</th></tr>` +
    users.map((u) => {
      const share = (u.tokens / total) * 100;
      const head = `<tr class="u-row"><td><b>${esc(u.name)}</b> <span class="pill sm-pill">${esc(u.role)}</span>
          <div class="u-sub"><span class="share" title="Anteil an allen Tokens"><i style="width:${share.toFixed(1)}%"></i></span>${fmt.n(share, 0)} % · ${fmt.n(u.chats)} Chats</div></td>
        <td class="num">${fmt.n(u.messages)}${u.messages_24h ? `<div class="u-sub">${fmt.n(u.messages_24h)} in 24 h</div>` : ""}</td>
        <td class="num">${fmt.tokens(u.in_tokens)}</td><td class="num">${fmt.tokens(u.out_tokens)}</td><td class="num">${fmt.tokens(u.tokens_24h)}</td>
        <td class="num sm">${u.last_active ? fmt.ago(u.last_active) : "nie"}</td></tr>`;
      const sub = (u.models || []).map((m) => `<tr class="u-model"><td class="sm">${esc(m.model)}${m.avg_tps ? ` <span class="muted">· ${fmt.n(m.avg_tps, 1)} tok/s</span>` : ""}</td>
        <td class="num sm">${fmt.n(m.messages)}</td><td class="num sm">${fmt.tokens(m.in_tokens)}</td><td class="num sm">${fmt.tokens(m.out_tokens)}</td>
        <td class="num sm">${fmt.tokens(m.tokens_24h)}</td><td class="num sm">${fmt.ago(m.last)}</td></tr>`).join("");
      return head + (sub || `<tr class="u-model"><td class="sm muted" colspan="6">noch keine Antworten</td></tr>`);
    }).join("");
}

function renderSystem(s) {
  const sys = s.system, gpu = s.gpu;
  const types = sys.core_types || [];
  $("#cores").innerHTML = sys.cpu.per_core.map((v, i) => {
    const t = types[i] || "";
    return `<div class="core ${t.startsWith("X") ? "x" : "a"}" title="Kern ${i} · ${esc(t)} · ${fmt.n(v)}% · ${fmt.n(sys.cpu.freq_mhz[i])} MHz"><div class="cb"><i style="height:${v}%"></i></div><span>${esc(t ? t[0] : "")}${i}</span></div>`;
  }).join("");
  setText("cpu-load", `Load ${sys.cpu.load.map((x) => fmt.n(x, 2)).join(" · ")}`);
  setText("cpu-model", `${sys.cpu_model} · ${sys.arch} · Kernel ${sys.kernel}`);

  setText("gpu-pstate", gpu.pstate || "");
  const th = gpu.throttle || {};
  const thTxt = !th.available ? "n/a" : th.active ? `<span class="${th.critical ? "code-bad" : ""}">${esc(th.reasons_text.join(", "))}</span>` : `<span class="code-ok">keine</span>`;
  $("#gpu-info").innerHTML = gpu.available
    ? [["Modell", gpu.name], ["Treiber", gpu.driver], ["Auslastung", `${fmt.n(gpu.util)} %`], ["Temperatur", `${fmt.n(gpu.temp)} °C`],
       ["Leistung", `${fmt.n(gpu.power, 1)} W`], ["SM-Takt", `${fmt.n(gpu.clock_mhz)} / ${fmt.n(gpu.clock_max_mhz)} MHz`]]
      .map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("") +
      `<dt>Drosselung</dt><dd>${thTxt}</dd>` + (th.available ? `<dt>gedrosselt (1 h)</dt><dd>${fmt.n(th.share_1h, 1)} %</dd>` : "")
    : `<dt>Fehler</dt><dd>${esc(gpu.error)}</dd>`;
  $("#gpu-procs").innerHTML = (gpu.processes || []).length
    ? gpu.processes.map((p) => `<div class="item"><span class="name">${esc(p.name)} <span class="muted mono">${p.pid}</span></span><span class="meta">${p.gpu_mem_mib ? fmt.gib(p.gpu_mem_mib * 1048576) : p.rss ? fmt.gib(p.rss) : ""}</span></div>`).join("")
    : `<div class="empty">Keine aktiven GPU-Prozesse.</div>`;

  const n = sys.net, io = sys.disk_io, mem = sys.memory;
  $("#io-info").innerHTML = [
    ["Interface", `${n.iface || "–"}${n.speed ? ` · ${n.speed >= 1000 ? `${n.speed / 1000} Gbit` : `${n.speed} Mbit`}` : ""}`],
    ["Empfang", fmt.rate(n.rx_bps)], ["Senden", fmt.rate(n.tx_bps)],
    ["Disk lesen", fmt.rate(io.read_bps)], ["Disk schreiben", fmt.rate(io.write_bps)],
    ["RAM verfügbar", fmt.gib(mem.available)], ["Page Cache", fmt.gib(mem.cached)],
    ["Swap", `${fmt.gib(mem.swap_used)} / ${fmt.gib(mem.swap_total, 0)}`],
  ].map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  $("#disks").innerHTML = sys.disks.map((d) => `<div class="disk"><div class="d-head"><span>Disk <span class="mono">${esc(d.mount)}</span></span><span>${fmt.bytes(d.used)} / ${fmt.bytes(d.total)}</span></div>
    <div class="meter"><i style="width:${d.percent}%"></i></div></div>`).join("");

  const t = sys.temps;
  const tl = [...t.zones.map((z) => ({ name: z.name, temp: z.temp })), ...(t.nvme != null ? [{ name: "NVMe", temp: t.nvme }] : []),
    ...(gpu.temp != null ? [{ name: "GPU", temp: gpu.temp }] : []), ...t.other];
  $("#temps").innerHTML = tl.map((z) => `<div class="item"><span class="name">${esc(z.name)}</span><span class="bar"><i style="width:${Math.min(100, z.temp)}%;background:${z.temp > 85 ? "var(--bad)" : z.temp > 70 ? "var(--warn)" : "var(--c-temp)"}"></i></span><span class="meta">${fmt.n(z.temp, 1)} °C</span></div>`).join("");

  const dk = s.docker;
  const cs = dk.containers || [];
  setText("ct-count", dk.available ? `${cs.filter((c) => c.state === "running").length} / ${cs.length} laufen` : (dk.error || ""));
  $("#containers").innerHTML = `<tr><th>Name</th><th>Image</th><th>Projekt</th><th class="num">CPU</th><th class="num">RAM</th><th class="num">Disk L/S</th><th class="num">Netz ↓/↑</th><th class="num">Neustarts</th><th>Status</th></tr>` + cs.map((c) => {
    const cls = c.state === "exited" && !c.exit_code ? "" : c.state !== "running" ? "bad" : c.health === "unhealthy" ? "bad" : c.health === "starting" ? "warn" : "ok";
    const run = c.state === "running";
    return `<tr><td><span class="st ${cls}"></span>${esc(c.name)}</td><td class="sm mono">${esc(c.image)}</td><td class="sm">${esc(c.project || "")}</td>
      <td class="num">${run && c.cpu_percent != null ? `${fmt.n(c.cpu_percent, c.cpu_percent < 10 ? 2 : 1)} %` : "–"}</td>
      <td class="num">${run && c.mem != null ? fmt.bytes(c.mem) : "–"}</td>
      <td class="num sm">${run && c.blk_read != null ? `${fmt.bytes(c.blk_read)} / ${fmt.bytes(c.blk_write)}` : "–"}</td>
      <td class="num sm">${run && c.net_rx != null ? `${fmt.bytes(c.net_rx)} / ${fmt.bytes(c.net_tx)}` : run ? "Host-Netz" : "–"}</td>
      <td class="num ${c.restarts ? "code-bad" : ""}">${fmt.n(c.restarts)}</td>
      <td class="sm" title="${esc(c.health_log || "")}">${esc(c.status)}${c.oom_killed ? ` · <span class="code-bad">OOM</span>` : ""}</td></tr>`;
  }).join("");
}

/* ------------------------------------------------------------------ health & ops */
function renderHealth(s) {
  const p = s.problems || [];
  const h = $("#health");
  h.className = `health ${p.length ? "bad" : "ok"}`;
  setText("health-txt", p.length ? `${p.length} Problem${p.length > 1 ? "e" : ""}` : "alles OK");
  const box = $("#problems");
  box.hidden = !p.length;
  box.innerHTML = p.length ? `<h2>Probleme</h2><ul>${p.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
}

function renderServices(s) {
  const sv = s.services || [];
  const down = sv.filter((x) => !x.ok).length;
  const st = $("#svc-status");
  st.textContent = down ? `${down} gestört` : `${sv.length} OK`; st.className = `pill ${down ? "bad" : "ok"}`;
  $("#svc-list").innerHTML = sv.map((x) => `<div class="item"><span class="name" title="${esc(x.target)}"><span class="st ${x.ok ? "ok" : "bad"}"></span>${esc(x.name)}</span>
    <span class="meta" title="${esc(x.info)}">${x.ok ? `${fmt.n(x.latency_ms, x.latency_ms < 10 ? 1 : 0)} ms · ${esc(String(x.info).slice(0, 26))}` : `<span class="code-bad">${esc(String(x.info).slice(0, 40))}</span>`}${x.ok ? "" : `<br><span class="muted">gestört seit ${fmt.dur(Date.now() / 1000 - x.since)}</span>`}</span></div>`).join("");
  const j = s.journal || {};
  const fu = j.failed_units;
  $("#units").innerHTML = fu == null ? `<div class="empty">wird geprüft…</div>`
    : fu.length ? fu.map((u) => `<div class="item"><span class="name"><span class="st bad"></span>${esc(u.unit)}</span><span class="meta">${fmt.ago(u.ts)}</span></div>`).join("")
    : `<div class="empty">Keine fehlgeschlagenen Units seit dem Boot.</div>`;
}

function renderPsi(s) {
  const pr = s.pressure || {};
  if (!pr.available) { $("#psi").innerHTML = `<div class="empty">/proc/pressure nicht verfügbar.</div>`; return; }
  const row = (label, d) => {
    const bar = (kind, v) => !v ? "" : `<div class="psi-bar ${kind}"><div class="meter"><i style="width:${Math.min(100, v.avg10 * 2)}%"></i></div><small>${kind === "full" ? "voll" : "teilw."} ${fmt.n(v.avg10, 1)} · ${fmt.n(v.avg60, 1)} · ${fmt.n(v.avg300, 1)} %</small></div>`;
    return `<div class="psi-row"><span>${label}</span>${bar("some", d.some)}${d.full && label !== "CPU" ? bar("full", d.full) : ""}</div>`;
  };
  $("#psi").innerHTML = row("CPU", pr.cpu || {}) + row("Speicher", pr.memory || {}) + row("IO", pr.io || {}) +
    `<div class="note" style="margin-top:0">Werte: 10 s · 60 s · 5 min. Balken bis 50 %.</div>`;
  const oom = (s.journal || {}).oom || {};
  $("#oom").innerHTML = (oom.recent || []).length
    ? oom.recent.map((o) => `<div class="item"><span class="name">${esc(o.process)} <span class="muted mono">${o.pid}</span></span><span class="meta">${o.cgroup ? "cgroup · " : ""}${fmt.ago(o.ts)}</span></div>`).join("")
    : `<div class="empty">Keine OOM-Kills${pr.oom_kills_boot != null ? ` (seit Boot: ${pr.oom_kills_boot})` : ""}.</div>`;
}

function renderNvme(s) {
  const nv = s.nvme || {}, sm = nv.smart, c = (nv.controllers || [])[0] || {};
  const st = $("#nvme-status");
  const rows = [["Modell", c.model || "–"], ["Firmware", c.firmware || "–"], ["Temperatur", `${fmt.n(s.metrics?.nvme_temp, 0)} °C`]];
  if (sm) {
    const bad = sm.critical_warning || sm.media_errors || sm.passed === false || (sm.percentage_used || 0) >= 90;
    st.textContent = bad ? "Warnung" : "gesund"; st.className = `pill ${bad ? "bad" : "ok"}`;
    rows.push(["Verschleiß", `${fmt.n(sm.percentage_used)} %`], ["Reserve", `${fmt.n(sm.available_spare)} % (min. ${fmt.n(sm.spare_threshold)} %)`],
      ["Geschrieben", fmt.bytes(sm.data_written, 2)], ["Gelesen", fmt.bytes(sm.data_read, 2)],
      ["Betriebsstunden", `${fmt.n(sm.power_on_hours)} h`], ["Einschaltvorgänge", fmt.n(sm.power_cycles)],
      ["Unsichere Abschaltungen", fmt.n(sm.unsafe_shutdowns)], ["Medienfehler", fmt.n(sm.media_errors)],
      ["Fehlerlog-Einträge", fmt.n(sm.error_log_entries)], ["Kritische Warnung", sm.critical_warning ? `0x${sm.critical_warning.toString(16)}` : "keine"]);
    $("#nvme-note").textContent = `SMART-Daten ${fmt.ago(sm.updated)} aktualisiert${sm.age > 7200 ? " (veraltet: Host-Job prüfen)" : ""}.`;
  } else {
    st.textContent = "kein SMART"; st.className = "pill";
    $("#nvme-note").innerHTML = nv.smart_error === "missing"
      ? `SMART-Werte (Verschleiß, geschriebene TB, Fehler) brauchen Root-Rechte. Ein Cronjob auf metis kann sie nach <span class="mono">data/smart.json</span> schreiben (siehe README).`
      : `SMART: ${esc(nv.smart_error || "–")}`;
  }
  $("#nvme-info").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
}

function renderPing(s) {
  const p = s.ping || {};
  setText("ping-ts", p.checked ? fmt.time(p.checked) : "");
  $("#ping").innerHTML = (p.targets || []).length ? p.targets.map((t) => {
    const cls = !t.ok ? "bad" : t.loss > 0 ? "warn" : "ok";
    return `<div class="item"><span class="name"><span class="st ${cls}"></span>${esc(t.name)} <span class="muted mono">${esc(t.host || "")}</span></span>
      <span class="meta">${t.ok ? `${fmt.n(t.avg, t.avg < 10 ? 2 : 1)} ms · ±${fmt.n(t.jitter, 2)}` : esc(t.error || "nicht erreichbar")}<br><span class="${t.loss ? "code-bad" : "muted"}">${fmt.n(t.loss)} % Verlust</span></span></div>`;
  }).join("") : `<div class="empty">${esc(p.error || "wird gemessen…")}</div>`;
}

function renderJournal(s) {
  const e = (s.journal || {}).errors;
  if (!e) { $("#jr-sources").innerHTML = `<div class="empty">wird gelesen…</div>`; return; }
  setText("jr-count", `${fmt.n(e.count_24h)} in 24 h · ${fmt.n(e.count_1h)} in 1 h`);
  const max = Math.max(1, ...e.by_source.map((x) => x.count));
  $("#jr-sources").innerHTML = e.by_source.length ? e.by_source.map((x) => `<div class="item"><span class="name mono">${esc(x.source)}</span><span class="bar"><i style="width:${(x.count / max) * 100}%;background:var(--bad)"></i></span><span class="meta">${fmt.n(x.count)}</span></div>`).join("")
    : `<div class="empty">Keine Fehler in 24 h.</div>`;
  $("#jr-recent").innerHTML = e.recent.length ? e.recent.map((r) => `<div class="item"><div class="j-meta"><span>${esc(r.source)} · ${esc(r.prio)}</span><span>${new Date(r.ts * 1000).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</span></div><div class="j-msg">${esc(r.msg)}</div></div>`).join("")
    : `<div class="empty">–</div>`;
}

function renderSsh(s) {
  const x = (s.journal || {}).ssh;
  if (!x) return;
  const st = $("#ssh-status");
  st.textContent = x.failed_24h ? `${x.failed_24h} Fehlversuche` : "ruhig"; st.className = `pill ${x.failed_24h >= 20 ? "bad" : x.failed_24h ? "warn" : "ok"}`;
  $("#ssh-kpis").innerHTML = kv("Logins 7 T", fmt.n(x.logins_7d)) + kv("Fehlversuche 24 h", fmt.n(x.failed_24h)) + kv("Fehlversuche 7 T", fmt.n(x.failed_7d));
  $("#ssh-logins").innerHTML = x.recent_logins.length ? x.recent_logins.slice(0, 5).map((l) => `<div class="item"><span class="name">${esc(l.user)} <span class="muted mono">${esc(l.name || l.ip)}</span></span><span class="meta">${esc(l.method)} · ${fmt.ago(l.ts)}</span></div>`).join("")
    : `<div class="empty">Keine Logins in 7 Tagen.</div>`;
  $("#ssh-fails").innerHTML = x.fail_sources.length ? x.fail_sources.map((f) => `<div class="item"><span class="name mono">${esc(f.name || f.ip)}${f.name ? ` <span class="muted">${esc(f.ip)}</span>` : ""}</span><span class="meta">${fmt.n(f.count)}</span></div>`).join("")
    : `<div class="empty">Keine Fehlversuche.</div>`;
}

function renderVoice(s) {
  const v = s.voice || {}, svc = Object.fromEntries((s.services || []).map((x) => [x.key, x]));
  const entries = Object.entries(v.services || {});
  $("#voice-body").innerHTML = entries.length ? entries.map(([k, x]) => {
    const c = svc[k] || {};
    const max = Math.max(1, ...x.hourly);
    return `<div class="voice-card"><div class="v-head"><span class="v-name"><span class="st ${c.ok ? "ok" : "bad"}"></span>${esc(x.name)}</span><span class="muted mono" title="${esc(c.info || "")}">${c.ok ? `${fmt.n(c.latency_ms, 0)} ms` : "offline"}</span></div>
      <dl class="dl"><dt>Modell</dt><dd title="${esc(c.info || "")}">${esc(String(c.info || "–").slice(0, 28))}</dd>
      <dt>Anfragen 1 h / 24 h</dt><dd>${fmt.n(x.requests_1h)} / ${fmt.n(x.requests_24h)}</dd><dt>7 Tage</dt><dd>${fmt.n(x.requests_7d)}</dd>
      <dt>Ø Dauer</dt><dd>${x.avg_duration_24h != null ? fmt.secs(x.avg_duration_24h) : "–"}</dd><dt>Zuletzt</dt><dd>${x.active ? `<span class="code-ok">aktiv</span>` : fmt.ago(x.last_use)}</dd>
      <dt>Clients</dt><dd>${x.clients.length ? x.clients.map((cl) => esc(cl.name || cl.ip)).join(", ") : "–"}</dd></dl>
      <div class="bars">${x.hourly.map((n, i) => `<i style="height:${(n / max) * 100}%" title="${23 - i === 0 ? "letzte Stunde" : `vor ${23 - i} h`}: ${n}"></i>`).join("")}</div></div>`;
  }).join("") : `<div class="empty">${esc(v.error || "Keine Sprachdienste konfiguriert.")}</div>`;
}

function renderAiEnergy(s) {
  const a = s.energy_attr || {};
  if (!a.available) {
    $("#ae-kpis").innerHTML = ""; $("#ae-models").innerHTML = ""; setText("ae-base", "");
    $("#ae-note").textContent = a.reason || a.error || "";
    return;
  }
  const price = s.power?.price_eur_kwh;
  setText("ae-base", `Leerlauf ≈ ${fmt.n(a.baseline_w, 1)} W`);
  $("#ae-kpis").innerHTML = kv("Ø pro Antwort", a.wh_per_reply != null ? fmt.n(a.wh_per_reply, 2) : "–", "Wh") +
    kv("Antworten gemessen", fmt.n(a.replies)) + kv("Summe", fmt.n(a.wh_total, 1), "Wh") +
    kv("Ollama-Anfragen 24 h", `${fmt.n(a.ollama_wh_24h, 1)}<small>Wh · ${fmt.n(a.ollama_requests_24h)}×</small>`);
  $("#ae-models").innerHTML = a.models.length
    ? `<tr><th>Modell</th><th class="num">Antworten</th><th class="num">Wh / Antwort</th><th class="num">Wh / 1k Tokens</th><th class="num">Ø Dauer</th><th class="num">Ø Mehrleistung</th>${price != null ? `<th class="num">€ / 100 Antw.</th>` : ""}</tr>` +
      a.models.map((m) => `<tr><td>${esc(m.model)}</td><td class="num">${fmt.n(m.replies)}</td><td class="num">${fmt.n(m.wh_per_reply, 2)}</td><td class="num">${fmt.n(m.wh_per_1k_tokens, 2)}</td>
        <td class="num">${fmt.secs(m.avg_seconds)}</td><td class="num">${fmt.n(m.avg_w, 0)} W</td>${price != null ? `<td class="num">${fmt.n(m.wh_per_reply * 100 / 1000 * price, 2)}</td>` : ""}</tr>`).join("")
    : `<tr><td class="empty">Noch keine Open-WebUI-Antworten mit Leistungsmessung.</td></tr>`;
  $("#ae-note").textContent = `Mehrverbrauch über Leerlauf während der Antwortzeit (Leerlauf = 10. Perzentil der letzten 24 h). Gemessen ab ${new Date(a.since * 1000).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}, aktualisiert alle 5 min.`;
}

/* ------------------------------------------------------------------ mqtt */
async function loadBrokers() {
  const r = await fetch("/api/brokers"); state.brokers = await r.json();
  renderBrokers();
}

function renderBrokers() {
  const b = state.brokers; if (!b) return;
  $("#btn-add").disabled = !b.editable;
  $("#mqtt-note").innerHTML = b.editable
    ? `Werte werden als JSON nach <span class="mono">&lt;präfix&gt;/state</span> gesendet. Mit HA-Discovery erscheint <b>${esc(b.node_id)}</b> automatisch als Gerät in Home Assistant.`
    : `Bearbeiten ist deaktiviert: <span class="mono">ADMIN_PASSWORD</span> in der <span class="mono">.env</span> setzen.`;
  $("#brokers").innerHTML = b.brokers.length ? b.brokers.map((x) => {
    const st = (state.snap?.mqtt || b.status || {})[x.id];
    const pill = !x.enabled ? `<span class="pill">deaktiviert</span>` : st?.connected ? `<span class="pill ok">verbunden</span>` : `<span class="pill bad">getrennt</span>`;
    return `<div class="broker" data-id="${esc(x.id)}"><div class="b-head"><span class="b-name">${esc(x.name)}</span>${pill}</div>
      <dl class="dl">
        <dt>Adresse</dt><dd>${esc(x.host)}:${x.port}${x.tls ? " (TLS)" : ""}</dd>
        <dt>Benutzer</dt><dd>${esc(x.username || "–")}</dd>
        <dt>Topic</dt><dd>${esc(x.base_topic)}/state</dd>
        <dt>Discovery</dt><dd>${x.discovery ? esc(x.discovery_prefix) : "aus"}</dd>
        <dt>Intervall</dt><dd>${x.interval} s</dd>
        <dt>Zuletzt gesendet</dt><dd class="b-last">${st?.last_publish ? fmt.ago(st.last_publish) : "–"}</dd>
        <dt>Nachrichten</dt><dd class="b-sent">${fmt.n(st?.sent)}</dd>
      </dl>
      ${st?.last_error ? `<div class="err">${esc(st.last_error)}</div>` : ""}
      ${b.editable ? `<div class="b-actions"><button class="btn" data-edit="${esc(x.id)}">Bearbeiten</button></div>` : ""}</div>`;
  }).join("") : `<div class="empty">Noch kein Broker eingerichtet.</div>`;
}

function renderMqttStatus(st) {
  if (!state.brokers) return;
  for (const el of document.querySelectorAll(".broker")) {
    const s = st[el.dataset.id]; const x = state.brokers.brokers.find((b) => b.id === el.dataset.id);
    if (!x) continue;
    const pill = $(".pill", el);
    if (x.enabled) { pill.textContent = s?.connected ? "verbunden" : "getrennt"; pill.className = `pill ${s?.connected ? "ok" : "bad"}`; }
    $(".b-last", el).textContent = s?.last_publish ? fmt.ago(s.last_publish) : "–";
    $(".b-sent", el).textContent = fmt.n(s?.sent);
    let err = $(".err", el);
    if (s?.last_error) { if (!err) { err = document.createElement("div"); err.className = "err"; $(".dl", el).after(err); } err.textContent = s.last_error; }
    else if (err) err.remove();
  }
}

const dlg = $("#dlg"), form = $("#broker-form");
const adminPw = { get: () => { try { return sessionStorage.getItem("mm-admin") || ""; } catch { return ""; } }, set: (v) => { try { sessionStorage.setItem("mm-admin", v); } catch { /* ignore */ } } };

function openDialog(b) {
  state.editing = b?.id || null;
  form.reset();
  $("#dlg-title").textContent = b ? `${b.name} bearbeiten` : "Broker hinzufügen";
  const d = b || { port: 1883, base_topic: state.brokers?.node_id || "metis", interval: 10, discovery_prefix: "homeassistant", enabled: true, discovery: true };
  for (const el of form.elements) {
    if (!el.name || el.name === "admin") continue;
    if (el.type === "checkbox") el.checked = !!d[el.name];
    else if (el.name === "password") { el.value = ""; el.placeholder = b?.has_password ? "unverändert" : "(leer)"; }
    else el.value = d[el.name] ?? "";
  }
  form.elements.admin.value = adminPw.get();
  $("#dlg-delete").style.display = b ? "" : "none";
  setMsg("");
  dlg.showModal();
}
function setMsg(t, cls = "") { const m = $("#dlg-msg"); m.textContent = t; m.className = `dlg-msg ${cls}`; }
function formBroker() {
  const f = form.elements, o = {};
  for (const k of ["id", "name", "host", "username", "base_topic", "discovery_prefix"]) o[k] = f[k].value.trim();
  o.port = +f.port.value; o.interval = +f.interval.value;
  for (const k of ["enabled", "discovery", "tls", "tls_insecure"]) o[k] = f[k].checked;
  const pw = f.password.value;
  o.password = pw === "" && state.editing ? null : pw;   // null = keep stored password
  if (!o.id) delete o.id;
  return o;
}
async function api(method, url, body) {
  const pw = form.elements.admin.value;
  const r = await fetch(url, { method, headers: { "content-type": "application/json", "x-admin-password": pw }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  adminPw.set(pw);
  return j;
}
async function saveBrokers(list) {
  state.brokers = await api("PUT", "/api/brokers", list);
  renderBrokers();
}
const currentList = () => state.brokers.brokers.map((b) => ({ ...b, password: null }));

form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const b = formBroker();
  const list = currentList();
  const i = list.findIndex((x) => x.id === state.editing);
  if (i >= 0) list[i] = { ...b, id: state.editing }; else list.push(b);
  try { setMsg("Speichere…"); await saveBrokers(list); dlg.close(); } catch (e) { setMsg(e.message, "bad"); }
});
$("#dlg-test").addEventListener("click", async () => {
  setMsg("Teste Verbindung…");
  try { const r = await api("POST", "/api/brokers/test", { ...formBroker(), id: state.editing }); setMsg(r.message, r.ok ? "ok" : "bad"); }
  catch (e) { setMsg(e.message, "bad"); }
});
$("#dlg-delete").addEventListener("click", async () => {
  if ($("#dlg-delete").dataset.confirm !== "1") { $("#dlg-delete").dataset.confirm = "1"; $("#dlg-delete").textContent = "Wirklich löschen?"; return; }
  try { await saveBrokers(currentList().filter((x) => x.id !== state.editing)); dlg.close(); } catch (e) { setMsg(e.message, "bad"); }
});
dlg.addEventListener("close", () => { const d = $("#dlg-delete"); d.dataset.confirm = ""; d.textContent = "Löschen"; });
$("#dlg-cancel").addEventListener("click", () => dlg.close());
$("#btn-add").addEventListener("click", () => openDialog(null));
$("#brokers").addEventListener("click", (ev) => {
  const id = ev.target.dataset?.edit; if (!id) return;
  openDialog(state.brokers.brokers.find((b) => b.id === id));
});

const dlgP = $("#dlg-power"), formP = $("#power-form");
$("#btn-power").addEventListener("click", async () => {
  const r = await (await fetch("/api/power")).json();
  const s = r.settings || {};
  formP.reset();
  formP.elements.host.value = s.host || "";
  formP.elements.channel.value = s.channel ?? 0;
  formP.elements.password.placeholder = s.has_password ? "unverändert" : "(kein Passwort)";
  formP.elements.price_eur_kwh.value = s.price_eur_kwh != null ? String(s.price_eur_kwh).replace(".", ",") : "";
  formP.elements.admin.value = adminPw.get();
  $("#power-msg").textContent = r.editable ? "" : "Bearbeiten deaktiviert: ADMIN_PASSWORD fehlt.";
  dlgP.showModal();
});
$("#power-cancel").addEventListener("click", () => dlgP.close());
formP.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = formP.elements, msg = $("#power-msg");
  const body = { host: f.host.value.trim(), channel: +f.channel.value || 0, price_eur_kwh: f.price_eur_kwh.value.trim() || null,
    password: f.password.value === "" ? null : f.password.value };
  try {
    const r = await fetch("/api/power", { method: "PUT", headers: { "content-type": "application/json", "x-admin-password": f.admin.value }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || r.statusText);
    adminPw.set(f.admin.value);
    dlgP.close();
  } catch (e) { msg.textContent = e.message; msg.className = "dlg-msg bad"; }
});

async function loadSensors() {
  const s = await (await fetch("/api/sensors")).json();
  setText("sensor-count", s.length);
  $("#sensors").innerHTML = `<tr><th>JSON-Key</th><th>Name</th><th>Einheit</th><th>Typ</th></tr>` +
    s.map((x) => `<tr><td class="mono sm">${esc(x.key)}</td><td>${esc(x.name)}</td><td class="sm">${esc(x.unit || "")}</td><td class="sm">${esc(x.component)}${x.device_class ? ` · ${esc(x.device_class)}` : ""}</td></tr>`).join("");
}

/* ------------------------------------------------------------------ wiring */
$("#range").addEventListener("click", (ev) => {
  const r = ev.target.dataset?.r; if (!r || r === state.range) return;
  state.range = r;
  for (const b of document.querySelectorAll("#range button")) b.classList.toggle("on", b.dataset.r === r);
  loadHistory();
});
$("#en-range").addEventListener("click", (ev) => {
  const r = ev.target.dataset?.r; if (!r || r === state.enRange) return;
  state.enRange = r;
  for (const b of document.querySelectorAll("#en-range button")) b.classList.toggle("on", b.dataset.r === r);
  loadEnHistory();
});
setInterval(() => { if (state.range !== "1h") loadHistory(); if (state.enRange !== "1h") loadEnHistory(); }, 60000);
let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawCharts, 120); });

// active nav highlight
const navLinks = [...document.querySelectorAll("#nav a")];
const io = new IntersectionObserver((ents) => {
  for (const e of ents) if (e.isIntersecting) navLinks.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${e.target.id}`));
}, { rootMargin: "-40% 0px -55% 0px" });
["overview", "energy", "ai", "system", "mqtt"].forEach((id) => io.observe(document.getElementById(id)));

function connect() {
  const live = $("#live");
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { live.className = "live on"; setText("live-txt", "live"); };
  ws.onmessage = (ev) => { try { render(JSON.parse(ev.data)); setText("live-txt", `live · ${fmt.time(Date.now() / 1000)}`); } catch (e) { console.error(e); } };
  ws.onclose = () => { live.className = "live off"; setText("live-txt", "getrennt – verbinde neu…"); setTimeout(connect, 3000); };
}

(async () => {
  await Promise.all([loadHistory(), loadEnHistory(), loadBrokers(), loadSensors()]);
  try { render(await (await fetch("/api/snapshot")).json()); } catch (e) { console.warn(e); }
  connect();
})();
