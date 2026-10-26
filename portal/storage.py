"""Small, atomic, file-backed storage for a single local portal process."""
import copy
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
from datetime import datetime, timezone

from .arrivals import default_arrivals


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audio_dir = self.root / "audio"
        self.audio_dir.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self._state = self._read("state.json", {
            "tracks": [], "selected_id": None, "clips": {}, "volume": 75,
            "life360": {"status": "disconnected", "account_name": None,
                        "checked_at": None, "saved_at": None, "message": None},
            "events": [],
        })
        self._state.setdefault("revision", 0)
        for clip in self._state.get("clips", {}).values():
            clip.setdefault("fade_in", 0)
            clip.setdefault("fade_out", 0)
        arrivals = self._state.setdefault("arrivals", default_arrivals())
        for key, value in default_arrivals().items():
            arrivals.setdefault(key, value)
        for key, value in default_arrivals()["directory"].items():
            arrivals["directory"].setdefault(key, value)
        self._credentials = self._read("credentials.json", {})
        key_path = self.root / "session.key"
        if key_path.exists():
            self.session_key = key_path.read_text().strip()
        else:
            self.session_key = secrets.token_hex(32)
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(self.session_key)

    def _read(self, name, default):
        path = self.root / name
        if not path.exists():
            return copy.deepcopy(default)
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read {name}. Restore it from a backup before restarting.") from exc

    def _write(self, name, data):
        fd, temporary = tempfile.mkstemp(prefix=".threshold-", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.root / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self._state)

    def mutate(self, function):
        with self.lock:
            result = function(self._state)
            self._state["revision"] = self._state.get("revision", 0) + 1
            self._write("state.json", self._state)
            return result

    def event(self, kind, title, detail=None):
        def apply(state):
            state["events"].insert(0, {"id": secrets.token_hex(8), "kind": kind,
                "title": title, "detail": detail, "at": now_iso()})
            state["events"] = state["events"][:60]
        self.mutate(apply)

    def token(self):
        with self.lock:
            return self._credentials.get("life360_token", "")

    def set_token(self, token):
        with self.lock:
            self._credentials = {"life360_token": token} if token else {}
            self._write("credentials.json", self._credentials)

    def track(self, track_id):
        with self.lock:
            return copy.deepcopy(next((track for track in self._state["tracks"]
                                       if track["id"] == track_id), None))

    def track_path(self, track):
        path = (self.audio_dir / track["stored_name"]).resolve()
        if path.parent != self.audio_dir:
            raise ValueError("Invalid audio path.")
        return path
