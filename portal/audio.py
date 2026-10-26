"""Audio inspection, demo generation, and actual server-side playback via ffplay."""
import array
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import wave

from mutagen import File as MutagenFile

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}
MAX_DURATION = 30 * 60


def audio_tools():
    return {name: shutil.which(name) for name in ("ffmpeg", "ffprobe", "ffplay")}


def inspect_audio(path):
    tools = audio_tools()
    duration = None
    if tools["ffprobe"]:
        import json
        try:
            result = subprocess.run([tools["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path)],
                capture_output=True, timeout=15)
            metadata = json.loads(result.stdout)
            if not metadata.get("streams"):
                raise ValueError("The file does not contain a readable audio track.")
            duration = float(metadata["format"]["duration"])
        except (subprocess.SubprocessError, ValueError, KeyError):
            duration = None
    if duration is None:
        try:
            media = MutagenFile(path)
            duration = float(media.info.length) if media and media.info else None
        except Exception:
            duration = None
    if duration is None and Path(path).suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as file:
                duration = file.getnframes() / file.getframerate()
        except (wave.Error, EOFError):
            pass
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError("This file could not be read as audio. Try a WAV or MP3 file, or install ffmpeg for more formats.")
    if duration > MAX_DURATION:
        raise ValueError("Choose an audio file shorter than 30 minutes.")
    return round(duration, 3), waveform(path, duration)


def waveform(path, duration, count=128):
    samples = None
    try:
        if Path(path).suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as file:
                if file.getsampwidth() == 2:
                    samples = array.array("h", file.readframes(file.getnframes()))
                    if sys.byteorder != "little":
                        samples.byteswap()
                    if file.getnchannels() > 1:
                        samples = samples[::file.getnchannels()]
        if samples is None and shutil.which("ffmpeg"):
            result = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(path),
                "-t", str(min(duration, MAX_DURATION)), "-ac", "1", "-ar", "4000", "-f", "s16le", "pipe:1"],
                capture_output=True, timeout=25)
            if result.returncode == 0:
                samples = array.array("h", result.stdout[:len(result.stdout) // 2 * 2])
                if sys.byteorder != "little":
                    samples.byteswap()
        if not samples:
            return None
        step = max(1, len(samples) // count)
        peaks = [max((abs(value) for value in samples[index * step:(index + 1) * step]), default=0)
                 for index in range(count)]
        maximum = max(peaks) or 1
        return [round(max(0.035, value / maximum), 3) for value in peaks]
    except (OSError, wave.Error, EOFError, subprocess.SubprocessError):
        return None


def generate_demo(path):
    """Generate an original 12-second instrumental chime, not a downloaded song."""
    rate, duration = 22050, 12
    notes = [(0.1, 261.63), (0.52, 329.63), (0.94, 392.0), (1.58, 523.25),
             (3.35, 329.63), (3.82, 392.0), (4.32, 587.33), (4.95, 523.25),
             (6.65, 392.0), (7.14, 329.63), (7.68, 261.63)]
    output = array.array("h")
    for index in range(rate * duration):
        t = index / rate
        value = 0.0
        for onset, frequency in notes:
            dt = t - onset
            if 0 <= dt < 3:
                attack = min(1.0, dt * 80)
                value += .21 * attack * math.exp(-2.0 * dt) * (
                    math.sin(2 * math.pi * frequency * dt) +
                    .28 * math.sin(2 * math.pi * frequency * 2 * dt))
        fade = min(1.0, t * 5, (duration - t) / 2)
        output.append(int(max(-.9, min(.9, value * fade)) * 32767))
    if sys.byteorder != "little":
        output.byteswap()
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(output.tobytes())
    os.chmod(path, 0o600)


class Player:
    def __init__(self, on_event):
        self.lock = threading.RLock()
        self.process = None
        self.generation = 0
        self.started_at = None
        self.track_id = None
        self.duration = 0
        self.offset = 0
        self.title = ""
        self.last_error = None
        self.on_event = on_event

    def capabilities(self):
        tools = audio_tools()
        hardware = bool(os.environ.get("PULSE_SERVER") or os.environ.get("SDL_AUDIODRIVER"))
        try:
            cards = Path("/proc/asound/cards").read_text()
            hardware = hardware or "no soundcards" not in cards.lower()
        except OSError:
            pass
        return {"server_available": bool(tools["ffplay"]), "hardware_detected": hardware,
            "ffmpeg_available": bool(tools["ffmpeg"]),
            "server_hint": "ffplay is installed; the operating system's default audio device will be used."
                if tools["ffplay"] else "Install ffmpeg on the Pi to enable speaker playback. Browser playback works now."}

    def state(self):
        with self.lock:
            playing = self.process is not None and self.process.poll() is None
            return {"playing": playing, "track_id": self.track_id if playing else None,
                "elapsed": min(self.duration, max(0, time.monotonic() - self.started_at)) if playing else 0,
                "duration": self.duration if playing else 0, "offset": self.offset if playing else 0,
                "error": self.last_error}

    def play(self, path, track, start, duration, volume):
        executable = shutil.which("ffplay")
        if not executable:
            raise ValueError("Server playback needs ffplay. On the Pi, run: sudo apt install ffmpeg. You can use This browser in the meantime.")
        self.stop(log=False)
        with self.lock:
            self.generation += 1
            generation = self.generation
            command = [executable, "-nodisp", "-autoexit", "-loglevel", "error", "-nostats",
                "-protocol_whitelist", "file,pipe", "-ss", str(start), "-t", str(duration),
                "-volume", str(round(volume)), str(path)]
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except OSError:
                raise ValueError("The server could not start ffplay. Check the ffmpeg installation.") from None
            self.process = process
            self.started_at = time.monotonic()
            self.track_id = track["id"]
            self.duration = duration
            self.offset = start
            self.title = track["title"]
            self.last_error = None
        self.on_event("play", "Playing on the speaker", track["title"])
        threading.Thread(target=self._watch, args=(process, generation), daemon=True).start()
        return self.state()

    def _watch(self, process, generation):
        _, stderr = process.communicate()
        with self.lock:
            if generation != self.generation:
                return
            self.process = None
            # ffplay can exit 0 even when SDL cannot open an audio device.
            # Inspect known fatal messages rather than claiming silent success.
            diagnostic = (stderr or b"").decode(errors="replace").lower()
            audio_failed = any(message in diagnostic for message in (
                "audio open failed", "failed to open file", "no available audio device",
                "couldn't open audio device", "failed to initialize", "failed to open audio"))
            if process.returncode != 0 or audio_failed:
                self.last_error = "Speaker playback failed. Check that the Pi has a working default audio output (try ffplay from its terminal)."
                self.on_event("error", "Speaker needs attention", self.last_error)
            else:
                self.on_event("complete", "Clip finished", self.title)

    def stop(self, log=True):
        with self.lock:
            process = self.process
            self.generation += 1
            self.process = None
            self.last_error = None
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                if log:
                    self.on_event("stop", "Playback stopped", self.title)
