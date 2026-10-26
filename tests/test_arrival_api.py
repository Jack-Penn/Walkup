import copy
import json
import re
import wave
from unittest.mock import Mock

import pytest
import app as portal_app
from portal.life360 import Life360Error


class Account:
    def __init__(self, token):
        pass
    def circles(self):
        return [{"id": "family", "name": "Family"}, {"id": "friends", "name": "Friends"}]
    def members(self, circle):
        return [{"id": "ada", "firstName": "Ada", "lastName": "Resident"},
                {"id": "bob" if circle == "family" else "cy", "firstName": "Bob" if circle == "family" else "Cy"}]
    def places(self, circle):
        return [{"id": "home", "name": "Home", "latitude": "29.65" if circle == "family" else "30.1", "longitude": "-82.33", "radius": "99999"}]


def authenticated(app):
    client = app.test_client()
    page = client.get("/login").get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    page = client.post("/login", data={"password": app.config["PORTAL_PASSWORD"], "csrf": csrf}, follow_redirects=True).get_data(as_text=True)
    csrf = re.search(r'name="portal-csrf" content="([^"]+)"', page).group(1)
    return client, {"X-Portal-CSRF": csrf}


@pytest.fixture
def portal(tmp_path):
    app = portal_app.create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "PORTAL_PASSWORD": "test-password",
                                "SEED_DEMO": False, "LIFE360_CLIENT_FACTORY": Account})
    client, headers = authenticated(app)
    store = app.extensions["store"]
    store.set_token("FAKE_SAVED_LIFE360_TOKEN")
    store.mutate(lambda state: state["life360"].update(status="connected", account_name="Owner", account_id="owner-one"))
    engine = app.extensions["arrivals"]
    engine._call = lambda function, *args: function(*args)
    engine.request_sync()
    engine.run_once()
    path = store.audio_dir / "long.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000 * 60)
    track = {"id": "long", "title": "Long welcome", "stored_name": path.name, "original_name": "Long welcome.wav",
        "duration": 60, "format": "WAV", "bytes": path.stat().st_size, "peaks": [0] * 128, "created_at": "2026-10-25T00:00:00+00:00"}
    def add(state):
        state["tracks"] = [track]
        state["selected_id"] = "long"
        state["clips"]["long"] = {"start": 0, "duration": 55, "fade_in": 2, "fade_out": 3}
    store.mutate(add)
    player = app.extensions["player"]
    capabilities = player.capabilities()
    player.capabilities = lambda: capabilities | {"server_available": True}
    player.play = Mock()
    yield app, client, headers
    engine.stop()


def assign(client, headers, duration=30):
    return client.post("/api/arrivals/assignments/ada", json={"track_id": "long", "start": 20,
        "duration": duration, "fade_in": 1, "fade_out": 2}, headers=headers)


def test_arrival_setup_is_password_and_csrf_protected(tmp_path):
    app = portal_app.create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "PORTAL_PASSWORD": "", "SEED_DEMO": False})
    client = app.test_client(use_cookies=False)
    html = client.get("/").get_data(as_text=True)
    token = re.search(r'name="portal-csrf" content="([^"]+)"', html).group(1)
    for path in ("/api/arrivals/sync", "/api/arrivals/settings", "/api/arrivals/assignments/ada"):
        assert client.post(path, json={}, headers={"X-Portal-CSRF": token}).status_code == 403


def test_discovery_state_exposes_all_circles_but_no_raw_coordinates_or_token(portal):
    app, client, headers = portal
    response = client.get("/api/state")
    state = response.json["state"]["arrivals"]
    assert len(state["circles"]) == 2 and len(state["people"]) == 3
    assert len(state["places"]) == 2
    assert "FAKE_SAVED_LIFE360_TOKEN" not in response.get_data(as_text=True)
    assert '"latitude"' not in response.get_data(as_text=True)
    assert app.extensions["arrivals"].threads == []  # Factory tests never start network workers.
    response = client.post("/api/arrivals/sync", json={}, headers=headers)
    assert response.status_code == 202
    assert response.json["state"]["arrivals"]["runtime"]["sync_pending"] is True


def test_arrival_registration_cap_does_not_change_manual_playback(portal):
    app, client, headers = portal
    assert assign(client, headers, 31).status_code == 400
    response = assign(client, headers)
    assert response.status_code == 200
    assert response.json["assignment"]["duration"] == 30
    assert response.json["state"]["clips"]["long"]["duration"] == 55
    response = client.post("/api/play", json={"track_id": "long", "output": "browser", "start": 0, "duration": 55}, headers=headers)
    assert response.status_code == 200
    assert response.json["duration"] == 55
    assert response.json["state"]["arrivals"]["assignments"]["ada"]["duration"] == 30


def test_saved_place_selection_and_radius_are_independent_of_life360_radius(portal):
    app, client, headers = portal
    assign(client, headers)
    response = client.post("/api/arrivals/settings", json={"home_key": "friends:home", "radius_m": 75, "enabled": True}, headers=headers)
    assert response.status_code == 200, response.json
    state = response.json["state"]["arrivals"]
    assert state["home"]["circle_id"] == "friends"
    assert state["radius_m"] == 75 and state["exit_radius_m"] == 125
    assert app.extensions["store"].snapshot()["arrivals"]["home"]["latitude"] == 30.1
    response = client.post("/api/arrivals/settings", json={"latitude": 1, "longitude": 2}, headers=headers)
    assert response.status_code == 400


def test_enable_requires_home_assignment_and_server_player(portal):
    app, client, headers = portal
    response = client.post("/api/arrivals/settings", json={"enabled": True}, headers=headers)
    assert response.status_code == 400
    response = client.post("/api/arrivals/settings", json={"enabled": True, "home_key": "family:home"}, headers=headers)
    assert response.status_code == 400
    assign(client, headers)
    app.extensions["player"].capabilities = lambda: {"server_available": False}
    response = client.post("/api/arrivals/settings", json={"enabled": True, "home_key": "family:home"}, headers=headers)
    assert response.status_code == 400
    assert "ffplay" in response.json["error"]


def test_unknown_person_and_streaming_source_cannot_be_assigned(portal):
    app, client, headers = portal
    assert client.post("/api/arrivals/assignments/unknown", json={"track_id": "long", "duration": 5}, headers=headers).status_code == 400
    assert client.post("/api/arrivals/assignments/ada", json={"track_id": "long", "source": "spotify", "duration": 5}, headers=headers).status_code == 400


def test_test_welcome_uses_registered_clip_but_does_not_simulate_arrival(portal):
    app, client, headers = portal
    assign(client, headers)
    response = client.post("/api/arrivals/assignments/ada/test", json={}, headers=headers)
    assert response.status_code == 200
    call = app.extensions["player"].play.call_args
    assert call.args[2:] == (20, 30, 75)
    assert call.kwargs == {"fade_in": 1, "fade_out": 2}
    assert app.extensions["store"].snapshot()["arrivals"]["last_triggered"] == {}
    assert app.extensions["store"].snapshot()["clips"]["long"]["duration"] == 55


def test_delete_audio_unassigns_arrival_and_disables_if_last_assignment(portal):
    app, client, headers = portal
    assign(client, headers)
    client.post("/api/arrivals/settings", json={"home_key": "family:home", "enabled": True}, headers=headers)
    response = client.delete("/api/audio/long", headers=headers)
    assert response.status_code == 200
    assert response.json["state"]["arrivals"]["assignments"] == {}
    assert response.json["state"]["arrivals"]["enabled"] is False


def test_failed_token_replacement_preserves_working_arrival_setup(portal, monkeypatch):
    app, client, headers = portal
    assign(client, headers)
    before = app.extensions["store"].snapshot()["arrivals"]
    app.extensions["arrivals"].not_before[0] = 0
    def invalid(token):
        raise Life360Error("Rejected", "reauth_required", 401)
    monkeypatch.setattr(portal_app, "validate_token", invalid)
    assert client.post("/api/life360/token", json={"token": "FAKE_NEW_BAD_TOKEN"}, headers=headers).status_code == 502
    assert app.extensions["store"].snapshot()["arrivals"] == before
    assert app.extensions["store"].token() == "FAKE_SAVED_LIFE360_TOKEN"


@pytest.mark.parametrize("different_account", [False, True])
def test_valid_token_replacement_resets_baseline_and_handles_account_switch(portal, monkeypatch, different_account):
    app, client, headers = portal
    assign(client, headers)
    engine = app.extensions["arrivals"]
    engine.not_before[0] = 0
    monkeypatch.setattr(portal_app, "validate_token", lambda token: {"account_name": "Owner", "account_id": "owner-two" if different_account else "owner-one"})
    response = client.post("/api/life360/token", json={"token": "FAKE_VALID_REPLACEMENT"}, headers=headers)
    assert response.status_code == 200
    assert response.json["state"]["arrivals"]["runtime"]["sync_pending"]
    assert bool(response.json["state"]["arrivals"]["assignments"]) is not different_account
    assert app.extensions["store"].track("long") is not None
    assert not engine.observations


def test_disconnect_clears_arrival_metadata_and_assignments_not_audio(portal):
    app, client, headers = portal
    assign(client, headers)
    response = client.delete("/api/life360/token", headers=headers)
    state = response.json["state"]
    assert state["arrivals"]["people"] == []
    assert state["arrivals"]["places"] == []
    assert state["arrivals"]["home"] is None
    assert state["arrivals"]["assignments"] == {}
    assert state["tracks"][0]["id"] == "long"
    assert app.extensions["store"].token() == ""


def test_cached_life360_people_cannot_be_served_without_a_password(tmp_path):
    from portal.storage import Store
    store = Store(tmp_path)
    store.mutate(lambda state: state["arrivals"]["directory"].update(people=[{"id": "private-person"}]))
    with pytest.raises(RuntimeError, match="PORTAL_PASSWORD"):
        portal_app.create_app({"TESTING": False, "DATA_DIR": str(tmp_path), "PORTAL_PASSWORD": "", "SEED_DEMO": False})


def test_late_browser_end_api_does_not_release_current_preview(portal):
    app, client, headers = portal
    for preview in ("old-page-1", "new-page-2"):
        response = client.post("/api/play", json={"track_id": "long", "output": "browser", "duration": 55,
            "preview_id": preview}, headers=headers)
        assert response.status_code == 200
    response = client.post("/api/arrivals/manual-ended", json={"preview_id": "old-page-1"}, headers=headers)
    assert response.json["state"]["arrivals"]["runtime"]["manual_active"]
    response = client.post("/api/arrivals/manual-ended", json={"preview_id": "new-page-2"}, headers=headers)
    assert not response.json["state"]["arrivals"]["runtime"]["manual_active"]
