'use strict';

/** Stream the selected file; schedule its fades on the Web Audio clock, not UI frames. */
class BrowserEnvelope {
  constructor(audio) {
    this.audio = audio;
    this.context = null;
    this.fadeGain = null;
    this.volumeGain = null;
    this.clip = null;
    this.endTimer = null;
    for (const event of ['playing', 'seeked']) audio.addEventListener(event, () => this.schedule());
    for (const event of ['waiting', 'seeking', 'pause']) audio.addEventListener(event, () => this.hold());
    audio.addEventListener('timeupdate', () => {
      if (this.clip && audio.currentTime >= this.clip.start + this.clip.duration) this.finish();
    });
    audio.addEventListener('ended', () => this.stop());
  }

  prepare(clip, volume) {
    this.stop();
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass && (clip.fade_in || clip.fade_out)) {
      throw new Error('This browser cannot preview fades. Try a modern browser or use Server speaker.');
    }
    if (!this.context && AudioContextClass) {
      this.context = new AudioContextClass();
      this.source = this.context.createMediaElementSource(this.audio);
      this.fadeGain = this.context.createGain();
      this.volumeGain = this.context.createGain();
      this.source.connect(this.fadeGain);
      this.fadeGain.connect(this.volumeGain);
      this.volumeGain.connect(this.context.destination);
      this.context.addEventListener('statechange', () => {
        if (this.context.state === 'running') this.schedule();
      });
    }
    this.clip = {...clip};
    this.setVolume(volume);
    if (this.context) {
      const now = this.context.currentTime;
      this.fadeGain.gain.cancelScheduledValues(now);
      this.fadeGain.gain.setValueAtTime(clip.fade_in > 0 ? 0 : 1, now);
      return this.context.resume();
    }
    return Promise.resolve();
  }

  setVolume(volume) {
    const value = Math.max(0, Math.min(1, Number(volume) || 0));
    if (this.volumeGain) {
      this.audio.volume = 1;
      this.volumeGain.gain.setValueAtTime(value, this.context.currentTime);
    } else this.audio.volume = value;
  }

  gainAt(elapsed) {
    const clip = this.clip;
    if (!clip) return 1;
    if (elapsed < 0 || elapsed >= clip.duration) return 0;
    if (clip.fade_in > 0 && elapsed < clip.fade_in) return elapsed / clip.fade_in;
    if (clip.fade_out > 0 && elapsed > clip.duration - clip.fade_out) return (clip.duration - elapsed) / clip.fade_out;
    return 1;
  }

  hold() {
    clearTimeout(this.endTimer);
    if (this.fadeGain && this.clip) {
      const now = this.context.currentTime;
      this.fadeGain.gain.cancelScheduledValues(now);
      this.fadeGain.gain.setValueAtTime(this.gainAt(this.audio.currentTime - this.clip.start), now);
    }
  }

  schedule() {
    clearTimeout(this.endTimer);
    if (!this.clip || this.audio.paused) return;
    const {start, duration, fade_in: fadeIn, fade_out: fadeOut} = this.clip;
    const elapsed = this.audio.currentTime - start;
    if (elapsed < -.03) return; // Wait for the requested seek, rather than fading the wrong section.
    const remaining = duration - Math.max(0, elapsed);
    if (remaining <= 0) return this.finish();
    if (this.fadeGain) {
      const now = this.context.currentTime;
      const gain = this.fadeGain.gain;
      const untilFull = Math.max(0, fadeIn - Math.max(0, elapsed));
      gain.cancelScheduledValues(now);
      gain.setValueAtTime(this.gainAt(Math.max(0, elapsed)), now);
      if (untilFull > 0) gain.linearRampToValueAtTime(1, now + untilFull);
      if (fadeOut > 0) {
        const untilFade = duration - fadeOut - Math.max(0, elapsed);
        if (untilFade > untilFull + .00001) gain.setValueAtTime(1, now + untilFade);
        gain.linearRampToValueAtTime(0, now + remaining);
      } else {
        // The clip stays bounded even if a background tab throttles JavaScript timers.
        gain.setValueAtTime(0, now + remaining + .001);
      }
    }
    this.endTimer = setTimeout(() => {
      if (!this.clip || this.audio.paused) return;
      if (this.audio.currentTime >= start + duration) this.finish();
      else this.schedule(); // Buffering or a seek changed the media clock.
    }, remaining * 1000 + 20);
  }

  finish() {
    if (!this.clip) return;
    this.audio.pause();
    this.stop();
    this.audio.dispatchEvent(new Event('clipended'));
  }

  stop() {
    clearTimeout(this.endTimer);
    this.endTimer = null;
    this.clip = null;
    if (this.fadeGain) {
      const now = this.context.currentTime;
      this.fadeGain.gain.cancelScheduledValues(now);
      this.fadeGain.gain.setValueAtTime(1, now);
    }
  }
}
