'use strict';

const $ = (sel) => document.querySelector(sel);
const state = {
  book: null,          // {book_id, title, author, chapters: [...]}
  voices: { clone: [], predefined: [] },
  mode: 'clone',
  voiceId: null,
  pollTimer: null,
};

const PARAMS = ['exaggeration', 'cfg_weight', 'temperature', 'speed_factor'];

/* ---------- helpers ---------- */

function fmtDuration(sec) {
  if (sec == null || !isFinite(sec)) return '--';
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

function fmtBytes(n) {
  if (!n) return '--';
  return n > 1e9 ? `${(n / 1e9).toFixed(2)} GB` : `${(n / 1e6).toFixed(1)} MB`;
}

function showError(el, message) {
  el.textContent = message;
  el.hidden = !message;
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body.detail) detail = typeof body.detail === 'string'
        ? body.detail : JSON.stringify(body.detail);
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------- engine status ---------- */

async function refreshEngine() {
  const pill = $('#engine');
  try {
    const s = await api('/api/status');
    if (!s.reachable) {
      pill.textContent = `engine down — ${s.detail}`;
      pill.className = 'pill pill-err';
      return false;
    }
    if (s.loaded) {
      let note = '';
      if (s.in_use_by && s.in_use_by.length) note = ' (in use)';
      else if (s.managed) note = ` (unloads after ${s.idle_unload_sec}s idle)`;
      pill.textContent = `model loaded — ${s.detail}${note}`;
      pill.className = 'pill pill-ok';
    } else {
      // Idle with the GPU released is the intended resting state, not a fault
      // — unless this app is not the one managing the model.
      pill.textContent = s.managed
        ? 'engine idle — model unloaded, loads when you run'
        : 'engine idle — no model loaded on the server';
      pill.className = `pill ${s.managed ? 'pill-wait' : 'pill-err'}`;
    }
    return true;
  } catch (e) {
    pill.textContent = `engine unreachable — ${e.message}`;
    pill.className = 'pill pill-err';
    return false;
  }
}

/* ---------- book upload ---------- */

async function uploadBook(file) {
  const drop = $('#drop');
  showError($('#book-error'), '');
  if (!file.name.toLowerCase().endsWith('.epub')) {
    showError($('#book-error'), 'That is not an .epub file.');
    return;
  }
  drop.classList.add('is-busy');
  drop.querySelector('.drop-inner').innerHTML =
    `<div class="drop-icon">⏳</div><p><strong>Reading ${file.name}</strong></p>`;

  try {
    const body = new FormData();
    body.append('file', file);
    state.book = await api('/api/books', { method: 'POST', body });
    renderBook();
  } catch (e) {
    showError($('#book-error'), `Could not read that book: ${e.message}`);
    resetDrop();
  } finally {
    drop.classList.remove('is-busy');
  }
}

function resetDrop() {
  $('#drop').querySelector('.drop-inner').innerHTML =
    `<div class="drop-icon">📖</div>
     <p><strong>Drop an .epub here</strong></p>
     <p class="muted">or click to browse</p>`;
  $('#drop').hidden = false;
  $('#book').hidden = true;
  state.book = null;
  updateRunButton();
}

function renderBook() {
  const book = state.book;
  $('#drop').hidden = true;
  $('#book').hidden = false;
  $('#book-title').textContent = book.title;
  $('#book-author').textContent = book.author;

  const list = $('#chapters');
  list.innerHTML = '';
  for (const ch of book.chapters) {
    const li = document.createElement('li');
    const label = document.createElement('label');

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = ch.include;
    cb.dataset.index = ch.index;
    cb.addEventListener('change', () => {
      ch.include = cb.checked;
      updateChapterSummary();
    });

    const title = document.createElement('span');
    title.className = 'ch-title';
    title.textContent = ch.title;
    title.title = ch.preview || ch.title;

    const chars = document.createElement('span');
    chars.className = 'ch-chars';
    chars.textContent = ch.chars.toLocaleString();

    label.append(cb, title, chars);
    li.append(label);
    list.append(li);
  }
  updateChapterSummary();
}

function selectedChapters() {
  return state.book ? state.book.chapters.filter((c) => c.include) : [];
}

function updateChapterSummary() {
  const picked = selectedChapters();
  const chars = picked.reduce((sum, c) => sum + c.chars, 0);
  $('#chapter-summary').textContent =
    `${picked.length} of ${state.book.chapters.length} chapters · ${chars.toLocaleString()} characters`;

  // Same arithmetic the server uses, kept live so the cost is visible up front.
  const audioHours = chars / 14.5 / 3600;
  $('#estimate').textContent = chars
    ? `≈ ${audioHours.toFixed(2)} h of audio, about ${(audioHours / 3.8).toFixed(2)} h to generate`
    : '';
  updateRunButton();
}

/* ---------- voices ---------- */

async function loadVoices() {
  try {
    state.voices = await api('/api/voices');
  } catch (e) {
    $('#voice-list').innerHTML = `<p class="muted">Could not load voices: ${e.message}</p>`;
    return;
  }
  renderVoices();
}

function renderVoices() {
  const list = state.voices[state.mode] || [];
  const box = $('#voice-list');
  box.innerHTML = '';

  if (!list.length) {
    box.innerHTML = state.mode === 'clone'
      ? `<p class="muted">No cloning references yet — upload a .wav below.</p>`
      : `<p class="muted">No built-in voices found.</p>`;
    return;
  }

  for (const voice of list) {
    const card = document.createElement('div');
    card.className = 'voice' + (state.voiceId === voice.id ? ' is-selected' : '');
    card.addEventListener('click', () => {
      state.voiceId = voice.id;
      renderVoices();
      updateRunButton();
    });

    const name = document.createElement('span');
    name.className = 'voice-name';
    name.textContent = voice.label;
    name.title = voice.id;

    const play = document.createElement('button');
    play.className = 'voice-play';
    play.type = 'button';
    play.textContent = '▶';
    play.title = 'Preview this voice';
    play.addEventListener('click', (ev) => {
      ev.stopPropagation();
      previewVoice(voice.id, play);
    });

    card.append(name, play);
    box.append(card);
  }
}

async function previewVoice(voiceId, button) {
  const original = button.textContent;
  button.textContent = '…';
  button.disabled = true;
  try {
    const body = new FormData();
    body.append('mode', state.mode);
    body.append('voice_id', voiceId);
    const res = await fetch('/api/voices/sample', { method: 'POST', body });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const url = URL.createObjectURL(await res.blob());
    const player = $('#player');
    player.src = url;
    player.play();
  } catch (e) {
    alert(`Preview failed: ${e.message}`);
  } finally {
    button.textContent = original;
    button.disabled = false;
  }
}

async function uploadVoice(file) {
  const status = $('#voice-upload-status');
  status.textContent = `Uploading ${file.name}…`;
  try {
    const body = new FormData();
    body.append('file', file);
    const added = await api('/api/voices/upload', { method: 'POST', body });
    await loadVoices();
    state.mode = 'clone';
    state.voiceId = added.id;
    document.querySelectorAll('.tab').forEach((t) =>
      t.classList.toggle('is-active', t.dataset.mode === 'clone'));
    renderVoices();
    updateRunButton();
    status.textContent = `Added ${added.label}.`;
  } catch (e) {
    status.textContent = `Upload failed: ${e.message}`;
  }
}

/* ---------- run ---------- */

function updateRunButton() {
  $('#run').disabled = !(state.book && state.voiceId && selectedChapters().length);
}

async function run() {
  showError($('#run-error'), '');
  const button = $('#run');
  button.disabled = true;
  button.textContent = 'Starting…';

  try {
    const body = new FormData();
    body.append('book_id', state.book.book_id);
    body.append('voice_mode', state.mode);
    body.append('voice_id', state.voiceId);
    body.append('chapters', JSON.stringify(selectedChapters().map((c) => c.index)));
    for (const p of PARAMS) body.append(p, $(`#${p}`).value);
    body.append('seed', $('#seed').value);

    await api('/api/jobs', { method: 'POST', body });
    await refreshJobs();
  } catch (e) {
    showError($('#run-error'), `Could not start: ${e.message}`);
  } finally {
    button.textContent = 'Run';
    updateRunButton();
  }
}

/* ---------- jobs ---------- */

async function refreshJobs() {
  let jobs;
  try {
    jobs = await api('/api/jobs');
  } catch {
    return;   // transient; the next poll will pick it up
  }

  const box = $('#jobs');
  if (!jobs.length) {
    box.innerHTML = '<p class="muted">No runs yet.</p>';
    return;
  }

  box.innerHTML = '';
  for (const job of jobs) box.append(renderJob(job));
}

function renderJob(job) {
  const el = document.createElement('div');
  el.className = 'job';

  const done = job.progress.chunks_done;
  const total = job.progress.chunks_total || 1;
  const pct = Math.round((done / total) * 100);
  const active = ['queued', 'running', 'assembling'].includes(job.status);

  const head = document.createElement('div');
  head.className = 'job-head';
  head.innerHTML =
    `<span class="job-title"></span><span class="badge ${job.status}">${job.status}</span>`;
  head.querySelector('.job-title').textContent = job.title;

  const actions = document.createElement('div');
  actions.className = 'job-actions';

  if (job.status === 'completed') {
    const dl = document.createElement('a');
    dl.className = 'btn btn-sm btn-primary';
    dl.href = `/api/jobs/${job.id}/download`;
    dl.textContent = 'Download .m4b';
    actions.append(dl);
  }
  if (active) {
    actions.append(button('Cancel', 'btn-ghost', async () => {
      await api(`/api/jobs/${job.id}/cancel`, { method: 'POST' });
      refreshJobs();
    }));
  }
  if (['failed', 'cancelled', 'interrupted'].includes(job.status)) {
    actions.append(button('Resume', 'btn-ghost', async () => {
      await api(`/api/jobs/${job.id}/resume`, { method: 'POST' });
      refreshJobs();
    }));
  }
  if (!active) {
    actions.append(button('Delete', 'btn-ghost', async () => {
      if (!confirm(`Delete "${job.title}" and its audio?`)) return;
      await api(`/api/jobs/${job.id}`, { method: 'DELETE' });
      refreshJobs();
    }));
  }
  head.append(actions);
  el.append(head);

  const bar = document.createElement('div');
  bar.className = 'bar';
  bar.innerHTML = `<span style="width:${pct}%"></span>`;
  el.append(bar);

  const meta = document.createElement('div');
  meta.className = 'job-meta';
  const bits = [
    `${done.toLocaleString()} / ${total.toLocaleString()} chunks (${pct}%)`,
    `voice: ${job.voice.id}`,
  ];
  if (job.progress.current_chapter_title && active) {
    bits.push(`now: ${job.progress.current_chapter_title}`);
  }
  if (job.status === 'running' && job.timing.eta_sec != null) {
    bits.push(`ETA ${fmtDuration(job.timing.eta_sec)}`);
  }
  if (job.timing.elapsed_sec) bits.push(`elapsed ${fmtDuration(job.timing.elapsed_sec)}`);
  if (job.output) {
    bits.push(`${fmtDuration(job.output.duration_sec)} audio`);
    bits.push(fmtBytes(job.output.size_bytes));
  }
  meta.textContent = bits.join('  ·  ');
  el.append(meta);

  if (job.error) {
    const err = document.createElement('div');
    err.className = 'error';
    err.textContent = job.error;
    el.append(err);
  }
  return el;
}

function button(text, cls, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = `btn btn-sm ${cls}`;
  b.textContent = text;
  b.addEventListener('click', onClick);
  return b;
}

/* ---------- wiring ---------- */

function init() {
  const drop = $('#drop');
  const epubInput = $('#epub-input');

  drop.addEventListener('click', () => epubInput.click());
  drop.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); epubInput.click(); }
  });
  epubInput.addEventListener('change', () => {
    if (epubInput.files[0]) uploadBook(epubInput.files[0]);
  });

  ['dragenter', 'dragover'].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('is-over'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('is-over'); }));
  drop.addEventListener('drop', (e) => {
    const file = e.dataTransfer.files[0];
    if (file) uploadBook(file);
  });
  // Dropping anywhere else must not make the browser navigate to the file.
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  $('#book-reset').addEventListener('click', resetDrop);
  $('#select-all').addEventListener('click', () => setAllChapters(true));
  $('#select-none').addEventListener('click', () => setAllChapters(false));

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.remove('is-active'));
      tab.classList.add('is-active');
      state.mode = tab.dataset.mode;
      state.voiceId = null;
      renderVoices();
      updateRunButton();
    });
  });

  $('#voice-input').addEventListener('change', (e) => {
    if (e.target.files[0]) uploadVoice(e.target.files[0]);
  });

  for (const p of PARAMS) {
    const input = $(`#${p}`);
    const out = $(`#${p}-out`);
    const sync = () => { out.textContent = Number(input.value).toFixed(2); };
    input.addEventListener('input', sync);
    sync();
  }

  $('#run').addEventListener('click', run);

  refreshEngine();
  loadVoices();
  refreshJobs();
  setInterval(refreshEngine, 20000);
  state.pollTimer = setInterval(refreshJobs, 2500);
}

function setAllChapters(value) {
  if (!state.book) return;
  state.book.chapters.forEach((c) => { c.include = value; });
  document.querySelectorAll('#chapters input[type=checkbox]')
    .forEach((cb) => { cb.checked = value; });
  updateChapterSummary();
}

document.addEventListener('DOMContentLoaded', init);
