// LocalMeetingSum — frontend
(() => {
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
  ws: null,
  utterances: [],
  partialsBySource: {},
  devices: [],          // all WASAPI devices from /api/devices
  sources: new Map(),   // key -> { dev, label, level, lastLevelTs }
  liveActive: false,
  mode: null,
};

// ============== Tabs ==============
$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $$('.tab-pane').forEach(p => p.classList.add('hidden'));
  $(`[data-pane="${t.dataset.tab}"]`).classList.remove('hidden');
}));

// ============== Status helper ==============
function setStatus(text, cls = '') {
  const s = $('#status');
  s.textContent = text;
  s.className = cls;
}

// ============== WebSocket ==============
function connect() {
  return new Promise((resolve, reject) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return resolve();
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { state.ws = ws; resolve(); };
    ws.onerror = (e) => reject(e);
    ws.onclose = () => {
      state.ws = null;
      setStatus('disconnected', '');
      // Clear all source levels on disconnect.
      for (const s of state.sources.values()) s.level = 0;
    };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleEvent(msg);
    };
  });
}

function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(obj));
  }
}

// ============== Event handler ==============
function handleEvent(msg) {
  switch (msg.type) {
    case 'source_added':
      // backend ack — nothing to do
      break;
    case 'source_removed':
      break;
    case 'level': {
      const s = state.sources.get(msg.source);
      if (s) { s.level = msg.level; s.lastLevelTs = performance.now(); }
      break;
    }
    case 'live_started':
      state.liveActive = true;
      setStatus(`live (${msg.sources.length} src)`, 'live');
      $('#btn-start-live').disabled = true;
      $('#btn-stop-live').disabled = false;
      break;
    case 'live_stopped':
      state.liveActive = false;
      setStatus('idle');
      $('#btn-start-live').disabled = state.sources.size === 0;
      $('#btn-stop-live').disabled = true;
      showMergePanel();
      break;
    case 'partial':
      state.partialsBySource[msg.source] = msg.text;
      renderPartials();
      break;
    case 'utterance':
      delete state.partialsBySource[msg.source];
      state.utterances.push(msg);
      renderTranscript();
      renderPartials();
      break;
    case 'progress':
      const pct = msg.percent || 0;
      $('#file-progress').value = pct;
      setStatus(`${msg.stage} ${pct}%`, 'busy');
      break;
    case 'done':
      setStatus('done');
      $('#file-progress').style.display = 'none';
      showMergePanel();
      break;
    case 'error':
      console.error('server error', msg);
      setStatus('error: ' + msg.message, '');
      break;
  }
}

// ============== Device & source management ==============
async function loadDevices() {
  try {
    const r = await fetch('/api/devices');
    const j = await r.json();
    state.devices = j.devices || [];
  } catch (e) {
    setStatus('设备加载失败: ' + e, '');
    state.devices = [];
  }
}

function refreshDeviceDropdown() {
  const kind = document.querySelector('input[name="new-kind"]:checked')?.value || 'input';
  const sel = $('#new-device');
  sel.innerHTML = '';
  const avail = state.devices.filter(d => d.kind === kind && !state.sources.has(d.key));
  if (avail.length === 0) {
    const opt = document.createElement('option');
    opt.textContent = '(没有可用设备)';
    opt.disabled = true;
    sel.append(opt);
    $('#btn-confirm-add').disabled = true;
    return;
  }
  $('#btn-confirm-add').disabled = false;
  for (const d of avail) {
    const opt = document.createElement('option');
    opt.value = d.key;
    opt.textContent = d.name + `  (${d.default_samplerate}Hz·${d.channels}ch)`;
    sel.append(opt);
  }
}

function autoLabel(dev) {
  let n = dev.name.split('(')[0].trim();
  if (n.length > 14) n = n.slice(0, 14);
  return dev.kind === 'loopback' ? n + ':sys' : n;
}

async function addSourceFromForm() {
  const sel = $('#new-device');
  const key = sel.value;
  if (!key) return;
  const dev = state.devices.find(d => d.key === key);
  if (!dev || state.sources.has(key)) return;
  await connect();
  const label = autoLabel(dev);
  state.sources.set(key, { dev, label, level: 0, lastLevelTs: 0 });
  send({ cmd: 'add_source', key, label });
  renderSources();
  $('#add-source-form').classList.add('hidden');
  $('#btn-show-add').classList.remove('hidden');
  $('#btn-start-live').disabled = state.liveActive || state.sources.size === 0;
}

function removeSource(key) {
  state.sources.delete(key);
  send({ cmd: 'remove_source', key });
  renderSources();
  if (state.sources.size === 0 && !state.liveActive) {
    $('#btn-start-live').disabled = true;
  }
}

function renderSources() {
  const root = $('#sources-list');
  root.innerHTML = '';
  for (const [key, s] of state.sources) {
    const row = document.createElement('div');
    row.className = 'source-row';
    row.dataset.key = key;

    const badge = document.createElement('span');
    badge.className = 'kind-badge ' + s.dev.kind;
    badge.textContent = s.dev.kind === 'input' ? '麦克风' : '系统输出';
    const name = document.createElement('span');
    name.className = 'src-name';
    name.title = s.dev.name;
    name.textContent = s.dev.name;
    const label = document.createElement('input');
    label.type = 'text';
    label.className = 'src-label';
    label.value = s.label;
    label.placeholder = '标签';
    label.addEventListener('change', () => {
      s.label = label.value.trim() || s.label;
      send({ cmd: 'relabel', key, label: s.label });
    });

    const meter = document.createElement('div');
    meter.className = 'meter';
    const ticks = document.createElement('div');
    ticks.className = 'meter-ticks';
    const fill = document.createElement('div');
    fill.className = 'meter-fill';
    meter.append(fill, ticks);

    const rm = document.createElement('button');
    rm.className = 'btn-remove';
    rm.type = 'button';
    rm.title = '移除';
    rm.textContent = '×';
    rm.addEventListener('click', () => removeSource(key));

    row.append(badge, name, label, meter, rm);
    root.append(row);
  }
}

// ============== Add-source form controls ==============
$('#btn-show-add').addEventListener('click', async () => {
  await loadDevices();
  refreshDeviceDropdown();
  $('#add-source-form').classList.remove('hidden');
  $('#btn-show-add').classList.add('hidden');
});
$('#btn-cancel-add').addEventListener('click', () => {
  $('#add-source-form').classList.add('hidden');
  $('#btn-show-add').classList.remove('hidden');
});
$('#btn-confirm-add').addEventListener('click', addSourceFromForm);
$$('input[name="new-kind"]').forEach(el => el.addEventListener('change', refreshDeviceDropdown));

// ============== Live capture ==============
$('#btn-start-live').addEventListener('click', async () => {
  if (state.sources.size === 0) { alert('请先添加至少一个音源'); return; }
  state.utterances = [];
  state.partialsBySource = {};
  state.mode = 'live';
  renderTranscript();
  renderPartials();
  $('#panel-merge').classList.add('hidden');
  $('#panel-summary').classList.add('hidden');
  await connect();
  send({ cmd: 'start_live' });
});

$('#btn-stop-live').addEventListener('click', () => {
  send({ cmd: 'stop_live' });
  $('#btn-start-live').disabled = state.sources.size === 0;
  $('#btn-stop-live').disabled = true;
});

// ============== Level meter animation ==============
function tickMeters() {
  const now = performance.now();
  for (const [key, s] of state.sources) {
    const row = document.querySelector(`.source-row[data-key="${CSS.escape(key)}"]`);
    if (!row) continue;
    const fill = row.querySelector('.meter-fill');
    // Decay if no level event for a while.
    const age = now - (s.lastLevelTs || 0);
    let level = s.level || 0;
    if (age > 150) level = level * Math.max(0, 1 - (age - 150) / 800);
    // Linear amplitude → dBFS → percentage (-60..0 dB).
    const db = 20 * Math.log10(Math.max(level, 1e-6));
    const pct = Math.max(0, Math.min(100, (db + 60) * 100 / 60));
    fill.style.width = pct + '%';
    fill.classList.toggle('peak', db > -6);
    fill.classList.toggle('warn', db > -20 && db <= -6);
  }
  requestAnimationFrame(tickMeters);
}
requestAnimationFrame(tickMeters);

// ============== File upload ==============
$('#file-input').addEventListener('change', () => {
  $('#btn-process-file').disabled = !$('#file-input').files[0];
});

$('#btn-process-file').addEventListener('click', async () => {
  const f = $('#file-input').files[0];
  if (!f) return;
  state.utterances = [];
  state.partialsBySource = {};
  state.mode = 'file';
  renderTranscript();
  renderPartials();
  $('#panel-merge').classList.add('hidden');
  $('#panel-summary').classList.add('hidden');

  setStatus('uploading…', 'busy');
  $('#file-progress').style.display = 'block';
  $('#file-progress').value = 0;
  const fd = new FormData();
  fd.append('file', f);
  let sid;
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const j = await r.json();
    sid = j.session_id;
  } catch (e) {
    setStatus('upload failed', ''); return;
  }
  await connect();
  const label = $('#file-label').value.trim() || 'File';
  send({ cmd: 'process_file', session_id: sid, label });
});

// ============== Rendering ==============
function renderPartials() {
  const root = $('#partials');
  root.innerHTML = '';
  const keys = Object.keys(state.partialsBySource);
  if (!keys.length) { root.textContent = ''; return; }
  for (const k of keys) {
    const row = document.createElement('div');
    row.className = 'partial-row';
    const src = document.createElement('span');
    src.className = 'src';
    const s = state.sources.get(k);
    src.textContent = s ? s.label : k;
    const txt = document.createElement('span');
    txt.textContent = state.partialsBySource[k];
    row.append(src, txt);
    root.append(row);
  }
}

function renderTranscript() {
  const root = $('#transcript');
  root.innerHTML = '';
  const sorted = [...state.utterances].sort((a, b) => (a.start_ms || 0) - (b.start_ms || 0));
  for (const u of sorted) {
    const row = document.createElement('div');
    row.className = 'utterance';
    const spk = document.createElement('span');
    spk.className = 'spk';
    spk.textContent = u.speaker;
    const txt = document.createElement('span');
    txt.className = 'txt';
    txt.textContent = u.text;
    const time = document.createElement('span');
    time.className = 'time';
    time.textContent = fmtMs(u.start_ms);
    row.append(spk, txt, time);
    root.append(row);
  }
  root.scrollTop = root.scrollHeight;
}

function fmtMs(ms) {
  if (!ms) return '0:00';
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// ============== Merge / relabel ==============
function showMergePanel() {
  const speakers = [...new Set(state.utterances.map(u => u.speaker))].sort();
  if (speakers.length === 0) return;
  const root = $('#merge-list');
  root.innerHTML = '';
  for (const sp of speakers) {
    const row = document.createElement('div');
    row.className = 'merge-row';
    const lbl = document.createElement('span');
    lbl.className = 'label';
    lbl.textContent = sp;
    const arrow = document.createElement('span');
    arrow.className = 'arrow';
    arrow.textContent = '→';
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.placeholder = `保持 "${sp}",或输入合并目标(可重复)`;
    inp.dataset.from = sp;
    row.append(lbl, arrow, inp);
    root.append(row);
  }
  $('#panel-merge').classList.remove('hidden');
  $('#panel-merge').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

$('#btn-skip-merge').addEventListener('click', () => runSummary({}));
$('#btn-apply-merge').addEventListener('click', () => {
  const mapping = {};
  for (const inp of $$('#merge-list input')) {
    const v = inp.value.trim();
    if (v) mapping[inp.dataset.from] = v;
  }
  runSummary(mapping);
});

async function runSummary(mapping) {
  setStatus('summarizing…', 'busy');
  if (Object.keys(mapping).length) {
    state.utterances = state.utterances.map(u => ({ ...u, speaker: mapping[u.speaker] || u.speaker }));
    renderTranscript();
  }
  try {
    const r = await fetch('/api/summarize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ utterances: state.utterances, mapping }),
    });
    if (!r.ok) {
      const t = await r.text();
      setStatus('summary failed: ' + t, '');
      return;
    }
    const j = await r.json();
    renderSummary(j);
    setStatus('done');
  } catch (e) {
    setStatus('summary error: ' + e, '');
  }
}

function renderSummary(s) {
  const root = $('#summary');
  root.innerHTML = '';
  for (const t of (s.topics || [])) {
    const card = document.createElement('div');
    card.className = 'topic';
    const title = document.createElement('div');
    title.className = 'title';
    title.textContent = t.title || '(未命名)';
    const summ = document.createElement('div');
    summ.className = 'summary';
    summ.textContent = t.summary || '';
    card.append(title, summ);
    for (const v of (t.viewpoints || [])) {
      const vp = document.createElement('div');
      vp.className = 'vp';
      const sp = document.createElement('span');
      sp.className = 'speaker';
      sp.textContent = v.speaker + ': ';
      const view = document.createElement('span');
      view.textContent = v.view || '';
      vp.append(sp, view);
      card.append(vp);
    }
    root.append(card);
  }
  if (s.todos && s.todos.length) {
    const td = document.createElement('div');
    td.className = 'todos';
    const h = document.createElement('div');
    h.className = 'title';
    h.textContent = '待办事项';
    td.append(h);
    for (const t of s.todos) {
      const r = document.createElement('div');
      r.className = 'todo';
      const o = document.createElement('span');
      o.className = 'owner';
      o.textContent = (t.owner || '未指明') + ': ';
      const task = document.createElement('span');
      task.textContent = t.task || '';
      r.append(o, task);
      td.append(r);
    }
    root.append(td);
  }
  $('#summary-json').textContent = JSON.stringify(s, null, 2);
  $('#panel-summary').classList.remove('hidden');
  $('#panel-summary').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ============== Init ==============
loadDevices();
})();
