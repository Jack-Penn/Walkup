import copy
import json
from types import SimpleNamespace

import pytest

from portal.arrivals import (ArrivalEngine, ARRIVAL_MAX_SECONDS, COOLDOWN_SECONDS,
    MAX_FIX_AGE_SECONDS, arrival_clip, default_arrivals, distance_meters, location_sample)
from portal.life360 import Life360Error
from portal.storage import Store


class Clock:
    def __init__(self):
        self.now = 1_790_000_000.0
        self.mono = 1000.0
    def time(self):
        return self.now
    def monotonic(self):
        return self.mono
    def advance(self, seconds=60):
        self.now += seconds
        self.mono += seconds


class Speaker:
    def __init__(self):
        self.calls = []
        self.playing = False
        self.error = None
    def capabilities(self):
        return {"server_available": True, "hardware_detected": False}
    def state(self):
        return {"playing": self.playing, "track_id": self.calls[-1][1]["id"] if self.playing else None,
                "error": self.error}
    def play(self, path, track, start, duration, volume, **fades):
        self.calls.append((path, track, start, duration, volume, fades))
        self.playing = True
    def stop(self, log=True):
        self.playing = False
        self.error = None


def raw_member(member_id, clock, meters=300, *, name="Ada", accuracy_m=10, age=0):
    return {"id": member_id, "firstName": name, "lastName": "Resident", "features": {"shareLocation": "1"},
        "location": {"latitude": str(29.65 + meters / 111194.9266), "longitude": "-82.33",
                     "accuracy": str(accuracy_m / .3048), "timestamp": str(clock.now - age), "since": "1"}}


class DirectoryClient:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.error = None
        self.values = {"family": [raw_member("ada", clock), raw_member("bob", clock, name="Bob")],
                       "friends": [raw_member("ada", clock), raw_member("cy", clock, name="Cy")]}
        self.saved_places = {
            "family": [{"id": "house", "name": "Home", "latitude": "29.65", "longitude": "-82.33", "radius": "99999"}],
            "friends": [{"id": "other", "name": "Home", "latitude": "30.1", "longitude": "-83.1"}],
        }
    def circles(self):
        self.calls.append(("circles",))
        if self.error:
            raise self.error
        return [{"id": "family", "name": "Family"}, {"id": "friends", "name": "Friends"}]
    def members(self, circle_id):
        self.calls.append(("members", circle_id))
        if self.error:
            raise self.error
        return copy.deepcopy(self.values[circle_id])
    def places(self, circle_id):
        self.calls.append(("places", circle_id))
        return copy.deepcopy(self.saved_places[circle_id])


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    store = Store(tmp_path)
    store.set_token("FAKE_TOKEN_FOR_LOCAL_TESTS")
    player = Speaker()
    client = DirectoryClient(clock)
    engine = ArrivalEngine(store, player, auth_enabled=True, client_factory=lambda token: client,
        clock=clock.time, monotonic=clock.monotonic)
    # Pacing belongs to the production HTTP worker; deterministic unit tests do not sleep.
    engine._call = lambda function, *args: function(*args)
    engine.request_sync()
    assert engine.run_once()
    track = {"id": "audio", "title": "A welcome", "duration": 120, "stored_name": "audio.wav"}
    store.track_path(track).write_bytes(b"test audio; the fake speaker does not decode")
    def seed(state):
        state["tracks"] = [track]
        state["selected_id"] = track["id"]
        state["clips"][track["id"]] = {"start": 0, "duration": 90, "fade_in": 2, "fade_out": 3}
    store.mutate(seed)
    engine.assign("ada", {"track_id": "audio", "start": 20, "duration": 30, "fade_in": 1, "fade_out": 2})
    engine.configure({"enabled": True, "home_key": "family:house", "radius_m": 100})
    yield SimpleNamespace(clock=clock, store=store, player=player, client=client, engine=engine, track=track)
    engine.stop()


def observe(setup, meters, member="ada", **kwargs):
    raw = raw_member(member, setup.clock, meters, **kwargs)
    sample = location_sample(raw)
    setup.engine.process_samples({member: sample})


def arrive(setup, member="ada"):
    observe(setup, 300, member)
    setup.clock.advance()
    observe(setup, 10, member)


def test_discovers_every_circle_and_deduplicates_people(setup):
    state = setup.engine.public_state()
    assert len(state["circles"]) == 2
    assert len(state["people"]) == 3
    ada = next(person for person in state["people"] if person["id"] == "ada")
    assert ada["circle_ids"] == ["family", "friends"]
    assert ada["circle_names"] == ["Family", "Friends"]
    assert {place["key"] for place in state["places"]} == {"family:house", "friends:other"}
    assert ("members", "family") in setup.client.calls and ("members", "friends") in setup.client.calls
    assert ("places", "family") in setup.client.calls and ("places", "friends") in setup.client.calls
    assert state["radius_m"] == 100  # Do not reuse the Life360 place's large radius.


def test_new_arrival_plays_assigned_segment_and_fades_on_speaker(setup):
    arrive(setup)
    assert len(setup.engine.queue) == 1
    assert setup.engine.drain_once()
    path, track, start, duration, volume, fades = setup.player.calls[-1]
    assert path.exists() and track["id"] == "audio"
    assert (start, duration, volume) == (20, 30, 75)
    assert fades == {"fade_in": 1, "fade_out": 2}
    assert setup.engine.public_state()["runtime"]["active"]["member_id"] == "ada"
    assert any(event["kind"] == "arrival_play" for event in setup.store.snapshot()["events"])


def test_never_plays_for_people_already_home_on_startup(setup):
    observe(setup, 10)
    setup.clock.advance()
    observe(setup, 15)
    assert not setup.engine.queue
    assert not setup.engine.drain_once()
    assert setup.engine.public_state()["observations"]["ada"]["status"] == "home"


def test_hysteresis_accounts_for_fix_uncertainty_and_avoids_boundary_flapping(setup):
    observe(setup, 300)
    setup.clock.advance()
    observe(setup, 95, accuracy_m=10)  # Accuracy circle is not fully inside 100m.
    assert not setup.engine.queue
    setup.clock.advance()
    observe(setup, 85, accuracy_m=10)
    assert len(setup.engine.queue) == 1
    setup.engine.queue.clear()
    setup.clock.advance(COOLDOWN_SECONDS + 1)
    observe(setup, 155, accuracy_m=10)  # Not definitely outside the 150m re-arm boundary.
    setup.clock.advance()
    observe(setup, 10)
    assert not setup.engine.queue
    setup.clock.advance()
    observe(setup, 170, accuracy_m=10)
    setup.clock.advance()
    observe(setup, 10)
    assert len(setup.engine.queue) == 1


@pytest.mark.parametrize("options", [{"age": MAX_FIX_AGE_SECONDS + 1}, {"accuracy_m": 101}, {"age": -61}])
def test_stale_inaccurate_and_future_fixes_never_trigger(setup, options):
    observe(setup, 300)
    setup.clock.advance()
    observe(setup, 5, **options)
    assert not setup.engine.queue
    assert setup.engine.public_state()["observations"]["ada"]["status"] in {"stale", "poor_accuracy"}


def test_duplicate_or_older_location_timestamp_never_triggers(setup):
    observe(setup, 300)
    observe(setup, 5)  # Different coordinate, but same source timestamp.
    assert not setup.engine.queue
    setup.clock.advance()
    observe(setup, 5, age=61)
    assert not setup.engine.queue
    observe(setup, 5)
    assert len(setup.engine.queue) == 1


def test_same_person_return_is_subject_to_cooldown_not_delayed_replay(setup):
    arrive(setup)
    setup.engine.queue.clear()
    setup.clock.advance()
    observe(setup, 300)
    setup.clock.advance()
    observe(setup, 10)
    assert not setup.engine.queue
    setup.clock.advance(COOLDOWN_SECONDS + 1)
    observe(setup, 10)
    assert not setup.engine.queue  # Remaining at home must not become a new arrival.
    setup.clock.advance()
    observe(setup, 300)
    setup.clock.advance()
    observe(setup, 10)
    assert len(setup.engine.queue) == 1


def test_unassigned_people_do_not_play_and_assigning_at_home_does_not_retroactively_play(setup):
    arrive(setup, "bob")
    assert not setup.engine.queue
    setup.engine.assign("bob", {"track_id": "audio", "duration": 5, "fade_in": 0, "fade_out": 0})
    setup.clock.advance()
    observe(setup, 10, "bob")
    assert not setup.engine.queue


def test_simultaneous_arrivals_queue_instead_of_interrupting(setup):
    setup.engine.assign("bob", {"track_id": "audio", "start": 1, "duration": 5, "fade_in": 0, "fade_out": 0})
    samples = {member: location_sample(raw_member(member, setup.clock, 300)) for member in ("ada", "bob")}
    setup.engine.process_samples(samples)
    setup.clock.advance()
    samples = {member: location_sample(raw_member(member, setup.clock, 5)) for member in ("ada", "bob")}
    setup.engine.process_samples(samples)
    assert len(setup.engine.queue) == 2
    assert setup.engine.drain_once()
    assert len(setup.player.calls) == 1
    assert not setup.engine.drain_once()
    setup.player.playing = False
    assert setup.engine.drain_once()
    assert [call[3] for call in setup.player.calls] == [30, 5]


def test_manual_preview_holds_auto_queue_until_it_finishes(setup):
    setup.engine.manual_override("browser", 90)
    arrive(setup)
    assert not setup.engine.drain_once()
    assert len(setup.engine.queue) == 1
    setup.engine.release_manual_preview()
    assert setup.engine.drain_once()


def test_disable_and_home_change_cancel_auto_and_reset_baselines(setup):
    arrive(setup)
    setup.engine.drain_once()
    assert setup.player.playing
    setup.engine.configure({"enabled": False})
    assert not setup.player.playing and not setup.engine.queue
    setup.engine.configure({"enabled": True, "home_key": "friends:other"})
    assert not setup.engine.observations


def test_queue_skips_departed_and_expired_arrivals(setup):
    arrive(setup)
    setup.clock.advance()
    observe(setup, 300)
    assert not setup.engine.drain_once()
    setup.clock.advance(COOLDOWN_SECONDS + 1)
    observe(setup, 10)
    assert len(setup.engine.queue) == 1
    setup.clock.advance(601)
    assert not setup.engine.drain_once()
    assert not setup.player.calls


def test_assignment_snapshot_persists_independently_of_manual_clip(setup):
    before = setup.store.snapshot()["arrivals"]["assignments"]["ada"]
    setup.store.mutate(lambda state: state["clips"]["audio"].update(start=5, duration=110, fade_in=10))
    reloaded = Store(setup.store.root).snapshot()
    assert reloaded["arrivals"]["assignments"]["ada"] == before
    assert reloaded["clips"]["audio"]["duration"] == 110
    assert reloaded["arrivals"]["home"]["key"] == "family:house"


@pytest.mark.parametrize("data", [
    {"duration": 30.001}, {"duration": 31}, {"duration": 120},
    {"duration": 3, "source": "spotify"}, {"duration": float("nan")},
])
def test_registration_enforces_30_second_arrival_cap(setup, data):
    with pytest.raises(ValueError):
        setup.engine.assign("ada", {"track_id": "audio", "fade_in": 0, "fade_out": 0, **data})
    assert setup.store.snapshot()["clips"]["audio"]["duration"] == 90


def test_playback_revalidates_even_tampered_oversized_arrival(setup):
    arrive(setup)
    setup.engine.queue[0]["clip"]["duration"] = 31
    assert not setup.engine.drain_once()
    assert not setup.player.calls
    assert setup.engine.speaker_error


@pytest.mark.parametrize("data", [{"latitude": 29, "longitude": -82}, {"home_key": "unknown"}, {"radius_m": 24},
    {"radius_m": float("inf")}, {"radius_m": True}, {"enabled": "yes"}])
def test_house_setup_accepts_only_saved_places_and_valid_radius(setup, data):
    with pytest.raises(ValueError):
        setup.engine.configure(data)
    assert setup.store.snapshot()["arrivals"]["home"]["key"] == "family:house"


def test_discovery_chooses_freshest_duplicate_location_across_circles(setup):
    observe(setup, 300)
    setup.clock.advance()
    setup.client.values["family"][0] = raw_member("ada", setup.clock, 5, age=20)
    setup.client.values["friends"][0] = raw_member("ada", setup.clock, 300)
    setup.engine.next_poll = 0
    assert setup.engine.run_once()
    assert not setup.engine.queue
    assert setup.engine.public_state()["observations"]["ada"]["status"] == "away"


def test_removed_home_place_disables_playback_without_losing_assignments(setup):
    setup.client.saved_places["family"] = []
    setup.clock.advance()
    setup.engine.request_sync()
    assert setup.engine.run_once()
    state = setup.store.snapshot()["arrivals"]
    assert state["home"] is None and state["enabled"] is False
    assert "ada" in state["assignments"]


def test_rate_limit_preserves_directory_credentials_and_honors_long_retry_after(setup):
    before = setup.store.snapshot()["arrivals"]["directory"]
    setup.client.error = Life360Error("Rate limited", "rate_limited", 429, "7200")
    setup.clock.advance()
    setup.engine.request_sync()
    assert setup.engine.run_once()
    assert setup.store.token() == "FAKE_TOKEN_FOR_LOCAL_TESTS"
    assert setup.store.snapshot()["arrivals"]["directory"] == before
    assert setup.engine.not_before[0] >= setup.clock.mono + 7200
    calls = len(setup.client.calls)
    assert not setup.engine.run_once()
    assert len(setup.client.calls) == calls
    assert setup.engine.public_state()["runtime"]["phase"] == "backoff"


def test_rejected_token_stops_auto_but_does_not_delete_credentials(setup):
    arrive(setup)
    setup.client.error = Life360Error("Rejected", "reauth_required", 401)
    setup.engine.next_poll = 0
    assert setup.engine.run_once()
    assert not setup.engine.queue
    assert setup.store.token() == "FAKE_TOKEN_FOR_LOCAL_TESTS"
    setup.clock.advance(9000)
    assert not setup.engine.run_once()


def test_public_state_and_persistence_do_not_store_members_coordinates(setup):
    arrive(setup)
    state = setup.engine.public_state()
    encoded = json.dumps(state)
    assert "FAKE_TOKEN" not in encoded
    assert '"latitude"' not in encoded and '"longitude"' not in encoded
    private = setup.store.snapshot()["arrivals"]
    assert private["home"]["latitude"] == 29.65  # Only saved place centers are persistent.
    assert all(set(person) == {"id", "name", "circle_ids", "circle_names"} for person in private["directory"]["people"])
    assert "observations" not in private


def test_disconnect_clears_life360_arrival_data_but_preserves_audio(setup):
    setup.engine.disconnect()
    assert setup.store.snapshot()["arrivals"] == default_arrivals()
    assert setup.store.track("audio") is not None


def test_deleting_assigned_audio_cancels_welcome_and_unassigns(setup):
    arrive(setup)
    setup.engine.remove_track("audio")
    assert not setup.engine.queue
    config = setup.store.snapshot()["arrivals"]
    assert not config["assignments"] and not config["enabled"]


@pytest.mark.parametrize("location", [None, {}, {"latitude": "nan", "longitude": "0", "accuracy": "10", "timestamp": "1"},
    {"latitude": "91", "longitude": "0", "accuracy": "10", "timestamp": "1"},
    {"latitude": "0", "longitude": "0", "accuracy": "0", "timestamp": "1"}])
def test_invalid_location_samples_are_not_treated_as_perfect_gps(location):
    assert location_sample({"location": location}) is None


def test_accuracy_units_milliseconds_and_sharing_are_respected():
    member = {"location": {"latitude": "0", "longitude": "0", "accuracy": "100", "timestamp": "1790000000000"}}
    sample = location_sample(member)
    assert sample["accuracy_m"] == pytest.approx(30.48)
    assert sample["timestamp"] == 1_790_000_000
    member["features"] = {"shareLocation": "0"}
    assert location_sample(member) is None
    assert distance_meters(0, 0, 0, 0) == 0
    assert distance_meters(0, 0, 1, 0) == pytest.approx(111194.9, abs=1)


def test_home_place_can_come_from_a_different_circle_than_the_arriving_person(setup):
    setup.engine.assign("bob", {"track_id": "audio", "duration": 5, "fade_in": 0, "fade_out": 0})
    setup.engine.configure({"home_key": "friends:other", "enabled": True})
    home = setup.store.snapshot()["arrivals"]["home"]
    def sample(meters):
        return {"latitude": home["latitude"] + meters / 111194.9266, "longitude": home["longitude"],
                "accuracy_m": 10, "timestamp": setup.clock.now}
    setup.engine.process_samples({"bob": sample(300)})
    setup.clock.advance()
    setup.engine.process_samples({"bob": sample(5)})
    assert setup.engine.queue[0]["member_id"] == "bob"
    assert setup.engine.queue[0]["home_key"] == "friends:other"


def test_late_preview_end_cannot_release_a_newer_browser_preview(setup):
    setup.engine.manual_override("browser", 60, "first-preview")
    setup.engine.manual_override("browser", 60, "newer-preview")
    assert not setup.engine.release_manual_preview("first-preview")
    assert setup.engine.public_state()["runtime"]["manual_active"]
    assert setup.engine.release_manual_preview("newer-preview")
    assert not setup.engine.public_state()["runtime"]["manual_active"]


def test_rejected_token_cannot_enable_and_status_is_not_a_retry_or_sync(setup):
    setup.engine._network_error(Life360Error("Rejected", "reauth_required", 401), syncing=False)
    assert setup.engine.public_state()["runtime"]["phase"] == "reauth_required"
    setup.engine.syncing = True
    assert setup.engine.public_state()["runtime"]["phase"] == "reauth_required"
    with pytest.raises(ValueError, match="Replace the rejected"):
        setup.engine.configure({"enabled": True})


@pytest.mark.parametrize("invalid", ["expired_fix", "poor_accuracy", "unavailable"])
def test_queued_welcome_rechecks_location_before_speaker_playback(setup, invalid):
    arrive(setup)
    if invalid == "expired_fix":
        setup.clock.advance(181)
    elif invalid == "poor_accuracy":
        setup.clock.advance()
        observe(setup, 5, accuracy_m=500)
    else:
        setup.engine.process_samples({})
    assert not setup.engine.drain_once()
    assert not setup.player.calls
    assert not setup.engine.queue


@pytest.mark.parametrize("blocked", ["authentication_disabled", "rejected_token"])
def test_dispatch_rechecks_protection_even_if_queue_already_exists(setup, blocked):
    arrive(setup)
    if blocked == "authentication_disabled":
        setup.engine.auth_enabled = False
    else:
        setup.store.mutate(lambda state: state["life360"].update(status="reauth_required"))
    assert not setup.engine.drain_once()
    assert not setup.player.calls
