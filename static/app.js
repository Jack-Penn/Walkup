'use strict';

const $ = (id) => document.getElementById(id);
const icon = (name, cls = '') => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const csrf = document.querySelector('meta[name="portal-csrf"]').content;
const audio = $('browser-audio');
let state = null;
let output = 'browser';
let initialized = false;
let browserTrack = null;
let browserClip = null;
let playTicket = 0;
let playPending = false;
let clipSaveTimer = null;
let volumeSaveTimer = null;
let lastServerPoll = performance.now();
let lastPlaybackError = null;
let lastLibrarySignature = '';
let lastEventsSignature = '';
let dragging = null;
const waveforms = new Map();

function formatTime(seconds) {
  const number = Math.max(0, Number(seconds) || 0);
  return `${Math.floor(number / 60)}:${String(Math.floor(number % 60)).padStart(2, '0')}`;
}
function formatBytes(bytes) {
  return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
function relativeTime(value) {
  if (!value) return 'Not yet';
  const age = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
  if (age < 45) return 'Just now';
  if (age < 3600) return `${Math.floor(age / 60)}m ago`;
  if (age < 86400) return `${Math.floor(age / 3600)}h ago`;
  return `${Math.floor(age / 86400)}d ago`;
}
function selectedTrack() { return state?.tracks.find((track) => track.id === state.selected_id) || null; }
function currentClip() {
  const track = selectedTrack();
  return track ? (state.clips[track.id] || {start: 0, duration: Math.min(12, track.duration)}) : {start: 0, duration: 0};
}
function getOutputPreference() { try { return localStorage.getItem('threshold-output'); } catch { return null; } }
function toast(message, error = false) {
  const element = document.createElement('div');
  element.className = `toast${error ? ' error' : ''}`;
  element.innerHTML = `${icon(error ? 'info' : 'check')}<span>${escapeHtml(message)}</span><button class="toast-close" aria-label="Dismiss notification">${icon('close')}</button>`;
  element.querySelector('button').onclick = () => element.remove();
  $('toasts').appendChild(element);
  setTimeout(() => element.remove(), error ? 9000 : 4200);
}
async function api(path, data, method = 'POST') {
  const response = await fetch(path, {
    method,
    headers: {'Content-Type': 'application/json', 'X-Portal-CSRF': csrf},
    body: data === undefined ? undefined : JSON.stringify(data),
    credentials: 'same-origin',
  });
  let result;
  try { result = await response.json(); }
  catch { throw new Error('The server did not return a readable response. Please try again.'); }
  if (result.code === 'auth_required') { location.href = '/login'; throw new Error('Please sign in again.'); }
  if (!response.ok || result.ok === false) {
    if (result.state) applyState(result.state);
    throw new Error(result.error || 'Something went wrong. Please try again.');
  }
  return result;
}
async function refreshState(quiet = true) {
  try {
    const result = await api('/api/state', undefined, 'GET');
    applyState(result.state);
    $('server-connection').classList.remove('offline');
    $('server-connection').innerHTML = '<span class="status-dot"></span>Server online';
  } catch (error) {
    $('server-connection').classList.add('offline');
    $('server-connection').innerHTML = '<span class="status-dot"></span>Reconnecting…';
    if (!quiet) toast(error.message, true);
  }
}

function applyState(next) {
  const preserveClip = state && (clipSaveTimer || dragging || ['clip-start', 'clip-duration'].includes(document.activeElement?.id));
  const oldClip = preserveClip ? {...currentClip()} : null;
  const oldSelected = state?.selected_id;
  state = next;
  if (oldClip && oldSelected === state.selected_id) state.clips[oldSelected] = oldClip;
  lastServerPoll = performance.now();
  if (!initialized) {
    const preference = getOutputPreference();
    output = preference || (state.system.server_available && state.system.hardware_detected ? 'server' : 'browser');
    if (output === 'server' && !state.system.server_available) output = 'browser';
    initialized = true;
  }
  $('nav-track-count').textContent = state.tracks.length;
  $('library-count').textContent = state.tracks.length;
  $('library-summary-title').textContent = `${state.tracks.length} ${state.tracks.length === 1 ? 'sound' : 'sounds'} in your library`;
  $('library-summary-sub').textContent = state.tracks.length ? `${formatBytes(state.tracks.reduce((sum, track) => sum + track.bytes, 0))} · stored on this device` : 'Add your very first welcome sound';
  $('library-storage').textContent = formatBytes(state.tracks.reduce((sum, track) => sum + track.bytes, 0));
  $('footer-device').textContent = `Threshold ${state.system.version} · ${state.system.hostname}`;
  const serverOption = $('output-select').querySelector('option[value="server"]');
  serverOption.disabled = !state.system.server_available;
  serverOption.textContent = state.system.server_available ? 'Server speaker' : 'Server speaker · setup needed';
  $('output-select').value = output;
  if (document.activeElement !== $('volume')) $('volume').value = state.volume;
  $('volume-value').textContent = `${state.volume}%`;
  audio.volume = state.volume / 100;
  renderOutput();
  renderLibrary();
  renderPlayer();
  renderConnection();
  renderEvents();
  renderSettings();
  if (state.playback.error && state.playback.error !== lastPlaybackError) toast(state.playback.error, true);
  lastPlaybackError = state.playback.error;
}

function renderOutput() {
  $('output-summary-title').textContent = output === 'server' ? 'Server speaker' : 'This browser';
  $('output-summary-sub').textContent = output === 'server' ? 'The server’s default audio device' : 'Local preview · no speaker required';
  $('output-icon').innerHTML = `<use href="#i-${output === 'server' ? 'speaker' : 'laptop'}"></use>`;
  $('output-note').textContent = output === 'server' ? 'Plays from the Pi, even when this page is closed.' : 'A quick preview, right where you are. Choose Server speaker on your Pi.';
}

function renderLibrary(force = false) {
  if (!state) return;
  const query = $('track-search').value.toLowerCase().trim();
  const sort = $('track-sort').value;
  const tracks = state.tracks.filter((track) => track.title.toLowerCase().includes(query));
  if (sort === 'name') tracks.sort((a, b) => a.title.localeCompare(b.title));
  if (sort === 'duration') tracks.sort((a, b) => a.duration - b.duration);
  const signature = JSON.stringify([tracks.map((track) => [track.id, track.title]), state.selected_id, query, sort]);
  if (!force && signature === lastLibrarySignature) return;
  lastLibrarySignature = signature;
  if (!tracks.length) {
    $('track-list').innerHTML = `<div class="empty-state">${icon(query ? 'search' : 'music')}<strong>${query ? 'No sounds by that name.' : 'Your next entrance starts here.'}</strong><p>${query ? 'Try another name, or clear the search.' : 'Drop in an audio file above. Your new favorite hello is one upload away.'}</p></div>`;
    return;
  }
  $('track-list').innerHTML = tracks.map((track) => {
    const selected = track.id === state.selected_id;
    const palette = parseInt(track.id.slice(0, 2), 16) % 4;
    return `<div class="track-row${selected ? ' selected' : ''}" data-track="${track.id}" role="button" tabindex="0" aria-pressed="${selected}" aria-label="Select ${escapeHtml(track.title)}">
      <div class="track-main"><div class="track-thumbnail palette-${track.demo ? 0 : palette}" aria-hidden="true"></div><div class="track-text"><div class="track-title"><span>${escapeHtml(track.title)}</span>${track.demo ? '<span class="inline-demo">DEMO</span>' : ''}</div><div class="track-description">${track.format} audio · ${track.demo ? 'Included welcome chime' : formatBytes(track.bytes)}</div></div></div>
      <span class="track-length">${formatTime(track.duration)}</span><div class="track-actions">${selected ? `<span class="track-selected" aria-label="Selected">${icon('check')}</span>` : ''}<button class="track-delete" data-delete="${track.id}" aria-label="Remove ${escapeHtml(track.title)}" title="Remove audio">${icon('trash')}</button></div></div>`;
  }).join('');
}

function renderPlayer() {
  const track = selectedTrack();
  const clip = currentClip();
  $('selected-title').textContent = track ? track.title : 'Choose your sound';
  $('selected-meta').textContent = track ? `${track.format} audio · ${formatTime(track.duration)} total${track.demo ? ' · original chime' : ''}` : 'Add an audio file to make yourself at home';
  $('selected-demo').hidden = !track?.demo;
  $('source-duration').textContent = formatTime(track?.duration || 0);
  const index = state.tracks.findIndex((item) => item.id === track?.id) + 1;
  $('art-sequence').textContent = `${String(index).padStart(2, '0')} / ${String(state.tracks.length).padStart(2, '0')}`;
  for (const id of ['clip-start', 'clip-duration', 'start-handle', 'end-handle']) $(id).disabled = !track;
  if (document.activeElement !== $('clip-start')) $('clip-start').value = Number(clip.start.toFixed(1));
  if (document.activeElement !== $('clip-duration')) $('clip-duration').value = Number(clip.duration.toFixed(1));
  $('clip-start').max = track ? Math.max(0, track.duration - .05) : 0;
  $('clip-duration').max = track ? track.duration - clip.start : 0;
  renderWaveform();
  renderTransport();
  if (track && !track.peaks) ensureWaveform(track);
}

function renderWaveform() {
  const track = selectedTrack();
  const clip = currentClip();
  const peaks = track ? (track.peaks || waveforms.get(track.id)) : null;
  const total = track?.duration || 1;
  const left = clip.start / total * 100;
  const right = (clip.start + clip.duration) / total * 100;
  $('wave-selection').style.left = `${left}%`;
  $('wave-selection').style.width = `${right - left}%`;
  $('start-handle').style.left = `${left}%`;
  $('end-handle').style.left = `${right}%`;
  $('start-handle').hidden = !track;
  $('end-handle').hidden = !track;
  $('waveform-unavailable').hidden = !track || !!peaks;
  const data = peaks && peaks.length ? peaks : Array(100).fill(.04);
  const width = 640 / data.length;
  $('waveform').innerHTML = data.map((peak, index) => {
    const position = (index + .5) / data.length * total;
    const inClip = position >= clip.start && position <= clip.start + clip.duration;
    const height = Math.max(3, Math.min(1, peak) * 77);
    return `<rect x="${index * width + 1}" y="${50 - height / 2}" width="${Math.max(1, width - 2.1)}" height="${height}" rx="1.3" fill="${inClip ? '#c48b6a' : '#d7decc'}"/>`;
  }).join('');
}

async function ensureWaveform(track) {
  if (waveforms.has(track.id)) return;
  waveforms.set(track.id, null);
  if (track.bytes > 16 * 1024 * 1024 || !(window.AudioContext || window.webkitAudioContext)) return;
  let context;
  try {
    const response = await fetch(track.url);
    if (!response.ok) return;
    const data = await response.arrayBuffer();
    context = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await context.decodeAudioData(data);
    const samples = decoded.getChannelData(0);
    const count = 128;
    const step = Math.max(1, Math.floor(samples.length / count));
    const peaks = [];
    let maximum = 0;
    for (let bin = 0; bin < count; bin++) {
      let peak = 0;
      for (let index = bin * step; index < Math.min(samples.length, (bin + 1) * step); index += 4) peak = Math.max(peak, Math.abs(samples[index]));
      peaks.push(peak); maximum = Math.max(maximum, peak);
    }
    waveforms.set(track.id, peaks.map((peak) => Math.max(.035, peak / (maximum || 1))));
    if (selectedTrack()?.id === track.id) renderWaveform();
  } catch { /* Metadata and playback can still work without a waveform. */ }
  finally { if (context) context.close(); }
}

function isBrowserPlaying() { return !!browserTrack && !audio.paused; }
function isPlaying() { return isBrowserPlaying() || !!state?.playback.playing; }
function renderTransport() {
  const playing = isPlaying();
  $('play-button').disabled = !selectedTrack() || playPending;
  $('play-button-label').textContent = playPending ? 'One moment…' : playing ? 'Stop playback' : 'Play clip';
  $('play-button').querySelector('svg').innerHTML = `<use href="#i-${playing ? 'stop' : 'play'}"></use>`;
  $('stop-button').disabled = !playing;
  $('player').classList.toggle('is-playing', playing);
  $('playing-status').classList.toggle('is-playing', playing);
  $('playing-status').innerHTML = `<span class="ready-dot"></span>${playing ? (isBrowserPlaying() ? 'Playing in this browser' : 'Playing on the speaker') : 'Ready when you are'}<span id="playback-time"></span>`;
  if (!playing) $('wave-playhead').hidden = true;
}

function renderConnection() {
  const connection = state.life360;
  const configured = connection.configured;
  const connected = configured && connection.status === 'connected';
  const labels = {connected: 'Connected', reauth_required: 'Token rejected', blocked: 'Temporarily blocked', rate_limited: 'Rate limited', offline: 'Offline', error: 'Check connection', disconnected: 'Not connected'};
  const label = configured ? (labels[connection.status] || 'Check connection') : 'Not connected';
  $('connection-status').className = `connection-status${connected ? ' connected' : configured ? ' error' : ''}`;
  $('connection-status').innerHTML = `<span></span>${escapeHtml(label)}`;
  $('connection-summary-title').textContent = connected ? 'Life360 connected' : label;
  $('connection-summary-sub').textContent = configured ? connection.account_name || 'Manage your saved connection' : 'Connect your Life360 account';
  $('security-notice').hidden = state.system.auth_enabled;
  $('token-input').placeholder = configured ? 'Paste a replacement access token' : 'Paste your access token';
  $('token-label').textContent = configured ? 'REPLACE ACCESS TOKEN' : 'LIFE360 ACCESS TOKEN';
  $('save-token').querySelector('span').textContent = configured ? 'Replace & test' : 'Save & test';
  $('account-strip').hidden = !configured;
  $('forget-token').hidden = !configured;
  if (configured) {
    $('account-name').textContent = connection.account_name || 'Life360 account';
    $('account-avatar').textContent = (connection.account_name || 'L').split(' ').map((part) => part[0]).slice(0, 2).join('').toUpperCase();
    $('account-check').textContent = connection.message || `Last tested ${relativeTime(connection.checked_at).toLowerCase()}`;
  }
}

function renderEvents(force = false) {
  const events = state.events.slice(0, 4);
  const signature = JSON.stringify(events.map((event) => [event.id, relativeTime(event.at)]));
  if (!force && signature === lastEventsSignature) return;
  lastEventsSignature = signature;
  const icons = {play: 'play', upload: 'music', connection: 'pin', stop: 'stop', error: 'info', system: 'spark', delete: 'trash', complete: 'check'};
  $('event-list').innerHTML = events.length ? events.map((event) => `<div class="event-item"><span class="event-icon ${escapeHtml(event.kind)}">${icon(icons[event.kind] || 'activity')}</span><div class="event-text"><strong>${escapeHtml(event.title)}</strong>${event.detail ? `<p title="${escapeHtml(event.detail)}">${escapeHtml(event.detail)}</p>` : ''}</div><time datetime="${event.at}">${relativeTime(event.at)}</time></div>`).join('') : '<div class="empty-state">A quiet start. Your next action will appear here.</div>';
}
function renderSettings() {
  const system = state.system;
  const rows = [['Device', system.hostname], ['Portal version', system.version], ['Portal password', system.auth_enabled ? 'Enabled' : 'Not set · audio preview only'], ['Speaker player', system.server_available ? 'ffplay installed' : 'Install ffmpeg on the Pi'], ['Audio processing', system.ffmpeg_available ? 'ffmpeg installed' : 'Basic metadata + browser waveform'], ['Upload limit', `${system.max_upload_mb} MB per file`], ['Arrival detection', 'Not enabled · manual playback']];
  $('settings-list').innerHTML = rows.map(([label, value]) => `<div class="settings-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
  $('logout-button').hidden = !system.auth_enabled;
}

async function selectTrack(id) {
  if (id === state?.selected_id) return;
  clearTimeout(clipSaveTimer); clipSaveTimer = null;
  try {
    await stopPlayback(true);
    const result = await api('/api/selection', {track_id: id});
    applyState(result.state);
  } catch (error) { toast(error.message, true); }
}

function updateClip(start, duration, save = true) {
  const track = selectedTrack();
  if (!track) return;
  const minimum = Math.min(.1, track.duration);
  start = Math.max(0, Math.min(track.duration - minimum, Number(start) || 0));
  duration = Math.max(minimum, Math.min(track.duration - start, Number(duration) || minimum));
  state.clips[track.id] = {start: Math.round(start * 1000) / 1000, duration: Math.round(duration * 1000) / 1000};
  renderPlayer();
  if (save) {
    clearTimeout(clipSaveTimer);
    clipSaveTimer = setTimeout(saveClip, 450);
  }
}
async function saveClip() {
  clipSaveTimer = null;
  const track = selectedTrack();
  if (!track) return;
  try {
    const result = await api('/api/selection', {track_id: track.id, ...currentClip()});
    applyState(result.state);
  } catch (error) { toast(error.message, true); }
}

async function playClip() {
  if (playPending) return;
  if (isPlaying()) return stopPlayback();
  const track = selectedTrack();
  if (!track) return toast('Choose an audio file first.', true);
  clearTimeout(clipSaveTimer); clipSaveTimer = null;
  const clip = {...currentClip()};
  const ticket = ++playTicket;
  playPending = true;
  renderTransport();
  try {
    if (output === 'browser') {
      browserTrack = track.id;
      browserClip = {...clip};
      if (!audio.src.endsWith(track.url)) audio.src = track.url;
      audio.volume = state.volume / 100;
      audio.onloadedmetadata = () => { if (browserTrack === track.id && ticket === playTicket) audio.currentTime = clip.start; };
      try { audio.currentTime = clip.start; } catch { /* The metadata handler completes the seek. */ }
      await audio.play();
      if (ticket !== playTicket) return;
      renderTransport();
    }
    const result = await api('/api/play', {track_id: track.id, ...clip, output});
    if (ticket !== playTicket) return;
    applyState(result.state);
  } catch (error) {
    audio.pause(); browserTrack = null; browserClip = null;
    toast(error.name === 'NotAllowedError' ? 'Your browser blocked audio. Click Play clip again to allow it.' : error.message, true);
  } finally {
    playPending = false;
    renderTransport();
  }
}
async function stopPlayback(silent = false) {
  const wasPlaying = isPlaying();
  playTicket++;
  audio.pause(); browserTrack = null; browserClip = null;
  try {
    if (state?.playback.playing || (wasPlaying && output === 'server')) {
      const result = await api('/api/stop', {});
      applyState(result.state);
    }
    if (wasPlaying && !silent) toast('Playback stopped.');
  } catch (error) { if (!silent) toast(error.message, true); }
  renderTransport();
}

function tickPlayback() {
  if (state) {
    let position = null, elapsed = 0, duration = 0, playingId = null;
    if (isBrowserPlaying() && browserClip) {
      if (audio.currentTime >= browserClip.start + browserClip.duration - .015) {
        audio.pause(); browserTrack = null; browserClip = null; renderTransport();
      } else {
        position = audio.currentTime; elapsed = Math.max(0, position - browserClip.start);
        duration = browserClip.duration; playingId = browserTrack;
      }
    } else if (state.playback.playing) {
      elapsed = Math.min(state.playback.duration, state.playback.elapsed + (performance.now() - lastServerPoll) / 1000);
      position = state.playback.offset + elapsed;
      duration = state.playback.duration; playingId = state.playback.track_id;
    }
    const selected = selectedTrack();
    if (position !== null && selected?.id === playingId) {
      $('wave-playhead').hidden = false;
      $('wave-playhead').style.left = `${Math.min(100, position / selected.duration * 100)}%`;
      const timer = $('playback-time');
      if (timer) timer.textContent = `${formatTime(elapsed)} / ${formatTime(duration)}`;
    } else $('wave-playhead').hidden = true;
  }
  requestAnimationFrame(tickPlayback);
}

audio.addEventListener('ended', () => { browserTrack = null; browserClip = null; renderTransport(); });
audio.addEventListener('error', () => {
  if (browserTrack) { toast('This browser could not decode the audio. Try WAV or MP3, or use Server speaker.', true); browserTrack = null; browserClip = null; renderTransport(); }
});

function uploadFile(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/audio');
    xhr.setRequestHeader('X-Portal-CSRF', csrf);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        const percent = Math.round(event.loaded / event.total * 100);
        $('upload-progress-bar').style.width = `${percent}%`;
        $('dropzone-description').textContent = percent === 100 ? 'Reading your audio and preparing its waveform…' : `Uploading… ${percent}%`;
      }
    };
    xhr.onload = () => {
      let result;
      try { result = JSON.parse(xhr.responseText); }
      catch { reject(new Error('The server could not read this upload.')); return; }
      if (result.code === 'auth_required') { location.href = '/login'; reject(new Error('Sign in again.')); return; }
      if (xhr.status >= 400 || !result.ok) reject(new Error(result.error || 'Upload failed.'));
      else resolve(result);
    };
    xhr.onerror = () => reject(new Error('The upload was interrupted. Check your connection.'));
    const form = new FormData(); form.append('file', file); xhr.send(form);
  });
}
async function uploadFiles(files) {
  if (!files.length || $('dropzone').classList.contains('busy')) return;
  $('dropzone').classList.add('busy'); $('upload-button').disabled = true;
  $('upload-progress').hidden = false;
  let uploaded = 0;
  await stopPlayback(true);
  for (const file of files) {
    if (file.size > 50 * 1024 * 1024) { toast(`${file.name} is larger than 50 MB.`, true); continue; }
    $('dropzone-title').textContent = file.name;
    $('dropzone-description').textContent = 'Making room for a new sound…';
    $('upload-progress-bar').style.width = '0%';
    try { const result = await uploadFile(file); applyState(result.state); uploaded++; }
    catch (error) { toast(error.message, true); }
  }
  $('dropzone').classList.remove('busy'); $('upload-button').disabled = false;
  $('upload-progress').hidden = true;
  $('dropzone-title').textContent = 'Drop in your next entrance.';
  $('dropzone-description').innerHTML = 'Drag a file here or <span>browse files</span>';
  $('file-input').value = '';
  if (uploaded) toast(`${uploaded === 1 ? 'Your new sound is' : `${uploaded} new sounds are`} ready to play.`);
}

function confirmAction(title, description, actionLabel = 'Remove') {
  return new Promise((resolve) => {
    const dialog = $('confirm-dialog');
    $('confirm-title').textContent = title;
    $('confirm-description').textContent = description;
    $('confirm-action').textContent = actionLabel;
    let decided = false;
    function finish(value) { if (decided) return; decided = true; dialog.close(); resolve(value); }
    $('confirm-action').onclick = () => finish(true);
    $('confirm-cancel').onclick = () => finish(false);
    dialog.addEventListener('close', () => finish(false), {once: true});
    dialog.showModal();
  });
}

$('track-list').addEventListener('click', async (event) => {
  const remove = event.target.closest('[data-delete]');
  if (remove) {
    const track = state.tracks.find((item) => item.id === remove.dataset.delete);
    if (!track) return;
    if (!await confirmAction('Remove this sound?', `“${track.title}” will be deleted from this server. Your original file is not affected.`)) return;
    try {
      if (browserTrack === track.id) await stopPlayback(true);
      const result = await api(`/api/audio/${track.id}`, undefined, 'DELETE');
      applyState(result.state); toast(result.message);
    } catch (error) { toast(error.message, true); }
    return;
  }
  const row = event.target.closest('[data-track]');
  if (row) selectTrack(row.dataset.track);
});
$('track-list').addEventListener('keydown', (event) => {
  if (['Enter', ' '].includes(event.key) && event.target.matches('.track-row')) {
    event.preventDefault(); selectTrack(event.target.dataset.track);
  }
});
$('track-search').addEventListener('input', () => renderLibrary());
$('track-sort').addEventListener('change', () => renderLibrary());
$('upload-button').onclick = () => $('file-input').click();
$('dropzone').onclick = () => $('file-input').click();
$('dropzone').onkeydown = (event) => { if (['Enter', ' '].includes(event.key)) { event.preventDefault(); $('file-input').click(); } };
$('file-input').onchange = (event) => uploadFiles(Array.from(event.target.files));
for (const type of ['dragenter', 'dragover']) $('dropzone').addEventListener(type, (event) => { event.preventDefault(); $('dropzone').classList.add('dragover'); });
for (const type of ['dragleave', 'drop']) $('dropzone').addEventListener(type, (event) => { event.preventDefault(); $('dropzone').classList.remove('dragover'); });
$('dropzone').addEventListener('drop', (event) => uploadFiles(Array.from(event.dataTransfer.files)));
window.addEventListener('dragover', (event) => event.preventDefault());
window.addEventListener('drop', (event) => event.preventDefault());
$('play-button').onclick = playClip;
$('stop-button').onclick = () => stopPlayback();
for (const id of ['clip-start', 'clip-duration']) {
  $(id).addEventListener('input', () => {
    if ($(id).value === '') return;
    if (isPlaying()) stopPlayback(true);
    const clip = currentClip();
    updateClip(id === 'clip-start' ? $(id).value : clip.start, id === 'clip-duration' ? $(id).value : clip.duration);
  });
  $(id).addEventListener('blur', () => renderPlayer());
}
$('output-select').onchange = async (event) => {
  const next = event.target.value;
  await stopPlayback(true); output = next;
  try { localStorage.setItem('threshold-output', output); } catch { /* Third-party iframe storage may be disabled. */ }
  renderOutput(); renderTransport();
};
$('volume').addEventListener('input', (event) => {
  if (!state) return;
  state.volume = Number(event.target.value); audio.volume = state.volume / 100;
  $('volume-value').textContent = `${state.volume}%`;
  clearTimeout(volumeSaveTimer);
  volumeSaveTimer = setTimeout(async () => {
    try { const result = await api('/api/settings', {volume: state.volume}); applyState(result.state); }
    catch (error) { toast(error.message, true); }
  }, 250);
});

$('waveform-wrap').addEventListener('pointerdown', (event) => {
  const track = selectedTrack(); if (!track) return;
  event.preventDefault();
  if (isPlaying()) stopPlayback(true);
  const rect = $('waveform-wrap').getBoundingClientRect();
  const position = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * track.duration;
  const clip = currentClip();
  const handle = event.target.closest('.wave-handle');
  dragging = {which: handle?.id === 'end-handle' ? 'end' : 'start', end: clip.start + clip.duration, start: clip.start};
  if (!handle) updateClip(position, Math.min(clip.duration, track.duration - position), false);
  $('waveform-wrap').setPointerCapture(event.pointerId);
});
$('waveform-wrap').addEventListener('pointermove', (event) => {
  if (!dragging || !selectedTrack()) return;
  const rect = $('waveform-wrap').getBoundingClientRect();
  const position = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * selectedTrack().duration;
  if (dragging.which === 'end') updateClip(dragging.start, Math.max(.1, position - dragging.start), false);
  else { const start = Math.min(dragging.end - .1, position); updateClip(start, dragging.end - start, false); }
});
function finishDrag() { if (dragging) { dragging = null; clearTimeout(clipSaveTimer); clipSaveTimer = setTimeout(saveClip, 200); } }
$('waveform-wrap').addEventListener('pointerup', finishDrag);
$('waveform-wrap').addEventListener('pointercancel', finishDrag);
for (const [id, which] of [['start-handle', 'start'], ['end-handle', 'end']]) {
  $(id).addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault(); const clip = currentClip(); const delta = event.key === 'ArrowLeft' ? -.1 : .1;
    if (which === 'start') updateClip(clip.start + delta, clip.duration - delta);
    else updateClip(clip.start, clip.duration + delta);
  });
}

$('toggle-token').onclick = () => {
  const showing = $('token-input').type === 'password';
  $('token-input').type = showing ? 'text' : 'password';
  $('toggle-token').setAttribute('aria-label', showing ? 'Hide token' : 'Show token');
};
$('token-form').onsubmit = async (event) => {
  event.preventDefault();
  if (!state.system.auth_enabled) { $('help-dialog').showModal(); return; }
  const token = $('token-input').value.trim();
  if (!token) { $('token-input').focus(); toast('Paste an access token first.', true); return; }
  $('save-token').disabled = true; $('save-token').querySelector('span').textContent = 'Testing…';
  try {
    const result = await api('/api/life360/token', {token});
    $('token-input').value = ''; $('token-input').type = 'password';
    applyState(result.state); toast(result.message);
  } catch (error) { toast(error.message, true); }
  finally { $('save-token').disabled = false; renderConnection(); }
};
$('test-connection').onclick = async () => {
  $('test-connection').disabled = true;
  try { const result = await api('/api/life360/test', {}); applyState(result.state); toast(result.message); }
  catch (error) { toast(error.message, true); }
  finally { $('test-connection').disabled = false; }
};
$('forget-token').onclick = async () => {
  if (!await confirmAction('Disconnect Life360?', 'This removes the saved token from this device. It does not revoke the token at Life360.', 'Disconnect')) return;
  try { const result = await api('/api/life360/token', undefined, 'DELETE'); applyState(result.state); toast(result.message); }
  catch (error) { toast(error.message, true); }
};
$('connection-summary').onclick = () => { $('connection').scrollIntoView({behavior: 'smooth', block: 'center'}); setTimeout(() => $('token-input').focus({preventScroll: true}), 350); };
$('output-summary').onclick = () => { $('player').scrollIntoView({behavior: 'smooth', block: 'center'}); setTimeout(() => $('output-select').focus({preventScroll: true}), 350); };
$('settings-button').onclick = () => $('settings-dialog').showModal();
$('help-button').onclick = $('security-guide-button').onclick = () => $('help-dialog').showModal();
$('logout-button').onclick = async () => { await stopPlayback(true); try { await api('/logout', {}); location.href = '/login'; } catch (error) { toast(error.message, true); } };
for (const button of document.querySelectorAll('[data-close-dialog]')) button.onclick = () => button.closest('dialog').close();
for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click', (event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
for (const link of document.querySelectorAll('.main-nav a')) link.addEventListener('click', () => { document.querySelectorAll('.main-nav a').forEach((item) => item.classList.toggle('active', item === link)); });
function updateClock() { $('local-clock').textContent = new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}); }
updateClock(); setInterval(updateClock, 10000);
refreshState(false);
setInterval(() => refreshState(), 15000);
setInterval(() => { if (state?.playback.playing) refreshState(); }, 900);
requestAnimationFrame(tickPlayback);
