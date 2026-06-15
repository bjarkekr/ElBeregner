'use strict';

// ─── Config ────────────────────────────────────────────────────────────────
const DEFAULT_BACKEND = '';
const STORAGE_KEY = 'elberegner_settings';

function loadSettings() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
  } catch {
    return {};
  }
}

function saveSettings(s) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
}

let settings = loadSettings();

function backendUrl() {
  return (settings.backendUrl || DEFAULT_BACKEND).replace(/\/$/, '');
}

// ─── Helpers ───────────────────────────────────────────────────────────────
const MÅNEDER = [
  'Januar','Februar','Marts','April','Maj','Juni',
  'Juli','August','September','Oktober','November','December'
];

function fmtKr(v) {
  return v == null ? '—' : v.toLocaleString('da-DK', {minimumFractionDigits:2, maximumFractionDigits:2}) + ' kr.';
}

function fmtKwh(v) {
  return v == null ? '—' : v.toLocaleString('da-DK', {minimumFractionDigits:1, maximumFractionDigits:1}) + ' kWh';
}

function fmtOre(v) {
  if (v == null) return '—';
  return (v * 100).toLocaleString('da-DK', {minimumFractionDigits:1, maximumFractionDigits:1}) + ' øre/kWh';
}

function pad(n) { return String(n).padStart(2, '0'); }

// Convert UTC ISO string to DK local hour label (CEST/CET)
function utcToDkLabel(isoUtc) {
  const d = new Date(isoUtc);
  return d.toLocaleString('da-DK', {
    timeZone: 'Europe/Copenhagen',
    hour: '2-digit',
    minute: '2-digit',
    day: '2-digit',
    month: '2-digit',
  });
}

function utcToHourLabel(isoUtc) {
  const d = new Date(isoUtc);
  return d.toLocaleString('da-DK', {
    timeZone: 'Europe/Copenhagen',
    hour: '2-digit',
    minute: '2-digit',
  });
}

async function apiFetch(path) {
  const base = backendUrl();
  if (!base) throw new Error('Backend URL er ikke konfigureret. Gå til Indstillinger og angiv din Railway-URL.');
  const headers = {};
  if (settings.apiKey) headers['X-API-Key'] = settings.apiKey;
  const resp = await fetch(base + path, { headers });
  if (!resp.ok) {
    let msg;
    try { msg = (await resp.json()).detail; } catch { msg = resp.statusText; }
    throw new Error(msg || `Serverfejl ${resp.status}`);
  }
  return resp.json();
}

// ─── Tab navigation ────────────────────────────────────────────────────────
let currentTab = 'maaned';

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(s => {
    s.classList.toggle('active', s.id === `tab-${tab}`);
    s.classList.toggle('hidden', s.id !== `tab-${tab}`);
  });
  if (tab === 'historik' && !historikLoaded) loadHistorik();
  if (tab === 'priser') loadPriser();
}

document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// ─── MÅNED VIEW ────────────────────────────────────────────────────────────
let currentYear = new Date().getFullYear();
let currentMonth = new Date().getMonth() + 1; // 1-based
let maanedChart = null;
let maanedCache = {};

function updateMonthLabel() {
  document.getElementById('month-label').textContent =
    `${MÅNEDER[currentMonth - 1]} ${currentYear}`;
  // Disable next-month if it would be in the future
  const now = new Date();
  const isCurrentOrFuture = currentYear > now.getFullYear() ||
    (currentYear === now.getFullYear() && currentMonth >= now.getMonth() + 1);
  document.getElementById('next-month').disabled = isCurrentOrFuture;
}

document.getElementById('prev-month').addEventListener('click', () => {
  currentMonth--;
  if (currentMonth < 1) { currentMonth = 12; currentYear--; }
  updateMonthLabel();
  loadMaaned();
});

document.getElementById('next-month').addEventListener('click', () => {
  currentMonth++;
  if (currentMonth > 12) { currentMonth = 1; currentYear++; }
  updateMonthLabel();
  loadMaaned();
});

function showMaanedState(state, msg) {
  const spinner = document.getElementById('maaned-spinner');
  const error = document.getElementById('maaned-error');
  const content = document.getElementById('maaned-content');
  spinner.classList.toggle('hidden', state !== 'loading');
  error.classList.toggle('hidden', state !== 'error');
  content.classList.toggle('hidden', state !== 'data');
  if (state === 'error') error.textContent = msg || 'Ukendt fejl';
}

async function loadMaaned() {
  const cacheKey = `${currentYear}-${pad(currentMonth)}`;
  showMaanedState('loading');

  try {
    let data = maanedCache[cacheKey];
    if (!data) {
      data = await apiFetch(`/api/maaned?aar=${currentYear}&maaned=${currentMonth}`);
      maanedCache[cacheKey] = data;
    }
    renderMaaned(data);
  } catch (err) {
    showMaanedState('error', err.message);
  }
}

function renderMaaned(data) {
  showMaanedState('data');

  document.getElementById('stat-kwh').textContent = fmtKwh(data.total_kwh);
  document.getElementById('stat-kr').textContent = fmtKr(data.total_kr);
  document.getElementById('stat-spot').textContent = fmtOre(data.gns_spotpris_kwh);
  document.getElementById('stat-timer').textContent = data.timer.length;

  // Manglende timer advarsel
  const advarsel = document.getElementById('manglende-advarsel');
  if (data.manglende_timer_antal > 0) {
    advarsel.textContent = `Bemærk: ${data.manglende_timer_antal} timer mangler data og er ikke medregnet.`;
    advarsel.classList.remove('hidden');
  } else {
    advarsel.classList.add('hidden');
  }

  renderMaanedChart(data.timer);
}

function renderMaanedChart(timer) {
  // Sample every hour for display (max 744 points)
  const labels = timer.map(t => utcToHourLabel(t.time));
  const kwh = timer.map(t => t.kwh);
  const spot = timer.map(t => parseFloat((t.spotpris_kwh * 100).toFixed(2))); // convert to øre

  if (maanedChart) { maanedChart.destroy(); maanedChart = null; }

  const ctx = document.getElementById('maaned-chart').getContext('2d');
  maanedChart = new Chart(ctx, {
    data: {
      labels,
      datasets: [
        {
          type: 'bar',
          label: 'Forbrug (kWh)',
          data: kwh,
          backgroundColor: 'rgba(76, 175, 80, 0.6)',
          borderColor: '#2e7d32',
          borderWidth: 0,
          yAxisID: 'yKwh',
          order: 2,
        },
        {
          type: 'line',
          label: 'Spotpris (øre/kWh)',
          data: spot,
          borderColor: '#e65100',
          backgroundColor: 'rgba(230, 81, 0, 0.08)',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.3,
          yAxisID: 'ySpot',
          order: 1,
          fill: true,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top' },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.datasetIndex === 0) return ` ${ctx.parsed.y.toFixed(3)} kWh`;
              return ` ${ctx.parsed.y.toFixed(1)} øre/kWh`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            maxTicksLimit: 24,
            font: { size: 10 },
          },
        },
        yKwh: {
          type: 'linear',
          position: 'left',
          title: { display: true, text: 'kWh', font: { size: 11 } },
          grid: { color: 'rgba(0,0,0,0.05)' },
        },
        ySpot: {
          type: 'linear',
          position: 'right',
          title: { display: true, text: 'Øre/kWh', font: { size: 11 } },
          grid: { display: false },
        },
      },
    },
  });

  // Fix chart canvas height
  document.querySelector('.chart-wrap').style.height = '300px';
}

// ─── HISTORIK VIEW ─────────────────────────────────────────────────────────
let historikLoaded = false;
let historikChart = null;

async function loadHistorik() {
  historikLoaded = true;
  const spinner = document.getElementById('historik-spinner');
  const error = document.getElementById('historik-error');
  const content = document.getElementById('historik-content');

  spinner.classList.remove('hidden');
  error.classList.add('hidden');
  content.classList.add('hidden');

  const now = new Date();
  const tasks = [];
  for (let i = 11; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    tasks.push({ aar: d.getFullYear(), maaned: d.getMonth() + 1 });
  }

  try {
    const results = await Promise.allSettled(
      tasks.map(t => apiFetch(`/api/maaned?aar=${t.aar}&maaned=${t.maaned}&kun_cache=true`))
    );

    const rows = [];
    results.forEach((r, i) => {
      if (r.status === 'fulfilled') rows.push({ ...tasks[i], ...r.value });
    });

    if (rows.length === 0) throw new Error('Ingen data tilgængelig for de seneste 12 måneder.');

    renderHistorik(rows);
    spinner.classList.add('hidden');
    content.classList.remove('hidden');
  } catch (err) {
    spinner.classList.add('hidden');
    error.classList.remove('hidden');
    error.textContent = err.message;
  }
}

function renderHistorik(rows) {
  const labels = rows.map(r => `${MÅNEDER[r.maaned - 1].slice(0,3)} ${r.aar}`);
  const krData = rows.map(r => r.total_kr);
  const kwhData = rows.map(r => r.total_kwh);

  if (historikChart) { historikChart.destroy(); historikChart = null; }

  const ctx = document.getElementById('historik-chart').getContext('2d');
  historikChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'Total (kr.)',
          data: krData,
          backgroundColor: 'rgba(46, 125, 50, 0.75)',
          borderColor: '#1b5e20',
          borderWidth: 1,
          yAxisID: 'yKr',
        },
        {
          type: 'line',
          label: 'Forbrug (kWh)',
          data: kwhData,
          borderColor: '#1565c0',
          backgroundColor: 'transparent',
          borderWidth: 2,
          pointRadius: 4,
          tension: 0.3,
          yAxisID: 'yKwh',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: 'top' } },
      scales: {
        yKr: {
          type: 'linear',
          position: 'left',
          title: { display: true, text: 'kr.' },
        },
        yKwh: {
          type: 'linear',
          position: 'right',
          title: { display: true, text: 'kWh' },
          grid: { display: false },
        },
      },
    },
  });

  // Table
  const tbody = document.getElementById('historik-tbody');
  tbody.innerHTML = '';
  [...rows].reverse().forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${MÅNEDER[r.maaned - 1]} ${r.aar}</td>
      <td>${r.total_kwh != null ? r.total_kwh.toLocaleString('da-DK', {maximumFractionDigits:1}) : '—'}</td>
      <td>${r.gns_spotpris_kwh != null ? (r.gns_spotpris_kwh * 100).toLocaleString('da-DK', {maximumFractionDigits:1}) + ' øre' : '—'}</td>
      <td><strong>${r.total_kr != null ? r.total_kr.toLocaleString('da-DK', {minimumFractionDigits:2, maximumFractionDigits:2}) + ' kr.' : '—'}</strong></td>
    `;
    tbody.appendChild(tr);
  });

  document.querySelector('#tab-historik .chart-wrap').style.height = '280px';
}

// ─── PRISER VIEW ───────────────────────────────────────────────────────────

function formatDagLabel(isoDate) {
  const d = new Date(isoDate + 'T12:00:00');
  return d.toLocaleDateString('da-DK', { weekday: 'long', day: 'numeric', month: 'long' });
}

function prisKlasse(value, alle) {
  const sorted = [...alle].sort((a, b) => a - b);
  const p33 = sorted[Math.floor(sorted.length / 3)];
  const p67 = sorted[Math.floor(2 * sorted.length / 3)];
  if (value <= p33) return 'pris-billig';
  if (value <= p67) return 'pris-middel';
  return 'pris-dyr';
}

function buildTooltip(t, afgifter) {
  const f = (v) => v.toLocaleString('da-DK', {minimumFractionDigits:1, maximumFractionDigits:1});
  const spotOre = t.spot_dkk_kwh * 100;
  const momsOeOreSpot = spotOre * (afgifter.moms_pct / 100);
  const spotInklMoms = spotOre + momsOeOreSpot;
  const total = t.total_dkk_kwh * 100;
  const lines = [
    `Spotpris:              ${f(spotOre)} øre`,
    `  + moms (${f(afgifter.moms_pct)}%):     ${f(momsOeOreSpot)} øre`,
    `  = spot inkl. moms:  ${f(spotInklMoms)} øre`,
    ``,
    `Elafgift:              ${f(afgifter.elafgift_ore)} øre`,
    `Nettarif:              ${f(t.nettarif_ore)} øre`,
    `Systemtarif:           ${f(afgifter.systemtarif_ore)} øre`,
    `Transmissionstarif:    ${f(afgifter.transmissionstarif_ore)} øre`,
  ];
  if (afgifter.elselskab_tillæg_ore > 0) {
    lines.push(`Elselskabstillæg:      ${f(afgifter.elselskab_tillæg_ore)} øre`);
  }
  lines.push(``, `I alt:                 ${f(total)} øre/kWh`);
  return lines.join('\n');
}

function renderPrisTabel(tbodyId, timer, afgifter) {
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML = '';
  const alleTotal = timer.map(t => t.total_dkk_kwh);
  timer.forEach(t => {
    const h = parseInt(t.time_dk.split(':')[0]);
    const tidLabel = `${pad(h)}–${pad((h + 1) % 24)}`;
    const cls = prisKlasse(t.total_dkk_kwh, alleTotal);
    const tr = document.createElement('tr');
    tr.className = cls + (t.er_nu ? ' pris-nu' : '');

    const totalTd = document.createElement('td');
    totalTd.className = 'pris-tooltip';
    totalTd.textContent = t.total_dkk_kwh.toLocaleString('da-DK', {minimumFractionDigits:2, maximumFractionDigits:2});
    if (afgifter) totalTd.setAttribute('data-tooltip', buildTooltip(t, afgifter));

    tr.innerHTML = `
      <td>${tidLabel}${t.er_nu ? ' <span class="nu-badge">nu</span>' : ''}</td>
      <td>${(t.spot_dkk_kwh * 100).toLocaleString('da-DK', {minimumFractionDigits:1, maximumFractionDigits:1})}</td>
    `;
    tr.appendChild(totalTd);
    tbody.appendChild(tr);
  });
}

async function loadPriser() {
  document.getElementById('priser-spinner').classList.remove('hidden');
  document.getElementById('priser-error').classList.add('hidden');
  document.getElementById('priser-content').classList.add('hidden');

  try {
    const data = await apiFetch('/api/priser/dag');

    document.getElementById('priser-idag-label').textContent = formatDagLabel(data.i_dag);
    renderPrisTabel('priser-idag-tbody', data.i_dag_timer, data.afgifter);

    document.getElementById('priser-imorgen-label').textContent = formatDagLabel(data.i_morgen);

    if (data.i_morgen_tilgængelig) {
      document.getElementById('priser-imorgen-mangler').classList.add('hidden');
      document.getElementById('priser-imorgen-tabel-wrap').classList.remove('hidden');
      renderPrisTabel('priser-imorgen-tbody', data.i_morgen_timer, data.afgifter);
    } else {
      const manglerEl = document.getElementById('priser-imorgen-mangler');
      manglerEl.textContent = 'Morgendagens priser offentliggøres normalt efter kl. 13:00.';
      manglerEl.classList.remove('hidden');
      document.getElementById('priser-imorgen-tabel-wrap').classList.add('hidden');
    }

    document.getElementById('priser-spinner').classList.add('hidden');
    document.getElementById('priser-content').classList.remove('hidden');
  } catch (err) {
    document.getElementById('priser-spinner').classList.add('hidden');
    document.getElementById('priser-error').classList.remove('hidden');
    document.getElementById('priser-error').textContent = err.message;
  }
}

document.getElementById('priser-refresh').addEventListener('click', loadPriser);

// ─── INDSTILLINGER VIEW ────────────────────────────────────────────────────
function loadSettingsForm() {
  document.getElementById('backend-url').value = settings.backendUrl || '';
  document.getElementById('api-key').value = settings.apiKey || '';
  document.getElementById('priszone-select').value = settings.zone || 'DK1';
}

document.getElementById('settings-form').addEventListener('submit', (e) => {
  e.preventDefault();
  settings.backendUrl = document.getElementById('backend-url').value.trim();
  settings.apiKey = document.getElementById('api-key').value.trim();
  settings.zone = document.getElementById('priszone-select').value;
  saveSettings(settings);

  // Clear caches so next load uses new settings
  maanedCache = {};
  historikLoaded = false;

  // Update zone badge
  document.getElementById('zone-badge').textContent = settings.zone || 'DK1';

  const fb = document.getElementById('settings-feedback');
  fb.textContent = 'Indstillinger gemt!';
  fb.className = 'feedback success';
  fb.classList.remove('hidden');
  setTimeout(() => fb.classList.add('hidden'), 3000);

  switchTab('maaned');
  loadMaaned();
});

// ─── DEBUG ─────────────────────────────────────────────────────────────────
document.getElementById('debug-dato').value = (() => {
  const d = new Date();
  d.setDate(d.getDate() - 3);
  return d.toISOString().slice(0, 10);
})();

document.getElementById('debug-btn').addEventListener('click', async () => {
  const dato = document.getElementById('debug-dato').value;
  const out = document.getElementById('debug-output');
  out.textContent = 'Henter…';
  out.classList.remove('hidden');
  try {
    const data = await apiFetch(`/api/debug/forbrug?dato=${dato}`);
    out.textContent = JSON.stringify(data, null, 2);
  } catch (err) {
    out.textContent = 'Fejl: ' + err.message;
  }
});

// ─── Init ──────────────────────────────────────────────────────────────────
function init() {
  updateMonthLabel();
  loadSettingsForm();
  document.getElementById('zone-badge').textContent = settings.zone || 'DK1';

  // PWA service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch(err => {
      console.warn('Service worker registrering fejlede:', err);
    });
  }

  // Load first tab
  if (backendUrl()) {
    loadMaaned();
  } else {
    showMaanedState('error',
      'Velkommen! Gå til Indstillinger og angiv din backend URL for at komme i gang.');
  }
}

init();
