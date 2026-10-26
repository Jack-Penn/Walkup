'use strict';

/** Small people/place controls; playback and manual clip editing stay in the main player. */
class ArrivalsUI {
  constructor(hooks) {
    this.h = hooks;
    this.lastPeople = '';
    this.lastCircles = '';
    this.lastPlaces = '';
    this.busyPeople = new Set();
    this.saving = false;
    this.$ = (id) => document.getElementById(id);
    this.$('sync-people').onclick = () => this.sync();
    this.$('arrival-setup-button').onclick = this.$('arrival-mode-button').onclick = () => this.openSettings();
    this.$('people-search').oninput = () => this.renderPeople(true);
    this.$('people-circle').onchange = () => this.renderPeople(true);
    this.$('home-radius').oninput = () => this.renderRadiusHint();
    this.$('home-place').onchange = () => this.renderForm(false);
    this.$('arrival-settings-form').onsubmit = (event) => { event.preventDefault(); this.saveSettings(); };
    this.$('people-list').addEventListener('click', (event) => this.handlePerson(event));
  }

  get data() { return this.h.getState()?.arrivals; }
  canAccess() { const state = this.h.getState(); return !!state?.system.auth_enabled && !!state?.life360.configured; }

  render() {
    if (!this.data) return;
    const state = this.h.getState();
    const data = this.data;
    const runtime = data.runtime;
    this.$('people-count').textContent = data.people.length;
    this.$('arrival-mode-label').textContent = data.enabled ? 'Proximity on' : 'Arrivals off';
    this.$('arrival-mode-button').classList.toggle('enabled', data.enabled);
    this.$('arrival-mode-tooltip').textContent = data.enabled ? 'Assigned clips play on the Pi when a fresh location changes from away to nearby.' : 'Choose a saved Life360 home place and assign welcome clips to enable proximity playback.';
    this.$('home-summary-title').textContent = data.home ? data.home.name : 'Choose your home place';
    this.$('home-summary-detail').textContent = data.home ? `${data.home.circle_name} · ${data.radius_m} m proximity` : 'A saved place from any of your Circles';
    this.$('arrival-setup-button').textContent = data.home ? 'Settings' : 'Set up';
    const needsToken = state.life360.status === 'reauth_required';
    const busy = !needsToken && (runtime.syncing || runtime.sync_pending);
    this.$('sync-people').disabled = !this.canAccess() || needsToken || busy;
    this.$('sync-people-label').textContent = busy ? 'Syncing…' : 'Sync Circles';
    this.$('sync-people').classList.toggle('is-syncing', busy);
    let note = !this.canAccess() ? 'Connect Life360 on your password-protected portal to load people and saved places.'
      : runtime.phase === 'backoff' ? `Life360 is paused. Retrying in ${Math.max(1, Math.ceil(runtime.retry_after / 60))} min. ${runtime.error || ''}`
      : runtime.phase === 'reauth_required' ? 'Replace the rejected Life360 token to resume arrival monitoring.'
      : busy ? 'Loading people and saved places from all your accessible Circles…'
      : data.enabled ? `Checking roughly every ${data.poll_seconds} seconds · ${runtime.last_poll_at ? `last checked ${this.relative(runtime.last_poll_at).toLowerCase()}` : 'waiting for the first fresh location'}`
      : 'Arrivals are off. Manual playback still works as before.';
    if (runtime.active) note = `Welcoming ${runtime.active.name} on the Pi speaker${runtime.queue_count ? ` · ${runtime.queue_count} queued` : ''}.`;
    else if (runtime.queue_count) note = `${runtime.queue_count} welcome ${runtime.queue_count === 1 ? 'clip' : 'clips'} queued${runtime.manual_active ? ' · waiting for your preview to finish' : ''}.`;
    if (runtime.speaker_error) note = runtime.speaker_error;
    this.$('arrival-runtime-note').textContent = note;
    this.$('arrival-runtime-note').classList.toggle('has-error', !!runtime.error || !!runtime.speaker_error);
    this.$('people-toolbar').hidden = !data.people.length;
    const circles = JSON.stringify(data.circles);
    if (circles !== this.lastCircles) {
      this.lastCircles = circles;
      const selected = this.$('people-circle').value;
      this.$('people-circle').innerHTML = '<option value="all">All Circles</option>' + data.circles.map((circle) => `<option value="${this.h.escapeHtml(circle.id)}">${this.h.escapeHtml(circle.name)}</option>`).join('');
      this.$('people-circle').value = data.circles.some((circle) => circle.id === selected) ? selected : 'all';
    }
    this.renderPeople();
    this.renderSelection();
    this.renderForm(false);
  }

  relative(value) {
    const age = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    return age < 45 ? 'just now' : age < 3600 ? `${Math.floor(age / 60)}m ago` : `${Math.floor(age / 3600)}h ago`;
  }

  time(value) { const number = Math.max(0, Number(value) || 0); return `${Math.floor(number / 60)}:${String(Math.floor(number % 60)).padStart(2, '0')}`; }
  seconds(value) { return Number(Number(value).toFixed(3)); }

  renderSelection() {
    if (!this.data) return;
    const track = this.h.getTrack();
    const clip = this.h.getClip();
    this.$('arrival-selection-note').textContent = track
      ? `Selected: ${track.title} · from ${this.time(clip.start)} · ${this.seconds(Math.min(30, clip.duration))}s${clip.duration > 30 ? ' (first 30s only for arrivals)' : ''}`
      : 'Choose and trim a track in the player, then assign it to a person.';
    for (const button of this.$('people-list').querySelectorAll('[data-person-assign]')) {
      button.disabled = !track || !this.canAccess() || this.h.getBusy() || this.busyPeople.has(button.dataset.personAssign);
    }
  }

  renderPeople(force = false) {
    if (!this.data) return;
    const data = this.data;
    const query = this.$('people-search').value.toLocaleLowerCase().trim();
    const circle = this.$('people-circle').value;
    const people = data.people.filter((person) => person.name.toLocaleLowerCase().includes(query) && (circle === 'all' || person.circle_ids.includes(circle)));
    const state = this.h.getState();
    const signature = JSON.stringify([people, data.assignments, data.observations, data.enabled, query, circle, state.tracks.map((track) => [track.id, track.title]), [...this.busyPeople], state.system.server_available]);
    if (!force && signature === this.lastPeople) return;
    this.lastPeople = signature;
    if (!people.length) {
      const empty = !data.people.length;
      this.$('people-list').innerHTML = `<div class="empty-state people-empty">${this.h.icon(empty ? 'users' : 'search')}<strong>${empty ? 'A welcome with their name on it.' : 'No people match this search.'}</strong><p>${empty ? 'Sync Life360 to see people from every Circle you belong to. No duplicate profiles.' : 'Try another name or choose All Circles.'}</p></div>`;
      return;
    }
    const labels = {home: 'Home', away: 'Away', nearby: 'Nearby', stale: 'Out-of-date GPS', poor_accuracy: 'GPS too broad', unavailable: 'GPS unavailable', waiting: 'Waiting for GPS'};
    const escape = this.h.escapeHtml;
    this.$('people-list').innerHTML = people.map((person) => {
      const assignment = data.assignments[person.id];
      const track = assignment && state.tracks.find((track) => track.id === assignment.track_id);
      const status = data.enabled ? data.observations[person.id]?.status || 'waiting' : 'off';
      const initials = person.name.trim().split(/\s+/).slice(0, 2).map((part) => Array.from(part)[0] || '').join('').toLocaleUpperCase();
      const busy = this.busyPeople.has(person.id);
      const summary = track ? `${track.title} · ${this.time(assignment.start)} · ${this.seconds(assignment.duration)}s` : 'No welcome clip assigned';
      const fadeTitle = assignment ? `Fade in ${assignment.fade_in}s · fade out ${assignment.fade_out}s. Load this clip into the player to edit it.` : '';
      return `<div class="person-row" data-person="${escape(person.id)}"><span class="person-avatar" aria-hidden="true">${escape(initials)}</span><div class="person-content"><div class="person-name-line"><strong>${escape(person.name)}</strong><span class="person-status ${escape(status)}">${escape(status === 'off' ? 'Not monitoring' : labels[status] || 'Waiting for GPS')}</span></div><p class="person-circles" title="${escape(person.circle_names.join(' · '))}">${escape(person.circle_names.join(' · '))}</p>${track ? `<button class="person-assignment" data-person-load="${escape(person.id)}" title="${escape(fadeTitle)}" aria-label="Load assigned clip for ${escape(person.name)} into the player">${this.h.icon('music')}<span>${escape(summary)}</span>${this.h.icon('chevron')}</button>` : `<p class="person-unassigned">${escape(summary)}</p>`}</div><div class="person-actions"><button class="button secondary person-assign" data-person-assign="${escape(person.id)}" ${busy ? 'disabled' : ''}>Use selected</button><button class="person-icon" data-person-test="${escape(person.id)}" aria-label="Test welcome for ${escape(person.name)} on the Pi speaker" title="Test on Pi speaker" ${!track || busy || !state.system.server_available ? 'disabled' : ''}>${this.h.icon('play')}</button><button class="person-icon" data-person-clear="${escape(person.id)}" aria-label="Unassign welcome clip for ${escape(person.name)}" title="Unassign clip" ${!assignment || busy ? 'disabled' : ''}>${this.h.icon('trash')}</button></div></div>`;
    }).join('');
    this.renderSelection();
  }

  renderRadiusHint() {
    const radius = Number(this.$('home-radius').value) || 100;
    this.$('home-radius-hint').textContent = `About ${Math.round(radius * 3.28084)} ft. Re-arms after a confirmed departure beyond ${Math.round(radius + Math.max(50, radius * .5))} m.`;
  }

  renderForm(force = false) {
    if (!this.data) return;
    const data = this.data;
    const dialog = this.$('arrival-settings-dialog');
    const select = this.$('home-place');
    const placeSignature = JSON.stringify(data.places);
    if (force || placeSignature !== this.lastPlaces) {
      const selected = !force && dialog.open ? select.value : data.home?.key || '';
      this.lastPlaces = placeSignature;
      select.innerHTML = '<option value="">Choose a saved place…</option>' + data.places.map((place) => `<option value="${this.h.escapeHtml(place.key)}">${this.h.escapeHtml(place.name)} · ${this.h.escapeHtml(place.circle_name)}</option>`).join('');
      select.value = data.places.some((place) => place.key === selected) ? selected : data.home?.key || '';
    }
    if (force || !dialog.open) {
      select.value = data.home?.key || '';
      this.$('home-radius').value = data.radius_m;
      this.$('arrivals-enabled').checked = data.enabled;
    }
    select.disabled = !this.canAccess() || !data.places.length;
    this.$('home-radius').disabled = !this.canAccess();
    const visible = new Set(data.people.map((person) => person.id));
    const hasAssignment = Object.keys(data.assignments).some((id) => visible.has(id));
    const needsToken = this.h.getState().life360.status === 'reauth_required';
    const prerequisites = !needsToken && !!select.value && hasAssignment && this.h.getState().system.server_available && !data.runtime.sync_pending && !data.runtime.syncing;
    this.$('arrivals-enabled').disabled = !this.canAccess() || (!data.enabled && !prerequisites);
    this.$('save-arrival-settings').disabled = !this.canAccess() || this.saving || !data.places.length;
    this.$('save-arrival-settings').textContent = this.saving ? 'Saving…' : 'Save settings';
    this.$('arrival-setup-note').textContent = !this.canAccess() ? 'Start your portal with a password, connect Life360, then sync your Circles.'
      : needsToken ? 'Replace the rejected Life360 token before enabling arrivals or syncing your Circles.'
      : !data.places.length ? 'No saved places loaded. Add a Home place in Life360, then choose Sync Circles.'
      : !hasAssignment ? 'Save your home place, then assign a welcome clip to at least one person before enabling arrivals.'
      : !this.h.getState().system.server_available ? 'Automatic playback requires ffplay on the Pi. Install ffmpeg first.'
      : 'People already home when monitoring starts will not trigger a welcome. GPS updates can be delayed; this is not a door-open detector.';
    this.renderRadiusHint();
  }

  openSettings() {
    this.renderForm(true);
    if (!this.$('arrival-settings-dialog').open) this.$('arrival-settings-dialog').showModal();
  }

  async saveSettings() {
    if (this.saving) return;
    this.saving = true; this.renderForm(false);
    try {
      const result = await this.h.api('/api/arrivals/settings', {home_key: this.$('home-place').value,
        radius_m: Number(this.$('home-radius').value), enabled: this.$('arrivals-enabled').checked});
      this.h.applyState(result.state);
      this.$('arrival-settings-dialog').close();
      this.h.toast(result.message);
    } catch (error) { this.h.toast(error.message, true); }
    finally { this.saving = false; this.renderForm(false); }
  }

  async sync() {
    if (!this.canAccess()) return;
    this.$('sync-people').disabled = true;
    try { const result = await this.h.api('/api/arrivals/sync', {}); this.h.applyState(result.state); this.h.toast(result.message); }
    catch (error) { this.h.toast(error.message, true); this.render(); }
  }

  async handlePerson(event) {
    const button = event.target.closest('[data-person-assign], [data-person-load], [data-person-test], [data-person-clear]');
    if (!button || button.disabled || !this.data) return;
    const member = button.dataset.personAssign || button.dataset.personLoad || button.dataset.personTest || button.dataset.personClear;
    if (this.busyPeople.has(member)) return;
    const assignment = this.data.assignments[member];
    if (button.hasAttribute('data-person-load')) {
      if (assignment) await this.h.loadClip(assignment);
      return;
    }
    this.busyPeople.add(member); this.renderPeople(true);
    try {
      let result;
      if (button.hasAttribute('data-person-clear')) {
        result = await this.h.api(`/api/arrivals/assignments/${encodeURIComponent(member)}`, undefined, 'DELETE');
      } else if (button.hasAttribute('data-person-test')) {
        await this.h.stopPlayback();
        result = await this.h.api(`/api/arrivals/assignments/${encodeURIComponent(member)}/test`, {});
      } else {
        const track = this.h.getTrack();
        if (!track) throw new Error('Choose and trim an uploaded track in the player first.');
        const clip = {...this.h.getClip()};
        clip.duration = Math.min(30, clip.duration);
        if (clip.fade_in + clip.fade_out > clip.duration) {
          clip.fade_in = Math.round(clip.fade_in * clip.duration / (clip.fade_in + clip.fade_out) * 1000) / 1000;
          clip.fade_out = Math.round((clip.duration - clip.fade_in) * 1000) / 1000;
        }
        // Assigning is an explicit save action: also flush the unmodified manual clip.
        await this.h.saveClip();
        result = await this.h.api(`/api/arrivals/assignments/${encodeURIComponent(member)}`, {track_id: track.id, source: 'upload', ...clip});
      }
      this.h.applyState(result.state); this.h.toast(result.message);
    } catch (error) { this.h.toast(error.message, true); }
    finally { this.busyPeople.delete(member); this.renderPeople(true); this.renderSelection(); }
  }
}
