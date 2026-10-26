"""Person-specific proximity arrivals, owned by the single portal process.

Only saved Life360 Places can supply the house coordinates. Phone coordinates
are processed in memory, not stored as a location history or returned by the API.
A fresh outside -> inside transition queues a <=30s local clip on the Pi speaker.
"""
import copy
from collections import deque
from datetime import datetime, timezone
import math
import threading
import time

from .clips import clip_values
from .life360 import Life360Client, Life360Error, identifier, retry_after_seconds

ARRIVAL_MAX_SECONDS = 30
POLL_SECONDS = 60
DIRECTORY_SECONDS = 3600
MAX_FIX_AGE_SECONDS = 180
MAX_ACCURACY_METERS = 100
COOLDOWN_SECONDS = 900
QUEUE_LIFETIME_SECONDS = 600


def iso_time(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat()


def default_arrivals():
    return {"enabled": False, "home": None, "radius_m": 100, "assignments": {},
            "last_triggered": {}, "directory": {"circles": [], "people": [], "places": [], "synced_at": None}}


def finite_number(value):
    if isinstance(value, bool):
        raise ValueError("Expected a number.")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Expected a finite number.")
    return value


def distance_meters(latitude, longitude, other_latitude, other_longitude):
    lat, other_lat = math.radians(latitude), math.radians(other_latitude)
    delta_lat = other_lat - lat
    delta_lon = math.radians(other_longitude - longitude)
    arc = math.sin(delta_lat / 2) ** 2 + math.cos(lat) * math.cos(other_lat) * math.sin(delta_lon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(min(1, max(0, arc))))


def location_sample(member):
    """Normalize the API's GPS fix; accuracy is reported in feet, not meters."""
    features = member.get("features")
    if isinstance(features, dict) and features.get("shareLocation") in (False, 0, "0"):
        return None
    location = member.get("location")
    if not isinstance(location, dict):
        return None
    try:
        latitude = finite_number(location.get("latitude"))
        longitude = finite_number(location.get("longitude"))
        accuracy = finite_number(location.get("accuracy")) * .3048
        timestamp = finite_number(location.get("timestamp"))
        if timestamp > 100_000_000_000:  # Accommodate milliseconds, never use `since` as fix time.
            timestamp /= 1000
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or accuracy <= 0 or timestamp <= 0:
            return None
        return {"latitude": latitude, "longitude": longitude, "accuracy_m": accuracy, "timestamp": timestamp}
    except (TypeError, ValueError, OverflowError):
        return None


def member_name(member):
    name = " ".join(str(member.get(key) or "") for key in ("firstName", "lastName")).strip()
    return name[:160] or "Life360 member"


def arrival_clip(track, data, existing=None):
    if data.get("source", "upload") != "upload":
        raise ValueError("Arrival playback currently supports uploaded audio, not streaming services.")
    clip = clip_values(data, track["duration"], existing)
    if clip["duration"] > ARRIVAL_MAX_SECONDS:
        raise ValueError("Assigned arrival clips must be 30 seconds or shorter. Manual playback is unchanged.")
    return {"source": "upload", "track_id": track["id"], **clip}


class WorkerStopped(Exception):
    pass


class ArrivalEngine:
    def __init__(self, store, player, *, auth_enabled, network_lock=None, not_before=None,
                 client_factory=Life360Client, clock=time.time, monotonic=time.monotonic):
        self.store = store
        self.player = player
        self.auth_enabled = auth_enabled
        self.network_lock = network_lock if network_lock is not None else threading.Lock()
        self.not_before = not_before if not_before is not None else [0.0]
        self.client_factory = client_factory
        self.clock = clock
        self.monotonic = monotonic
        self.lock = threading.RLock()
        self.audio_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.threads = []
        self.observations = {}
        self.queue = deque()
        self.active = None
        self.manual_until = 0
        self.manual_id = None
        self.sync_pending = bool(store.snapshot()["arrivals"]["enabled"] and store.token())
        self.syncing = False
        self.needs_sync = self.sync_pending
        self.next_poll = 0
        self.next_directory = self.monotonic() + DIRECTORY_SECONDS
        self.last_poll_at = None
        self.last_error = None
        self.speaker_error = None
        self.failures = 0
        self.epoch = 0
        self._last_request_at = -1

    def start(self):
        if not self.auth_enabled or self.threads:
            return
        self.stop_event.clear()
        for target, name in ((self._network_loop, "Threshold locations"), (self._playback_loop, "Threshold arrivals")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.stop_event.set()
        self.cancel_auto()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)

    def request_sync(self):
        with self.lock:
            self.sync_pending = True

    def reset_for_token(self, different_account=False):
        """Call only after successful verification. Failed replacements change nothing."""
        with self.audio_lock, self.lock:
            self.cancel_auto()
            self.epoch += 1
            self.observations.clear()
            self.last_error = None
            self.speaker_error = None
            self.failures = 0
            self.needs_sync = True
            self.sync_pending = True
            if different_account:
                self.store.mutate(lambda state: state.update(arrivals=default_arrivals()))

    def disconnect(self):
        with self.audio_lock, self.lock:
            self.cancel_auto()
            self.epoch += 1
            self.observations.clear()
            self.sync_pending = False
            self.needs_sync = False
            self.last_error = None
            self.speaker_error = None
            self.store.mutate(lambda state: state.update(arrivals=default_arrivals()))

    def cancel_auto(self, member_id=None):
        with self.audio_lock, self.lock:
            self.queue = deque(item for item in self.queue if member_id is not None and item["member_id"] != member_id)
            if self.active and (member_id is None or self.active["member_id"] == member_id):
                self.player.stop(log=False)
                self.active = None

    def manual_override(self, output=None, duration=0, preview_id=None):
        with self.audio_lock, self.lock:
            self.cancel_auto()
            self.manual_until = self.monotonic() + duration + 1 if output == "browser" else 0
            self.manual_id = preview_id if output == "browser" else None

    def release_manual_preview(self, preview_id=None):
        with self.lock:
            if self.manual_id is not None and self.manual_id != preview_id:
                return False
            self.manual_until = 0
            self.manual_id = None
            return True

    def configure(self, data):
        with self.audio_lock, self.lock:
            return self._configure_locked(data)

    def _configure_locked(self, data):
        if set(data) - {"enabled", "home_key", "radius_m"}:
            raise ValueError("Choose a saved Life360 place; manual GPS coordinates are not supported.")
        state = self.store.snapshot()
        previous = state["arrivals"]
        enabled = data.get("enabled", previous["enabled"])
        if not isinstance(enabled, bool):
            raise ValueError("Arrival playback must be enabled or disabled explicitly.")
        try:
            radius = finite_number(data.get("radius_m", previous["radius_m"]))
            if not 25 <= radius <= 2000:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Choose a proximity radius between 25 and 2,000 meters.") from None
        key = data.get("home_key", (previous["home"] or {}).get("key"))
        home = next((place for place in previous["directory"]["places"] if place["key"] == key), None)
        if key and not home:
            raise ValueError("That Life360 place is no longer available. Sync your Circles and choose a saved place.")
        if enabled:
            if state["life360"]["status"] == "reauth_required":
                raise ValueError("Replace the rejected Life360 token before enabling arrivals.")
            if not self.auth_enabled or not self.store.token():
                raise ValueError("Protect the portal and connect Life360 before enabling arrivals.")
            if not home:
                raise ValueError("Choose a saved Life360 place for this house first.")
            visible = {person["id"] for person in previous["directory"]["people"]}
            if not any(member in visible and self.store.track(clip["track_id"]) for member, clip in previous["assignments"].items()):
                raise ValueError("Assign a welcome clip to at least one person first.")
            if not self.player.capabilities()["server_available"]:
                raise ValueError("Automatic arrivals need ffplay and the Pi speaker. Install ffmpeg first.")
            if self.needs_sync:
                raise ValueError("Wait for your Circles to finish syncing before enabling arrivals.")
        with self.audio_lock, self.lock:
            changed = previous["home"] != home or previous["radius_m"] != round(radius) or previous["enabled"] != enabled
            def update(state):
                state["arrivals"].update(enabled=enabled, home=copy.deepcopy(home), radius_m=round(radius))
            self.store.mutate(update)
            if changed:
                self.cancel_auto()
                self.epoch += 1
                self.observations.clear()  # Never greet everyone already home after a setup change.
                self.next_poll = 0
            self.speaker_error = None

    def assign(self, member_id, data):
        state = self.store.snapshot()
        if not any(person["id"] == member_id for person in state["arrivals"]["directory"]["people"]):
            raise ValueError("Choose a person from one of your synced Life360 Circles.")
        track = self.store.track(data.get("track_id"))
        if not track:
            raise ValueError("Choose an uploaded track from your library.")
        clip = arrival_clip(track, data, state["clips"].get(track["id"]))
        with self.audio_lock, self.lock:
            self.cancel_auto(member_id)
            self.store.mutate(lambda state: state["arrivals"]["assignments"].update({member_id: clip}))
        return clip

    def unassign(self, member_id):
        with self.audio_lock, self.lock:
            self.cancel_auto(member_id)
            def update(state):
                config = state["arrivals"]
                config["assignments"].pop(member_id, None)
                if not config["assignments"]:
                    config["enabled"] = False
            self.store.mutate(update)

    def remove_track(self, track_id):
        state = self.store.snapshot()
        for member_id, clip in state["arrivals"]["assignments"].items():
            if clip["track_id"] == track_id:
                self.unassign(member_id)

    def public_state(self, snapshot=None):
        state = snapshot if snapshot is not None else self.store.snapshot()
        config = state["arrivals"]
        directory = config["directory"]
        with self.lock:
            observations = {}
            for member_id, observation in self.observations.items():
                status = observation.get("display", "waiting")
                timestamp = observation.get("timestamp")
                if timestamp and self.clock() - timestamp > MAX_FIX_AGE_SECONDS:
                    status = "stale"
                observations[member_id] = {"status": status, "updated_at": iso_time(timestamp) if timestamp else None}
            if not self.store.token():
                phase = "not_connected"
            elif state["life360"]["status"] == "reauth_required":
                phase = "reauth_required"
            elif self.syncing:
                phase = "syncing"
            elif self.last_error and self.monotonic() < max(self.not_before[0], self.next_poll):
                phase = "backoff"
            elif self.sync_pending or self.needs_sync:
                phase = "sync_pending"
            elif not config["enabled"]:
                phase = "disabled"
            else:
                phase = "tracking"
            runtime = {"phase": phase, "syncing": self.syncing, "sync_pending": self.sync_pending,
                "last_poll_at": self.last_poll_at, "error": self.last_error, "speaker_error": self.speaker_error,
                "retry_after": max(0, math.ceil(max(self.not_before[0], self.next_poll) - self.monotonic())) if phase == "backoff" else 0,
                "queue_count": len(self.queue), "active": copy.deepcopy(self.active),
                "manual_active": self.monotonic() < self.manual_until,
                "worker_running": any(thread.is_alive() for thread in self.threads)}
        # Saved place centers stay server-side; never expose members' raw GPS fixes.
        public_place = lambda place: {key: place[key] for key in ("key", "id", "circle_id", "name", "circle_name")}
        return {"enabled": config["enabled"], "home": public_place(config["home"]) if config["home"] else None,
            "radius_m": config["radius_m"], "exit_radius_m": config["radius_m"] + max(50, config["radius_m"] * .5),
            "assignments": copy.deepcopy(config["assignments"]), "circles": copy.deepcopy(directory["circles"]),
            "people": copy.deepcopy(directory["people"]), "places": [public_place(place) for place in directory["places"]],
            "synced_at": directory["synced_at"], "observations": observations, "runtime": runtime,
            "max_clip_seconds": ARRIVAL_MAX_SECONDS, "poll_seconds": POLL_SECONDS,
            "max_fix_age_seconds": MAX_FIX_AGE_SECONDS, "max_accuracy_m": MAX_ACCURACY_METERS,
            "cooldown_seconds": COOLDOWN_SECONDS}

    def _network_loop(self):
        while not self.stop_event.is_set():
            self.run_once()
            self.stop_event.wait(.75)

    def _playback_loop(self):
        while not self.stop_event.is_set():
            try:
                self.drain_once()
            except Exception:
                # An unexpected local-player failure must not kill monitoring or expose data.
                with self.lock:
                    self.speaker_error = "Arrival playback could not continue. Check the speaker and restart the portal if needed."
            self.stop_event.wait(.25)

    def _call(self, function, *args):
        delay = .5 - (self.monotonic() - self._last_request_at)
        if delay > 0 and self.stop_event.wait(delay):
            raise WorkerStopped
        if self.stop_event.is_set():
            raise WorkerStopped
        try:
            return function(*args)
        finally:
            self._last_request_at = self.monotonic()

    def run_once(self):
        """One due discovery/poll cycle; injectable clocks/client make it testable."""
        state = self.store.snapshot()
        token = self.store.token()
        if not self.auth_enabled or not token or self.stop_event.is_set() or state["life360"]["status"] == "reauth_required":
            return False
        with self.lock:
            sync = self.sync_pending or (state["arrivals"]["enabled"] and self.monotonic() >= self.next_directory)
            if not sync and (not state["arrivals"]["enabled"] or self.monotonic() < self.next_poll):
                return False
            if self.monotonic() < self.not_before[0]:
                return False
        if not self.network_lock.acquire(blocking=False):
            return False
        try:
            if sync:
                with self.lock:
                    self.sync_pending = False
                    self.syncing = True
                self._synchronize(token)
            else:
                self._poll(token)
            if token == self.store.token() and not self.stop_event.is_set():
                with self.lock:
                    self.failures = 0
                    self.last_error = None
                    self.not_before[0] = max(self.not_before[0], self.monotonic() + .5)
        except WorkerStopped:
            return False
        except Life360Error as error:
            if token == self.store.token() and not self.stop_event.is_set():
                self._network_error(error, sync)
        except Exception:
            if token == self.store.token() and not self.stop_event.is_set():
                self._network_error(Life360Error("Could not process Life360's response. Settings were kept; the API may have changed."), sync)
        finally:
            with self.lock:
                self.syncing = False
            self.network_lock.release()
        return True

    def _network_error(self, error, syncing):
        with self.audio_lock, self.lock:
            if error.status == "reauth_required":
                self.cancel_auto()
                self.observations.clear()
                self.needs_sync = True
            self.failures += 1
            delay = min(900, POLL_SECONDS * 2 ** min(4, self.failures - 1))
            if error.status == "rate_limited":
                delay = max(delay, retry_after_seconds(error.retry_after, self.clock()))
            self.not_before[0] = max(self.not_before[0], self.monotonic() + delay)
            self.next_poll = self.not_before[0]
            self.sync_pending = syncing and error.status != "reauth_required"
            changed = self.last_error != str(error)
            self.last_error = str(error)
            def update(state):
                state["life360"].update(status=error.status, checked_at=iso_time(self.clock()), message=str(error))
            self.store.mutate(update)
            if changed:
                self.store.event("error", "Arrival monitoring paused", str(error))

    def _synchronize(self, token):
        client = self.client_factory(token)
        circles = []
        people = {}
        places = {}
        samples = {}
        for raw in self._call(client.circles):
            try:
                circle_id = identifier(raw.get("id"))
            except ValueError:
                continue
            if any(circle["id"] == circle_id for circle in circles):
                continue
            circle = {"id": circle_id, "name": str(raw.get("name") or "Life360 Circle")[:160]}
            circles.append(circle)
            members = self._call(client.members, circle_id)
            self._collect_members(members, circle, people, samples)
            for raw_place in self._call(client.places, circle_id):
                try:
                    place_id = identifier(raw_place.get("id"))
                    latitude = finite_number(raw_place.get("latitude"))
                    longitude = finite_number(raw_place.get("longitude"))
                    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    continue
                key = f"{circle_id}:{place_id}"
                places[key] = {"key": key, "id": place_id, "circle_id": circle_id, "circle_name": circle["name"],
                    "name": str(raw_place.get("name") or "Saved place")[:160], "latitude": latitude, "longitude": longitude}
        directory = {"circles": circles, "people": sorted(people.values(), key=lambda person: person["name"].casefold()),
            "places": sorted(places.values(), key=lambda place: (place["circle_name"].casefold(), place["name"].casefold())),
            "synced_at": iso_time(self.clock())}
        with self.audio_lock, self.lock:
            if token != self.store.token() or self.stop_event.is_set():
                return
            previous = self.store.snapshot()["arrivals"]
            old_home = previous["home"]
            home = places.get(old_home["key"]) if old_home else None
            home_changed = home != old_home
            def update(state):
                state["arrivals"]["directory"] = directory
                state["arrivals"]["home"] = home
                if not home:
                    state["arrivals"]["enabled"] = False
                state["life360"].update(status="connected", message=None, checked_at=iso_time(self.clock()))
            self.store.mutate(update)
            self.needs_sync = False
            self.next_directory = self.monotonic() + DIRECTORY_SECONDS
            self.next_poll = self.monotonic() + POLL_SECONDS
            self.last_poll_at = iso_time(self.clock())
            if home_changed:
                self.cancel_auto()
                self.epoch += 1
                self.observations.clear()
            active_ids = set(people)
            self.observations = {key: value for key, value in self.observations.items() if key in active_ids}
            self.queue = deque(item for item in self.queue if item["member_id"] in active_ids)
            if self.active and self.active["member_id"] not in active_ids:
                self.cancel_auto(self.active["member_id"])
            epoch = self.epoch
        self.store.event("connection", "Your Circles are in sync", f"{len(circles)} Circles · {len(people)} people · {len(places)} saved places")
        if old_home and not home:
            self.store.event("system", "Choose a new home place", "The saved Life360 place is no longer available. Arrival playback was switched off.")
        self.process_samples(samples, epoch=epoch, token=token)

    @staticmethod
    def _collect_members(members, circle, people, samples):
        for member in members:
            try:
                member_id = identifier(member.get("id"))
            except ValueError:
                continue
            if member_id not in people:
                people[member_id] = {"id": member_id, "name": member_name(member), "circle_ids": [], "circle_names": []}
            person = people[member_id]
            if circle["id"] not in person["circle_ids"]:
                person["circle_ids"].append(circle["id"])
                person["circle_names"].append(circle["name"])
            sample = location_sample(member)
            previous = samples.get(member_id)
            if sample and (not previous or sample["timestamp"] > previous["timestamp"]):
                samples[member_id] = sample
            else:
                samples.setdefault(member_id, None)

    def _poll(self, token):
        state = self.store.snapshot()
        config = state["arrivals"]
        with self.lock:
            epoch = self.epoch
        if not config["home"] or self.needs_sync:
            self.request_sync()
            return
        client = self.client_factory(token)
        people, samples = {}, {}
        for circle in config["directory"]["circles"]:
            members = self._call(client.members, circle["id"])
            self._collect_members(members, circle, people, samples)
        with self.audio_lock, self.lock:
            if token != self.store.token() or epoch != self.epoch or self.stop_event.is_set():
                return
            new_people = sorted(people.values(), key=lambda person: person["name"].casefold())
            old_people = self.store.snapshot()["arrivals"]["directory"]["people"]
            if new_people != old_people:
                self.store.mutate(lambda state: state["arrivals"]["directory"].update(people=new_people))
                visible = set(people)
                self.queue = deque(item for item in self.queue if item["member_id"] in visible)
                if self.active and self.active["member_id"] not in visible:
                    self.cancel_auto(self.active["member_id"])
            self.next_poll = self.monotonic() + POLL_SECONDS
            self.last_poll_at = iso_time(self.clock())
            if state["life360"]["status"] != "connected":
                self.store.mutate(lambda state: state["life360"].update(status="connected", message=None))
        self.process_samples(samples, epoch=epoch, token=token)

    def process_samples(self, samples, *, epoch=None, token=None):
        """Consume normalized, deduplicated samples; no triggering on the initial fix."""
        state = self.store.snapshot()
        config = state["arrivals"]
        if not config["enabled"] or not config["home"] or not self.auth_enabled or not self.store.token():
            return
        now = self.clock()
        home = config["home"]
        radius = config["radius_m"]
        exit_radius = radius + max(50, radius * .5)
        with self.lock:
            if (epoch is not None and epoch != self.epoch) or (token is not None and token != self.store.token()):
                return
            for person in config["directory"]["people"]:
                member_id = person["id"]
                sample = samples.get(member_id)
                observation = self.observations.setdefault(member_id, {"stable": None, "armed": False, "timestamp": None})
                if not sample:
                    observation["display"] = "unavailable"
                    continue
                timestamp = sample["timestamp"]
                if now - timestamp > MAX_FIX_AGE_SECONDS or timestamp > now + 60:
                    observation["display"] = "stale"
                    continue
                if sample["accuracy_m"] > min(MAX_ACCURACY_METERS, radius):
                    observation["display"] = "poor_accuracy"
                    continue
                distance = distance_meters(home["latitude"], home["longitude"], sample["latitude"], sample["longitude"])
                inside = distance + sample["accuracy_m"] <= radius
                outside = distance - sample["accuracy_m"] >= exit_radius
                if observation["timestamp"] is not None and timestamp <= observation["timestamp"]:
                    observation["display"] = observation["stable"] or "nearby"
                    continue
                first_fix = observation["timestamp"] is None
                observation["timestamp"] = timestamp
                if outside:
                    observation.update(stable="away", armed=True, display="away")
                elif inside:
                    arriving = not first_fix and observation["armed"]
                    observation.update(stable="home", armed=False, display="home")
                    assignment = config["assignments"].get(member_id)
                    last = config["last_triggered"].get(member_id, 0)
                    if arriving and assignment and now - last >= COOLDOWN_SECONDS:
                        if len(self.queue) >= 50:
                            self.store.event("system", "Arrival queue is full", "A welcome was skipped rather than interrupt another clip.")
                            continue
                        if any(item["member_id"] == member_id for item in self.queue) or (self.active and self.active["member_id"] == member_id):
                            continue
                        self.queue.append({"member_id": member_id, "name": person["name"], "clip": copy.deepcopy(assignment),
                            "queued_at": now, "home_key": home["key"]})
                        self.store.mutate(lambda state, mid=member_id: state["arrivals"]["last_triggered"].update({mid: now}))
                        self.store.event("arrival", f"{person['name']} is nearby", "Welcome clip queued from a fresh Life360 proximity transition.")
                else:
                    observation["display"] = observation["stable"] or "nearby"

    def drain_once(self):
        """Play queued welcomes sequentially without interrupting a manual playback."""
        with self.audio_lock, self.lock:
            playback = self.player.state()
            if self.active and not playback["playing"]:
                if playback.get("error"):
                    self.speaker_error = playback["error"]
                self.active = None
            if playback["playing"] or self.monotonic() < self.manual_until:
                return False
            state = self.store.snapshot()
            config = state["arrivals"]
            if (not self.auth_enabled or not config["enabled"] or not config["home"] or not self.store.token()
                    or state["life360"]["status"] == "reauth_required" or self.needs_sync or self.stop_event.is_set()):
                return False
            visible = {person["id"] for person in config["directory"]["people"]}
            while self.queue:
                item = self.queue.popleft()
                member_id = item["member_id"]
                if member_id not in visible or item["home_key"] != config["home"]["key"]:
                    continue
                if self.clock() - item["queued_at"] > QUEUE_LIFETIME_SECONDS or self.observations.get(member_id, {}).get("stable") == "away":
                    self.store.event("system", "Queued welcome skipped", "The arrival was over ten minutes ago or the person has already left.")
                    continue
                observation = self.observations.get(member_id, {})
                timestamp = observation.get("timestamp")
                if (timestamp is None or self.clock() - timestamp > MAX_FIX_AGE_SECONDS
                        or observation.get("display") in {"unavailable", "stale", "poor_accuracy"}):
                    self.store.event("system", "Queued welcome skipped", "The person's current GPS fix is no longer usable.")
                    continue
                track = self.store.track(item["clip"]["track_id"])
                if not track or member_id not in config["assignments"]:
                    continue
                try:
                    clip = arrival_clip(track, item["clip"])
                    if item["clip"] != config["assignments"][member_id]:
                        continue
                    self.player.play(self.store.track_path(track), track, clip["start"], clip["duration"], state["volume"],
                                     fade_in=clip["fade_in"], fade_out=clip["fade_out"])
                    self.active = {"member_id": member_id, "name": item["name"], "track_id": track["id"]}
                    self.speaker_error = None
                    self.store.event("arrival_play", f"Welcoming {item['name']}", f"{track['title']} · {clip['duration']:g}s · Pi speaker")
                    return True
                except (ValueError, OSError):
                    self.speaker_error = "Could not play the arrival clip. Check ffplay, the audio file, and the Pi's default speaker."
                    self.store.event("error", "Welcome sound could not play", self.speaker_error)
            return False
