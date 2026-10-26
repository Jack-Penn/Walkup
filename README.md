# Threshold

A small, self-hosted control room for your homecoming soundtrack. One Raspberry Pi, a wired powered speaker, and a browser. No ESP32, cloud audio service, database server, or Home Assistant required.

## What works

- Upload MP3, WAV, OGG, M4A, FLAC, AAC, or OPUS audio (50 MB/file; 30 minutes maximum).
- Search, sort, select, and delete audio files.
- Pick a clip's **start time and duration**, with a waveform and draggable selection handles.
- Play/stop the clip through **the server's speaker** or **this browser**. Choose the output explicitly.
- Set playback volume. Browser volume updates live; server volume is applied at the start of the next playback.
- Replace your Life360 token and verify it before saving; test the saved token or remove it from this server.
- Keep audio, selections, volume, events, and credentials across restarts.
- Optional portal sign-in. **A portal password is required before any Life360 token can be stored or tested.**
- A generated, original 12-second demo chime is included on first launch.

This is a **manual portal**, not an arrival automation engine. It does not yet monitor locations, attach songs to residents, detect a door, or record video.

## Raspberry Pi setup

Use Raspberry Pi OS with Python 3.9 or newer. A Pi 3/4/5 or Zero 2 W is a reasonable starting point; a USB sound adapter is useful on models without a headphone output. Use a powered speaker, not a passive speaker connected directly to GPIO.

```bash
sudo apt update
sudo apt install -y python3-venv ffmpeg
cd threshold
chmod +x run.sh
./run.sh
```

The launcher creates a virtual environment, installs the three Python dependencies if needed, and asks for a **portal password** without displaying it. This is NOT your Life360 password. The password is kept in the server process environment, not written to the project.

Open **http://raspberrypi.local:8080** or **http://YOUR_PI_IP:8080**, then sign in using the portal password. You must provide the same password when restarting, or intentionally choose a new one; the existing data is unaffected.

### Manual launch

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
read -r -s -p 'Portal password: ' PORTAL_PASSWORD; echo
export PORTAL_PASSWORD
python3 app.py
```

For an **audio-only preview**, `python3 app.py` without `PORTAL_PASSWORD` works too. Token storage and connection tests are intentionally unavailable until you restart with a password. If credentials have already been saved, a password is required to start the server at all.

## Speaker playback

1. Connect your powered speaker via the Pi's supported audio output, USB sound adapter, or HDMI.
2. Configure that device as the default output in Raspberry Pi OS.
3. Run `ffplay -nodisp -autoexit /path/to/a/test.wav` in the **same user session** that will run Threshold. Confirm you hear sound.
4. Select **Server speaker** in the portal and click **Play clip**.

`ffplay` is installed by the `ffmpeg` package. It performs real server-side playback; the browser does not need to remain open. Only one server clip plays at a time. Linux sound permissions and session/PipeWire/ALSA configuration can affect playback, especially if you later run this as a boot service.

**This browser** is a separate preview output using HTML audio. It stops when the page is closed. There is no silent fallback between the two outputs. A hosted development preview has no physical Pi speaker, so browser playback is the default there.

Without ffmpeg, WAV and many other audio formats can still be inspected with Mutagen; browser playback remains available. The browser tries to generate missing waveforms for smaller files. ffmpeg/ffprobe provide broader decoding and server-side waveforms.

## Life360 token management

1. Log in to your own account at https://life360.com/login.
2. In browser developer tools, use Application → Cookies → the Life360 entry to copy `LIFE360_AUTH_TOKEN` (or inspect your own login response as appropriate).
3. In Threshold, paste the token into **Life360 access token** and choose **Save & test**. A `Bearer ` prefix is accepted.
4. The server checks `/v3/users/me` over HTTPS, with certificate validation enabled and redirects refused. It only saves the replacement after a successful account-profile response.

This reuses the transport from the earlier working `life360_test.py`. Life360's API is unofficial and can change. **This portal does not perform automatic login, extend a token, or exchange refresh tokens.**

A failed replacement preserves the previous saved token. A saved-token test marks a 401 as needing reauthentication; a 403 does not delete credentials; a 429 results in a cooldown. There is a minimum 10-second spacing between explicit connection tests, longer after blocking or rate limits. The portal does not automatically poll Life360 when the page is open.

Raw tokens are never included in HTML, API state responses, events, or application messages. The input clears after a successful save. Removing a token here removes the local copy; it does **not** revoke it at Life360. Use location information only with the participants' consent.

## Data and configuration

Data defaults to `threshold/data/`:

- `audio/`: uploaded audio, stored with generated filenames.
- `state.json`: track metadata, clip selections, volume, account display status, recent events.
- `credentials.json`: the Life360 access token, readable by the server's OS user (`0600`). It is protected by filesystem permissions, **not encrypted at rest**.
- `session.key`: a persistent random signing key for portal sessions (`0600`).

Back up this directory securely. Do not commit it or share it in a project archive. The supplied source ZIP excludes runtime data and credentials.

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `PORTAL_PASSWORD` | unset | Enables portal sign-in and unlocks token management. |
| `PORT` | `8080` | HTTP port. |
| `PORTAL_DATA_DIR` | `./data` beside app.py | Alternate persistent data directory. |
| `PORTAL_ALLOWED_HOSTS` | unset | Comma-separated additional hostnames you own. Local/private IPs, local hostnames, `.local`, `.lan`, `.home.arpa`, and development `.e2b.app` previews work by default. |
| `PORTAL_SECURE_COOKIE` | `0` | Set to `1` when serving the portal through HTTPS. |

The server binds to **0.0.0.0** and uses Waitress, not Flask's debug server. Run only **one process**: the in-memory player owns the speaker. Threads are supported; multiple workers would each have their own playback state.

## Security boundaries

This is a local, single-household prototype. Set a strong portal password before adding credentials. Do not forward port 8080 to the public internet. Plain local HTTP does not encrypt passwords or uploaded tokens in transit; use a trusted LAN and put HTTPS or a secure private network in front of it if appropriate.

Mutating requests require a same-origin CSRF token, no CORS is enabled, foreign DNS hostnames are rejected by default, uploads use generated filenames, file types are inspected, subprocesses do not use a shell, and TLS validation to Life360 is never disabled. These measures do not replace HTTPS or OS/device security.

## Tests

```bash
pip install pytest
python3 -m pytest -q
```

Tests use generated audio and mocked Life360 responses. They do not require your real token or contact your account.

## Project layout

```text
app.py                 Flask routes, authentication, API, server entry point
portal/storage.py      Atomic JSON persistence and credential file handling
portal/life360.py      Read-only, token-based connection validation
portal/audio.py        Audio metadata, waveform, demo sound, ffplay playback
static/app.js          Portal controls and browser playback
static/app.css         Responsive interface
static/favicon.svg     Local icon; no external assets or fonts
templates/             Portal and sign-in pages
tests/                 Backend tests
run.sh                 Raspberry Pi/Linux launcher
```
