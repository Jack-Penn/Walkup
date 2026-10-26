#!/usr/bin/env python3
"""Threshold — a local audio and Life360 control portal.

Start with: PORTAL_PASSWORD='choose-a-password' python3 app.py
One process owns audio playback; do not deploy multiple worker processes.
"""
import atexit
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hmac
import ipaddress
import math
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit
import uuid

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from portal.audio import ALLOWED_EXTENSIONS, Player, generate_demo, inspect_audio
from portal.clips import default_clip, clip_values as validate_clip
from portal.life360 import Life360Error, normalize_token, validate_token
from portal.storage import Store, now_iso

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD_MB = 50
APP_VERSION = "0.2.0"


def create_app(test_config=None):
    app = Flask(__name__)
    app.jinja_env.globals["asset_version"] = APP_VERSION
    app.config.update(
        DATA_DIR=os.environ.get("PORTAL_DATA_DIR", str(ROOT / "data")),
        PORTAL_PASSWORD=os.environ.get("PORTAL_PASSWORD", ""),
        SEED_DEMO=True,
        MAX_CONTENT_LENGTH=(MAX_UPLOAD_MB + 2) * 1024 * 1024,  # multipart overhead
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.environ.get("PORTAL_SECURE_COOKIE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    if test_config:
        app.config.update(test_config)
    store = Store(app.config["DATA_DIR"])
    app.secret_key = store.session_key
    password = app.config["PORTAL_PASSWORD"]
    auth_version = hmac.new(app.secret_key.encode(), password.encode(), "sha256").hexdigest()

    def signed_in():
        return session.get("authenticated") and session.get("auth_version") == auth_version

    if store.token() and not password and not app.testing:
        raise RuntimeError("Set PORTAL_PASSWORD before starting a portal with saved Life360 credentials.")
    player = Player(store.event)
    atexit.register(lambda: player.stop(log=False))
    app.extensions.update(store=store, player=player)
    started = time.monotonic()
    check_lock = threading.Lock()
    next_check = [0.0]
    login_attempts = {}
    public_csrf = secrets.token_urlsafe(32)

    if not store.snapshot().get("initialized"):
        if app.config["SEED_DEMO"]:
            identifier = uuid.uuid4().hex
            path = store.audio_dir / (identifier + ".wav")
            generate_demo(path)
            duration, peaks = inspect_audio(path)
            track = {"id": identifier, "title": "Soft landing", "original_name": "Soft landing.wav",
                "stored_name": path.name, "format": "WAV", "bytes": path.stat().st_size,
                "duration": duration, "peaks": peaks, "created_at": now_iso(), "demo": True}
            def seed(state):
                state["tracks"].append(track)
                state["selected_id"] = identifier
                state["clips"][identifier] = default_clip(duration)
            store.mutate(seed)
            store.event("upload", "A soft landing, included", "An original demo chime to try things out.")
        store.mutate(lambda state: state.update(initialized=True))
        store.event("system", "Your control room is ready", "Everything starts with a little sound.")

    def fail(message, status=400, **extra):
        return jsonify(ok=False, error=message, **extra), status

    def csrf():
        # Audio-only previews need no third-party cookies inside an embedded viewer.
        # Password-protected portals use a separate, session-bound nonce.
        if not password:
            return public_csrf
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def allowed_host(host):
        if host in {"localhost", socket.gethostname().lower()}:
            return True
        if host.endswith((".e2b.app", ".local", ".lan", ".home.arpa")) or "." not in host and ":" not in host:
            return True
        custom = {value.strip().lower() for value in os.environ.get("PORTAL_ALLOWED_HOSTS", "").split(",") if value.strip()}
        if host in custom:
            return True
        try:
            address = ipaddress.ip_address(host)
            return address.is_private or address.is_loopback or address.is_link_local
        except ValueError:
            return False

    @app.before_request
    def protect():
        try:
            host = (urlsplit("http://" + request.host).hostname or "").lower()
        except ValueError:
            return fail("Invalid host.", 400)
        if not allowed_host(host):
            return fail("Host not allowed. Add this hostname to PORTAL_ALLOWED_HOSTS if it is yours.", 400)
        if request.path.startswith("/static/") or request.path == "/health":
            return None
        if password and not signed_in() and request.path != "/login":
            if request.path.startswith(("/api/", "/media/")):
                return fail("Sign in to your portal.", 401, code="auth_required")
            return redirect(url_for("login"))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("X-Portal-CSRF") or request.form.get("csrf", "")
            expected = session.get("csrf", "") if password else public_csrf
            if not isinstance(supplied, str) or not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
                return fail("This page's security token expired. Reload and try again.", 403)
            if request.is_json and request.method in {"POST", "PUT", "PATCH"}:
                if not isinstance(request.get_json(silent=True), dict):
                    return fail("Send a JSON object with this request.")
        return None

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.path.startswith("/api/") or request.path in {"/", "/login"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    def public_track(track):
        return {key: value for key, value in track.items() if key != "stored_name"} | {
            "url": url_for("media", track_id=track["id"])}

    def public_state():
        state = store.snapshot()
        connection = state["life360"] | {"configured": bool(store.token())}
        return {
            "tracks": [public_track(track) for track in state["tracks"]],
            "selected_id": state["selected_id"], "clips": state["clips"], "volume": state["volume"],
            "revision": state.get("revision", 0),
            "life360": connection, "events": state["events"][:12],
            "playback": player.state(),
            "system": player.capabilities() | {"hostname": socket.gethostname(),
                "auth_enabled": bool(password), "max_upload_mb": MAX_UPLOAD_MB,
                "uptime_seconds": round(time.monotonic() - started), "version": APP_VERSION},
        }

    def success(message=None, **extra):
        return jsonify(ok=True, state=public_state(), message=message, **extra)

    @app.get("/health")
    def health():
        return jsonify(ok=True, app="Threshold")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password or signed_in():
            return redirect(url_for("index"))
        error = None
        if request.method == "POST":
            key = request.remote_addr or "local"
            attempts = [at for at in login_attempts.get(key, []) if time.monotonic() - at < 900]
            if len(attempts) >= 10:
                error = "Too many attempts. Wait 15 minutes before trying again."
            elif hmac.compare_digest(request.form.get("password", "").encode(), password.encode()):
                session.clear()
                session["authenticated"] = True
                session["auth_version"] = auth_version
                session.permanent = True
                return redirect(url_for("index"))
            else:
                attempts.append(time.monotonic())
                login_attempts[key] = attempts
                error = "That password doesn't match. Give it another try."
        return render_template("login.html", csrf_token=csrf(), error=error)

    @app.post("/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=csrf())

    @app.get("/api/state")
    def state():
        return jsonify(ok=True, state=public_state())

    @app.post("/api/audio")
    def upload():
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            return fail("Choose an audio file to upload.")
        extension = Path(uploaded.filename.replace("\\", "/")).suffix.lower()
        original = secure_filename(uploaded.filename)
        if not Path(original).suffix:
            original = "audio" + extension
        if extension not in ALLOWED_EXTENSIONS:
            return fail("Choose an MP3, WAV, OGG, M4A, FLAC, AAC, or OPUS file.")
        identifier = uuid.uuid4().hex
        path = store.audio_dir / (identifier + extension)
        try:
            uploaded.save(path)
            os.chmod(path, 0o600)
            if path.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                raise ValueError(f"This audio file is too large. The limit is {MAX_UPLOAD_MB} MB.")
            duration, peaks = inspect_audio(path)
        except (ValueError, OSError) as error:
            path.unlink(missing_ok=True)
            message = str(error) if isinstance(error, ValueError) else "The file could not be saved. Check disk space and permissions."
            return fail(message)
        title = Path(uploaded.filename.replace("\\", "/")).stem[:160].strip() or "Untitled audio"
        track = {"id": identifier, "title": title, "original_name": original,
            "stored_name": path.name, "format": extension[1:].upper(), "bytes": path.stat().st_size,
            "duration": duration, "peaks": peaks, "created_at": now_iso(), "demo": False}
        def add(state):
            state["tracks"].insert(0, track)
            state["selected_id"] = identifier
            state["clips"][identifier] = default_clip(duration)
        store.mutate(add)
        store.event("upload", "New sound, new possibilities", title)
        return success("Audio uploaded and selected.", track=public_track(track), selected_track_id=identifier)

    @app.get("/media/<track_id>")
    def media(track_id):
        track = store.track(track_id)
        if not track:
            return fail("This audio file is no longer in the library.", 404)
        path = store.track_path(track)
        if not path.exists():
            return fail("The audio file is missing from the server.", 404)
        response = send_file(path, conditional=True, download_name=track["original_name"])
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    @app.delete("/api/audio/<track_id>")
    def delete_audio(track_id):
        track = store.track(track_id)
        if not track:
            return fail("Audio not found.", 404)
        if player.state()["track_id"] == track_id:
            player.stop(log=False)
        def remove(state):
            state["tracks"] = [item for item in state["tracks"] if item["id"] != track_id]
            state["clips"].pop(track_id, None)
            if state["selected_id"] == track_id:
                state["selected_id"] = state["tracks"][0]["id"] if state["tracks"] else None
        store.mutate(remove)
        store.track_path(track).unlink(missing_ok=True)
        store.event("delete", "Audio removed", track["title"])
        return success("Audio removed from this device.")

    def clip_values(data, track):
        existing = store.snapshot()["clips"].get(track["id"])
        return validate_clip(data, track["duration"], existing)

    @app.post("/api/selection")
    def select():
        data = request.get_json(silent=True) or {}
        track = store.track(data.get("track_id"))
        if not track:
            return fail("Choose an audio file first.", 404)
        if not isinstance(data.get("select", True), bool):
            return fail("The select flag must be true or false.")
        try:
            clip = clip_values(data, track)
        except ValueError as error:
            return fail(str(error))
        def update(state):
            if data.get("select", True):
                state["selected_id"] = track["id"]
            state["clips"][track["id"]] = clip
        store.mutate(update)
        return success()

    @app.post("/api/settings")
    def settings():
        data = request.get_json(silent=True) or {}
        try:
            volume = float(data.get("volume"))
            if not math.isfinite(volume) or not 0 <= volume <= 100:
                raise ValueError
        except (TypeError, ValueError):
            return fail("Volume must be between 0 and 100.")
        store.mutate(lambda state: state.update(volume=round(volume)))
        return success()

    @app.post("/api/play")
    def play():
        data = request.get_json(silent=True) or {}
        track = store.track(data.get("track_id"))
        if not track:
            return fail("Choose an audio file first.", 404)
        output = data.get("output", "server")
        if output not in {"server", "browser"}:
            return fail("Choose the server speaker or this browser.")
        try:
            clip = clip_values(data, track)
            volume = store.snapshot()["volume"]
            if output == "server":
                player.play(store.track_path(track), track, clip["start"], clip["duration"], volume,
                            fade_in=clip["fade_in"], fade_out=clip["fade_out"])
            else:
                player.stop(log=False)
                store.event("play", "Playing in this browser", track["title"])
        except ValueError as error:
            return fail(str(error))
        def update(state):
            state["selected_id"] = track["id"]
            state["clips"][track["id"]] = clip
        store.mutate(update)
        return success(output=output, url=url_for("media", track_id=track["id"]), **clip)

    @app.post("/api/stop")
    def stop():
        player.stop()
        return success("Playback stopped.")

    def perform_check(token, replacing=False):
        if not password:
            return fail("Protect this portal before adding credentials. Set PORTAL_PASSWORD and restart the server; see the setup guide.", 403, code="password_required")
        if time.monotonic() < next_check[0]:
            seconds = max(1, math.ceil(next_check[0] - time.monotonic()))
            return fail(f"Wait {seconds} seconds before testing Life360 again.", 429, retry_after=seconds)
        if not check_lock.acquire(blocking=False):
            return fail("A connection test is already running.", 409)
        try:
            next_check[0] = time.monotonic() + 10
            profile = validate_token(token)
            checked = now_iso()
            if replacing:
                store.set_token(token)
            def update(state):
                state["life360"].update(status="connected", account_name=profile["account_name"], checked_at=checked, message=None)
                if replacing:
                    state["life360"]["saved_at"] = checked
            store.mutate(update)
            store.event("connection", "Life360 is connected", "Token replaced and verified." if replacing else "Your saved token is still working.")
            return success("Token verified and saved." if replacing else "Your Life360 connection is working.")
        except Life360Error as error:
            if error.status == "rate_limited":
                try:
                    delay = float(error.retry_after or 60)
                except (ValueError, TypeError):
                    try:
                        retry_at = parsedate_to_datetime(error.retry_after)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        delay = 60
                if not math.isfinite(delay):
                    delay = 60
                next_check[0] = time.monotonic() + max(60, delay)
            elif error.status == "blocked":
                next_check[0] = time.monotonic() + 60
            if not replacing:
                store.mutate(lambda state: state["life360"].update(status=error.status,
                    checked_at=now_iso(), message=str(error)))
            store.event("error", "Life360 connection needs attention", str(error))
            return fail(str(error), 502, code="life360_error", upstream_status=error.http_status, state=public_state())
        finally:
            check_lock.release()

    @app.post("/api/life360/token")
    def replace_token():
        data = request.get_json(silent=True) or {}
        try:
            token = normalize_token(data.get("token"))
        except ValueError as error:
            return fail(str(error))
        return perform_check(token, replacing=True)

    @app.post("/api/life360/test")
    def test_token():
        token = store.token()
        if not token:
            return fail("Add a Life360 token first.")
        return perform_check(token)

    @app.delete("/api/life360/token")
    def forget_token():
        store.set_token("")
        def update(state):
            state["life360"] = {"status": "disconnected", "account_name": None,
                "checked_at": None, "saved_at": None, "message": None}
        store.mutate(update)
        store.event("connection", "Life360 disconnected", "The token was removed from this device, not revoked at Life360.")
        return success("Saved token removed from this device.")

    @app.errorhandler(413)
    def too_large(error):
        return fail(f"This upload is too large. The limit is {MAX_UPLOAD_MB} MB.", 413)

    @app.errorhandler(Exception)
    def handle_error(error):
        if isinstance(error, HTTPException):
            return fail(error.description, error.code)
        app.logger.exception("Portal request failed")
        return fail("Something went wrong on the server. Check its terminal and try again.", 500)

    return app


if __name__ == "__main__":
    from waitress import serve
    app = create_app()
    port = int(os.environ.get("PORT", "8080"))
    protected = bool(app.config["PORTAL_PASSWORD"])
    print(f"\nThreshold is ready on http://0.0.0.0:{port}", flush=True)
    print("Portal password: enabled" if protected else
          "Preview mode: audio is available. Set PORTAL_PASSWORD and restart before saving Life360 credentials.", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=6, channel_timeout=65, ident="Threshold")
