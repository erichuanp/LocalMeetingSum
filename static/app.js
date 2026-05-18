// LocalMeetingSum — frontend
// All audio capture happens in the browser via getUserMedia / getDisplayMedia.
// PCM is streamed to the server as WebSocket binary frames; the server runs
// STT and pushes back transcript events as JSON text frames.
(() => {
'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const TARGET_SR = 16000;
const EMIT_MS = 100;          // per-frame chunk size sent over WS
const FRAME_VERSION = 1;

const state = {
  ws: null,
  utterances: [],
  partialsBySource: {},
  sources: new Map(),         // id -> { kind, label, stream, ctx, node, level, lastLevelTs }
  liveActive: false,
  nextId: 1,
  permissionAsked: false,
};

// ============== Tabs ==============
$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $$('.tab-pane').forEach(p => p.classList.add('hidden'));
  $(`[data-pane="${t.dataset.tab}"]`).classList.remove('hidden');
}));

// ============== Status ==============
function setStatus(text, cls = '') {
  const s = $('#status');
  s.textContent = text;
  s.className = cls;
}

// ============== WebSocket (control + binary PCM) ==============
function connect() {
  return new Promise((resolve, reject) => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) return resolve();
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { state.ws = ws; resolve(); };
    ws.onerror = (e) => reject(e);
    ws.onclose = () => {
      state.ws = null;
      setStatus('disconnected', '');
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try { handleEvent(JSON.parse(ev.data)); } catch {}
      }
    };
  });
}

function sendCtrl(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(obj));
  }
}

// Binary frame: [u8 version=1][u8 id_len][id bytes][float32 LE PCM ...]
function sendPCM(sourceId, float32) {
  const ws = state.ws;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const idBytes = new TextEncoder().encode(sourceId);
  if (idBytes.length > 255) return;
  const buf = new ArrayBuffer(2 + idBytes.length + float32.byteLength);
  const dv = new DataView(buf);
  dv.setUint8(0, FRAME_VERSION);
  dv.setUint8(1, idBytes.length);
  new Uint8Array(buf, 2, idBytes.length).set(idBytes);
  new Uint8Array(buf, 2 + idBytes.length).set(new Uint8Array(float32.buffer));
  ws.send(buf);
}

// ============== Event handler ==============
function handleEvent(msg) {
  switch (msg.type) {
    case 'source_opened':
    case 'source_closed':
      break;
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
      $('#file-progress').value = msg.percent || 0;
      setStatus(`${msg.stage} ${msg.percent || 0}%`, 'busy');
      break;
    case 'done':
      setStatus('done');
      $('#file-progress').style.display = 'none';
      showMergePanel();
      break;
    case 'error':
      console.error('server error', msg);
      setStatus('err: ' + msg.message, '');
      break;
  }
}

// ============== Add-source UI ==============
async function ensureMicPermission() {
  // Triggering getUserMedia once unlocks device labels in enumerateDevices.
  if (state.permissionAsked) return;
  try {
    const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
    probe.getTracks().forEach(t => t.stop());
    state.permissionAsked = true;
  } catch (e) {
    // User denied — labels will be empty but we let them try anyway.
  }
}

async function refreshMicDropdown() {
  const sel = $('#new-device');
  sel.innerHTML = '';
  let devices = [];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch (e) {
    sel.append(opt('(无法枚举设备)', '', true));
    return;
  }
  const mics = devices.filter(d => d.kind === 'audioinput');
  if (mics.length === 0) {
    sel.append(opt('(没有麦克风设备)', '', true));
    $('#btn-confirm-add').disabled = true;
    return;
  }
  if (!mics[0].label) {
    // Permission not granted yet — show "授权后显示设备名" placeholder
    sel.append(opt('点击"添加"后浏览器会请求麦克风权限', '', true));
  }
  for (const d of mics) {
    sel.append(opt(d.label || `麦克风 ${d.deviceId.slice(0,6)}`, d.deviceId));
  }
  $('#btn-confirm-add').disabled = false;
}

function opt(text, value, disabled = false) {
  const o = document.createElement('option');
  o.textContent = text;
  o.value = value;
  if (disabled) o.disabled = true;
  return o;
}

function showScreenAvailability() {
  // Only Chromium desktop browsers support getDisplayMedia with system audio.
  const canScreen = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  $('#kind-screen-label').style.opacity = canScreen ? '' : '0.4';
  $('#kind-screen-label').title = canScreen ? '' : '此浏览器不支持屏幕音频(常见于手机)';
}

$('#btn-show-add').addEventListener('click', async () => {
  showScreenAvailability();
  await ensureMicPermission();
  await refreshMicDropdown();
  $('#add-source-form').classList.remove('hidden');
  $('#btn-show-add').classList.add('hidden');
});
$('#btn-cancel-add').addEventListener('click', () => {
  $('#add-source-form').classList.add('hidden');
  $('#btn-show-add').classList.remove('hidden');
});
$$('input[name="new-kind"]').forEach(el => el.addEventListener('change', () => {
  const kind = document.querySelector('input[name="new-kind"]:checked').value;
  $('#new-device').style.display = (kind === 'mic') ? '' : 'none';
}));

// ============== Add / remove source ==============
async function addSource() {
  const kind = document.querySelector('input[name="new-kind"]:checked').value;
  let stream;
  let displayName;
  try {
    if (kind === 'mic') {
      const deviceId = $('#new-device').value;
      if (!deviceId) { alert('请选择一个麦克风'); return; }
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: { exact: deviceId },
          echoCancellation: true,
          noiseSuppression: false,
          autoGainControl: false,
        }
      });
      const opt = $('#new-device').options[$('#new-device').selectedIndex];
      displayName = (opt && opt.textContent) || '麦克风';
    } else {
      // Screen audio (Chrome desktop only)
      const full = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: true,
      });
      const audio = full.getAudioTracks();
      if (audio.length === 0) {
        full.getTracks().forEach(t => t.stop());
        alert('未捕获到系统音频。请在分享对话框里勾选"分享系统声音"。');
        return;
      }
      // Discard video track to save bandwidth/cpu.
      full.getVideoTracks().forEach(t => t.stop());
      stream = new MediaStream(audio);
      displayName = '屏幕音频';
    }
  } catch (e) {
    if (e.name === 'NotAllowedError') alert('权限被拒绝');
    else alert('采集失败: ' + e.message);
    return;
  }

  const id = `s${state.nextId++}`;
  const label = autoLabel(kind, displayName);

  // Build audio graph: source → worklet → (no output, we don't want to hear ourselves)
  const ctx = new AudioContext({ sampleRate: TARGET_SR, latencyHint: 'interactive' });
  try {
    await ctx.audioWorklet.addModule('/static/pcm-worklet.js');
  } catch (e) {
    stream.getTracks().forEach(t => t.stop());
    ctx.close();
    alert('AudioWorklet 不可用,需要现代浏览器');
    return;
  }
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, 'pcm-worklet', {
    processorOptions: { emitMs: EMIT_MS },
    numberOfInputs: 1,
    numberOfOutputs: 1,
    channelCount: 1,
    channelCountMode: 'explicit',
    channelInterpretation: 'speakers',
  });
  src.connect(node);
  // node.connect(ctx.destination);  // intentionally NOT connected — no monitoring

  const entry = { id, kind, label, stream, ctx, src, node, level: 0, lastLevelTs: 0, displayName };
  state.sources.set(id, entry);

  node.port.onmessage = (e) => {
    const pcm = e.data.pcm;
    const sr = e.data.sr || ctx.sampleRate;
    const out = sr === TARGET_SR ? pcm : resampleTo16k(pcm, sr);
    // Update level meter (peak amplitude).
    let peak = 0;
    for (let i = 0; i < out.length; i++) {
      const a = out[i] < 0 ? -out[i] : out[i];
      if (a > peak) peak = a;
    }
    entry.level = peak;
    entry.lastLevelTs = performance.now();
    // Send to server (server drops if STT not yet enabled).
    sendPCM(id, out);
  };

  // If the user revokes permission or unplugs the device, MediaStreamTrack 'ended' fires.
  stream.getAudioTracks()[0].addEventListener('ended', () => removeSource(id));

  await connect();
  sendCtrl({ cmd: 'open_source', id, label, sample_rate: TARGET_SR });

  $('#add-source-form').classList.add('hidden');
  $('#btn-show-add').classList.remove('hidden');
  $('#btn-start-live').disabled = state.liveActive;
  renderSources();
}

function autoLabel(kind, name) {
  let n = (name || '').split('(')[0].trim();
  if (n.length > 14) n = n.slice(0, 14);
  if (!n) n = kind === 'screen' ? 'Screen' : 'Mic';
  return n + (kind === 'screen' ? ':sys' : '');
}

function removeSource(id) {
  const s = state.sources.get(id);
  if (!s) return;
  try { s.stream.getTracks().forEach(t => t.stop()); } catch {}
  try { s.src.disconnect(); } catch {}
  try { s.node.disconnect(); } catch {}
  try { s.ctx.close(); } catch {}
  state.sources.delete(id);
  sendCtrl({ cmd: 'close_source', id });
  renderSources();
  if (state.sources.size === 0) $('#btn-start-live').disabled = true;
}

// ============== Simple linear resampler (for AudioContext sr != 16k fallback) ==============
function resampleTo16k(pcm, srcSr) {
  if (srcSr === TARGET_SR) return pcm;
  const ratio = TARGET_SR / srcSr;
  const n = Math.round(pcm.length * ratio);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const t = i / ratio;
    const i0 = Math.floor(t);
    const i1 = Math.min(i0 + 1, pcm.length - 1);
    const f = t - i0;
    out[i] = pcm[i0] * (1 - f) + pcm[i1] * f;
  }
  return out;
}

// ============== Source rendering + level animation ==============
function renderSources() {
  const root = $('#sources-list');
  root.innerHTML = '';
  for (const [id, s] of state.sources) {
    const row = document.createElement('div');
    row.className = 'source-row';
    row.dataset.id = id;

    const badge = document.createElement('span');
    badge.className = 'kind-badge ' + (s.kind === 'mic' ? 'input' : 'loopback');
    badge.textContent = s.kind === 'mic' ? '麦克风' : '屏幕音频';
    const name = document.createElement('span');
    name.className = 'src-name';
    name.title = s.displayName;
    name.textContent = s.displayName;

    const labelInput = document.createElement('input');
    labelInput.type = 'text';
    labelInput.className = 'src-label';
    labelInput.value = s.label;
    labelInput.placeholder = '标签';
    labelInput.addEventListener('change', () => {
      s.label = labelInput.value.trim() || s.label;
      sendCtrl({ cmd: 'relabel', id, label: s.label });
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
    rm.addEventListener('click', () => removeSource(id));

    row.append(badge, name, labelInput, meter, rm);
    root.append(row);
  }
}

function tickMeters() {
  const now = performance.now();
  for (const [id, s] of state.sources) {
    const row = document.querySelector(`.source-row[data-id="${CSS.escape(id)}"]`);
    if (!row) continue;
    const fill = row.querySelector('.meter-fill');
    const age = now - (s.lastLevelTs || 0);
    let level = s.level || 0;
    if (age > 150) level = level * Math.max(0, 1 - (age - 150) / 800);
    const db = 20 * Math.log10(Math.max(level, 1e-6));
    const pct = Math.max(0, Math.min(100, (db + 60) * 100 / 60));
    fill.style.width = pct + '%';
    fill.classList.toggle('peak', db > -6);
    fill.classList.toggle('warn', db > -20 && db <= -6);
  }
  requestAnimationFrame(tickMeters);
}
requestAnimationFrame(tickMeters);

// ============== Confirm-add button wiring ==============
$('#btn-confirm-add').addEventListener('click', addSource);

// ============== Live start / stop ==============
$('#btn-start-live').addEventListener('click', async () => {
  if (state.sources.size === 0) { alert('请先添加至少一个音源'); return; }
  state.utterances = [];
  state.partialsBySource = {};
  renderTranscript();
  renderPartials();
  $('#panel-merge').classList.add('hidden');
  $('#panel-summary').classList.add('hidden');
  await connect();
  sendCtrl({ cmd: 'start_live' });
});
$('#btn-stop-live').addEventListener('click', () => {
  sendCtrl({ cmd: 'stop_live' });
  $('#btn-start-live').disabled = state.sources.size === 0;
  $('#btn-stop-live').disabled = true;
});

// ============== File upload ==============
$('#file-input').addEventListener('change', () => {
  $('#btn-process-file').disabled = !$('#file-input').files[0];
});
$('#btn-process-file').addEventListener('click', async () => {
  const f = $('#file-input').files[0];
  if (!f) return;
  state.utterances = [];
  state.partialsBySource = {};
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
  sendCtrl({ cmd: 'process_file', session_id: sid, label });
});

// ============== Rendering: partials, transcript ==============
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

// ============== Merge / summary ==============
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
showScreenAvailability();
})();
