# Threshold · 0.3.0

A small, self-hosted control room for your homecoming soundtrack. **One Raspberry Pi, a wired powered speaker, and a browser for setup.** No ESP32, Home Assistant, database server, or cloud audio service required.

Threshold now plays a person's assigned audio when their Life360 location changes from away to within your home's proximity. This is a **proximity trigger, not proof of someone entering the building**. A door sensor, camera recording, Spotify playback, and automatic token renewal are not implemented.

## What works

- Upload MP3, WAV, OGG, M4A, FLAC, AAC, or OPUS audio (50 MB/file; 30 minutes maximum).
- Search, sort, select, and delete audio. **New uploads immediately become selected** in the library and player; a hiding search is cleared. Uploads do not auto-play.
- Choose start time, duration, and optional fade-in/out, with a waveform and selection handles.
- Play manually through **Server speaker** or **This browser**; set playback volume. Per-track manual selections and fades persist.
- Import people from **every Life360 Circle visible to your account**. A person belonging to multiple Circles appears once. Filter people by Circle or search by name.
- Choose home from **saved Life360 places only**, from any of those Circles. Set a separate Threshold proximity radius without changing the place in Life360. The Pi needs no GPS hardware.
- Assign each person their own uploaded song/segment and fades. **Arrival assignments are capped at 30 seconds; manual/library playback is not.**
- Monitor and play automatically on the Pi **with no browser open**. Simultaneous welcomes are queued rather than mixed.
- Suppress first-fix/startup welcomes, inaccurate or stale fixes, GPS-boundary chatter, and rapid repeat greetings.
- Verify a replacement Life360 token before saving. A failed replacement preserves the current setup.
- Persist uploads, assignments, saved home, radius, volume, cooldown timestamps, and recent activity across restarts.

A generated original 12-second chime is included on first launch. A portal password is required for Life360 features and for serving previously saved Life360 data.

## Raspberry Pi setup

Use Raspberry Pi OS with Python 3.9 or newer. A Pi 3/4/5 or Zero 2 W is a reasonable starting point. Models without a headphone output can use a USB sound adapter. Use a **powered speaker**, not a passive speaker connected to GPIO.

```bash
sudo apt update
sudo apt install -y python3-venv ffmpeg
cd threshold
chmod +x run.sh
./run.sh
```

The launcher creates a virtual environment, installs the three Python dependencies if needed, and prompts for a **portal password** without displaying it. This is not your Life360 password. It stays in the server process environment and is not written into the project.

Open **http://raspberrypi.local:8080** or **http://YOUR_PI_IP:8080** and sign in. Supply the password again when restarting, or intentionally choose a new one; existing audio/settings are preserved.

### Manual launch

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
read -r -s -p 'Portal password: ' PORTAL_PASSWORD; echo
export PORTAL_PASSWORD
python3 app.py
```

For an **audio-only preview**, `python3 app.py` without `PORTAL_PASSWORD` also works. Life360 setup is unavailable until you restart with a password. If credentials or cached Life360 people/places/home/assignments already exist, a password is required to start the server at all.

Keep the server running for arrival detection. The launcher runs in the foreground; closing its terminal stops monitoring. If you configure a boot service, run exactly one process as the same OS user with working speaker access and the appropriate environment. Do not publish the portal to the public internet.

## Set up personal welcomes

1. **Check the speaker first.** Configure the powered speaker as the Pi's default output. In the same OS user/session that will run Threshold, run `ffplay -nodisp -autoexit /path/to/test.wav` and confirm you hear it. Install `ffmpeg` if `ffplay` is missing.
2. **Connect Life360.** Save and test your token in the Life360 card, as described below. Successful verification schedules a background import. **Sync Circles** refreshes it later.
3. In **Personal welcomes**, choose **Set up**, select your saved home place, choose an independent proximity radius, and save. The default is 100 m; the allowed range is 25–2,000 m. Add or correct the place in Life360 itself if needed, then sync again. There is no manual map pin or coordinate field.
4. Upload/select audio and set **Start at**, **Play for**, and the optional fades in the existing player.
5. Click **Use selected** beside a person. This saves a separate snapshot of the clip for that person. If the selected manual segment is longer than 30 seconds, only its first 30 seconds are assigned; the manual selection is kept unchanged. Fades are scaled to fit when necessary.
6. Repeat for anyone else, across any of your Circles. A person's Circle need not be the same as the Circle containing the home place. The small play button beside a person **tests on the Pi speaker**, even if the main player is set to browser output. Testing does not simulate an arrival or consume their arrival cooldown.
7. Reopen home settings, enable **Play welcome clips automatically**, and save. This requires at least one assigned, visible person, a saved place, a valid connection, a completed initial sync, and `ffplay`.

Click an assigned clip's name to load its snapshot into the player. Edit and click **Use selected** again to update that person's assignment. Later edits to a manual clip do not silently change everyone's assignments. Unassigning a person keeps the audio file. Deleting an audio file removes assignments using it; removing the last assignment turns arrivals off.

Arrivals always use the **Pi's speaker**, never an unattended browser. The main output selector controls manual playback only. Manual Play takes priority and clears any currently queued welcomes. Browser previews hold newly detected welcomes until the preview finishes; Stop clears the queue too. Disable the arrival toggle to pause detection without deleting your setup.

## How proximity detection behaves

- The server polls member locations about every **60 seconds**, across all accessible Circles. It deduplicates by member ID, using the newest available fix. The directory is refreshed roughly hourly while enabled or on demand with Sync Circles. Calls are paced; rate limits and network failures can make intervals much longer.
- Only location information your account can access is usable. A member whose sharing is off, whose phone is offline, or whose GPS is too broad/out-of-date will not trigger. Threshold reads existing locations; it does not request forced phone updates or write to Life360.
- A fix must have coordinates, a source-fix timestamp, and positive reported accuracy. Fixes older than **3 minutes**, more than **1 minute in the future**, or less precise than the smaller of **100 m or your selected radius** are ignored. Duplicate/out-of-order fixes cannot trigger a second welcome.
- A person counts as inside only when their reported uncertainty fits inside the proximity radius: `distance + accuracy <= radius`. They re-arm only after a clear departure beyond `radius + max(50 m, half the radius)`, also allowing for uncertainty. With a 100 m proximity radius, the re-arm boundary is 150 m before allowing for GPS error. A smaller radius is not necessarily more reliable.
- **The first trusted fix establishes a baseline and never greets.** This applies after a restart, enabling/changing home settings, or replacing a token. Someone already home should not receive a startup greeting; they need a fresh observed departure and return.
- There is a persisted **15-minute cooldown per person**. Up to 50 arrivals can wait in sequence, with a 10-minute queue expiry. Missing/departed people, GPS fixes that become unusable while queued, removed/changed assignments, and invalid files are skipped instead of playing an obsolete welcome.
- Playback uses the person's saved segment/fades and the portal's current volume. It does not wait for a door to open. Life360's own update delays plus polling mean it is not a second-precise doorway detector.

Keep participating Circle members informed and use their location information with consent. If the unofficial API or a saved-place response differs on your account, the portal reports the failure rather than guessing a home location. Verify your imported places and run a speaker test before enabling unattended playback.

## Choosing clips, fades, and manual output

**Fade in / Fade out** are measured in seconds; **0 turns that fade off**. Start at 20, Play for 12, Fade in 1, Fade out 2 starts at file position 20 seconds, fades up during the first second, and fades down during the final two seconds. The original file is not changed.

Manual selections auto-save per track and can exceed 30 seconds, up to the available audio. Their combined fades fit within the selected segment; shortening a clip scales fades if necessary. Editing a playing clip stops it; press Play again to hear the change. Existing tracks start with both fades off.

**Server speaker** uses real `ffplay` playback on the machine running Threshold. **This browser** is a separate HTML-audio preview and stops when the page closes. There is no silent fallback between outputs. Browser volume changes live; server volume applies at the start of the next clip. Only one server clip plays at a time.

A hosted preview has no physical Pi speaker, so browser playback is the default there. Without `ffmpeg`, some files can still be inspected with Mutagen and previewed in a compatible browser, but automatic Pi playback requires `ffplay`. Linux sound permissions and PipeWire/ALSA sessions matter, particularly for boot services.

## Life360 token management

1. Log in to your own account at https://life360.com/login.
2. In browser developer tools, use Application → Cookies → the Life360 entry to copy `LIFE360_AUTH_TOKEN` from your own account.
3. Paste it into **Life360 access token** and choose **Save & test**. A `Bearer ` prefix is accepted.
4. The server verifies `/v3/users/me` over HTTPS, with certificate validation enabled and redirects refused. The replacement is saved only after a successful account-profile response.

The transport reuses the earlier working `life360_test.py` approach, with read-only Circle/member/place discovery added. The API is **unofficial and can change**. Saved places use `/v3/circles/{id}/places`; a `.json` alternative is tried only after a 404, never to evade authentication, blocking, or rate limits. No real-account saved-place response is bundled or assumed by the automated tests.

**Threshold does not sign in automatically, extend tokens, or promise a token lifetime.** A 401 pauses monitoring until you manually verify a replacement. A 403 preserves credentials and backs off; a 429 honors Retry-After, including long delays. Profile checks and monitoring share a network lock/cooldown; rapid clicks do not bypass it. When arrivals are off and no import is scheduled, the worker does not continuously poll member locations.

A failed replacement preserves the existing token and setup. A verified replacement for the same recognized account preserves assignments/home but resets the location baseline. A recognized account change clears the previous account's arrival directory, home, and assignments; uploaded audio remains. **Remove token** clears that local arrival data too. Removing the local copy does not revoke the token at Life360.

Raw tokens never appear in HTML, API state, recent events, or application error messages. The token field clears after a successful save.

## Spotify and other future integrations

Arrival playback currently uses **permitted uploaded audio only**. Spotify is not implemented: its Developer Policy restricts alert/ringtone/alarm functionality without written approval, and previews cannot be used for personalization/messaging. A 30-second duration does not remove those restrictions. [1](https://developer.spotify.com/policy)

Catalog discovery is a separate future question, not permission to download, extract, or repurpose Spotify previews. Door-open timing, door/camera hardware, video capture, and token-renewal mechanisms remain separate future work.

## Data, privacy, and configuration

Data defaults to `threshold/data/`:

- `audio/`: uploaded audio with generated filenames.
- `state.json`: track metadata, manual clips/fades, volume, recent events, account display status, Circle/member/place directory, saved home coordinates/radius, assignments, and last-trigger timestamps.
- `credentials.json`: the Life360 token (`0600`). This is protected by filesystem permissions, **not encrypted at rest**.
- `session.key`: persistent random portal-session signing key (`0600`).

Phone coordinates are processed in memory rather than saved as a continuous GPS history. Place centers remain server-side and are not returned in the public browser state. People names, selected place names, assignments, arrival/cooldown timestamps, and recent arrival activity are still personal information: protect both the portal and its data/backups. The server applies restrictive permissions and atomic JSON writes.

Back up the data directory securely; do not commit or share it. The source ZIP excludes all runtime data, credentials, keys, uploads, virtual environments, and caches. To upgrade, stop the old server and replace source files **without deleting `data/` or changing your data path**, then restart one process. Existing manual audio/clips/fades migrate without a new 30-second restriction; arrival monitoring is off until configured.

| Environment variable | Default | Purpose |
|---|---|---|
| `PORTAL_PASSWORD` | unset | Portal sign-in; required for Life360 credentials and saved arrival data. |
| `PORT` | `8080` | HTTP port. |
| `PORTAL_DATA_DIR` | `./data` beside app.py | Alternate persistent data directory. |
| `PORTAL_ALLOWED_HOSTS` | unset | Additional comma-separated hostnames you own. Private/local IPs, local hostnames, `.local`, `.lan`, `.home.arpa`, and `.e2b.app` previews work by default. |
| `PORTAL_SECURE_COOKIE` | `0` | Set to `1` when the portal is behind HTTPS. |

The server binds **0.0.0.0** and uses Waitress with threads, not Flask's debug server. Run exactly **one process**: multiple processes would each own a player, location baseline, and queue and could produce duplicate welcomes.

## Security boundaries

This is a local single-household prototype. Use a strong portal password, protect the OS account, and do not forward port 8080 publicly. Plain LAN HTTP does not encrypt passwords or tokens in transit; use a trusted network and HTTPS or a secure private network where appropriate.

Mutations require a same-origin CSRF token. No CORS is enabled. Foreign DNS hostnames are rejected by default; uploads use generated filenames and inspected audio; subprocesses do not use a shell; Life360 requests use a fixed HTTPS host, verified TLS, bounded reads/timeouts, and no redirects. These controls do not replace HTTPS, OS security, or participant consent.

## Tests

```bash
pip install pytest
python3 -m pytest -q
```

Tests use temporary/generated audio and simulated Life360 accounts; they never need your token or contact a real account. Backend coverage includes all-Circle deduplication, cross-Circle home selection, GPS age/accuracy/baselines, cooldowns, queued playback, manual precedence, arrival-only limits, account changes, persistence, token privacy, and transport/backoff behavior. With ffmpeg/ffplay installed, tests also check real fade filters and playback through SDL's silent dummy output—not an actual powered speaker.

Optional Chromium tests:

```bash
pip install playwright
python3 -m playwright install --with-deps chromium
THRESHOLD_BROWSER_TESTS=1 python3 -m pytest -q
```

These cover uploads, stale responses, saved per-track settings, actual Web Audio fades, volume, people filters, saved-place setup, per-person assignments, the 30-second arrival-only cap, browser-closed background playback using a simulated speaker/account, and mobile layouts. Their temporary server/data never touches your working `data/`. Playwright is a test-only dependency. Automated tests do not verify the current real Life360 place schema or your physical Pi audio output.

## Project layout

```text
app.py                   Flask routes, auth, APIs, worker startup, server entry
portal/arrivals.py       Background discovery, proximity state, assignments, queue
portal/storage.py        Atomic JSON persistence and credentials
portal/life360.py        Read-only token/profile/Circle/member/place transport
portal/audio.py          Audio inspection, waveform, demo chime, ffplay player
portal/clips.py          Shared manual clip/fade validation (no global 30s cap)
static/app.js            Library/player and browser preview coordination
static/arrivals.js       People, assignments, saved-home setup, runtime status
static/audio-envelope.js Browser Web Audio fade envelope
static/app.css           Responsive interface
static/favicon.svg       Local icon; no external fonts or assets
templates/               Portal and sign-in pages
tests/                   Backend and optional Chromium tests
run.sh                   Raspberry Pi/Linux launcher
```
