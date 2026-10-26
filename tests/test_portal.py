import io
import json
import os
import re
import stat
import wave
from unittest.mock import Mock

import pytest
import app as portal_app
from portal.life360 import Life360Error, normalize_token


@pytest.fixture
def application(tmp_path):
    return portal_app.create_app({"TESTING": True, "DATA_DIR": str(tmp_path),
        "PORTAL_PASSWORD": "a-portal-test-password", "SEED_DEMO": False})


def login(application):
    client = application.test_client()
    page = client.get("/login")
    token = re.search(r'name="csrf" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    response = client.post("/login", data={"csrf": token, "password": application.config["PORTAL_PASSWORD"]}, follow_redirects=True)
    assert response.status_code == 200
    token = re.search(r'name="portal-csrf" content="([^"]+)"', response.get_data(as_text=True)).group(1)
    return client, {"X-Portal-CSRF": token}


@pytest.fixture
def client(application):
    return login(application)


def wav_data(seconds=4):
    data = io.BytesIO()
    with wave.open(data, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(b"\x10\x10\x00\x00\xf0\xef\x00\x00" * (8000 * seconds // 4))
    data.seek(0)
    return data


def upload(client, title="Arrival.wav"):
    browser, headers = client
    response = browser.post("/api/audio", data={"file": (wav_data(), title)}, headers=headers)
    assert response.status_code == 200, response.json
    return response.json["track"]


def test_login_and_api_protection(application):
    browser = application.test_client()
    assert browser.get("/").status_code == 302
    assert browser.get("/api/state").json["code"] == "auth_required"
    assert browser.get("/health").json["ok"] is True


def test_wrong_login_and_csrf(application):
    browser = application.test_client()
    assert browser.post("/login", data={"password": "wrong"}).status_code == 403
    page = browser.get("/login").get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    response = browser.post("/login", data={"password": "wrong", "csrf": csrf})
    assert "doesn&#39;t match" in response.get_data(as_text=True)


def test_mutating_requests_require_csrf(client):
    browser, headers = client
    assert browser.post("/api/settings", json={"volume": 45}).status_code == 403
    assert browser.post("/api/settings", json={"volume": 45}, headers={"X-Portal-CSRF": "é"}).status_code == 403
    assert browser.post("/api/settings", json={"volume": 45}, headers=headers).json["state"]["volume"] == 45


def test_foreign_host_rejected_and_preview_host_accepted(application):
    browser = application.test_client()
    assert browser.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert browser.get("/login", headers={"Host": "8080-example.e2b.app"}).status_code == 200
    assert browser.get("/login", headers={"Host": "192.168.1.50:8080"}).status_code == 200


def test_upload_metadata_selection_and_range_download(client, application):
    browser, headers = client
    track = upload(client, "My welcome.wav")
    assert track["duration"] == 4
    assert track["title"] == "My welcome"
    assert len(track["peaks"]) == 128
    assert "stored_name" not in track
    response = browser.get(track["url"], headers={"Range": "bytes=0-10"})
    assert response.status_code == 206
    assert len(response.data) == 11
    assert application.extensions["store"].track(track["id"])["stored_name"] != "My welcome.wav"
    response = browser.post("/api/selection", json={"track_id": track["id"], "start": 1.2, "duration": 2.3}, headers=headers)
    assert response.json["state"]["clips"][track["id"]] == {"start": 1.2, "duration": 2.3, "fade_in": 0, "fade_out": 0}


def test_upload_rejects_non_audio(client):
    browser, headers = client
    assert browser.post("/api/audio", data={"file": (io.BytesIO(b"hello"), "test.txt")}, headers=headers).status_code == 400
    assert browser.post("/api/audio", data={"file": (io.BytesIO(b"not an mp3"), "fake.mp3")}, headers=headers).status_code == 400
    assert browser.get("/api/state").json["state"]["tracks"] == []


@pytest.mark.parametrize("start,duration", [(-1, 1), (4, 1), (1, 9), (0, 0), (0, -1), (float("nan"), 1), (0, float("inf"))])
def test_clip_validation(client, start, duration):
    browser, headers = client
    track = upload(client)
    result = browser.post("/api/selection", json={"track_id": track["id"], "start": start, "duration": duration}, headers=headers)
    assert result.status_code == 400


def test_browser_play_request_and_delete(client):
    browser, headers = client
    track = upload(client)
    response = browser.post("/api/play", json={"track_id": track["id"], "output": "browser", "start": .5, "duration": 2}, headers=headers)
    assert response.status_code == 200
    assert response.json["url"] == track["url"]
    assert response.json["start"] == .5
    assert response.json["state"]["playback"]["playing"] is False
    response = browser.delete(f"/api/audio/{track['id']}", headers=headers)
    assert response.json["state"]["tracks"] == []
    assert response.json["state"]["selected_id"] is None
    assert browser.get(track["url"]).status_code == 404


def test_server_play_passes_real_file_and_clip(client, application):
    browser, headers = client
    track = upload(client)
    player = application.extensions["player"]
    player.play = Mock()
    response = browser.post("/api/play", json={"track_id": track["id"], "output": "server", "start": 1, "duration": 2}, headers=headers)
    assert response.status_code == 200
    path, received_track, start, duration, volume = player.play.call_args.args
    assert path.exists()
    assert received_track["id"] == track["id"]
    assert (start, duration, volume) == (1, 2, 75)


def test_missing_server_player_is_a_helpful_error(client, monkeypatch):
    browser, headers = client
    track = upload(client)
    monkeypatch.setattr("portal.audio.shutil.which", lambda name: None)
    response = browser.post("/api/play", json={"track_id": track["id"], "output": "server"}, headers=headers)
    assert response.status_code == 400
    assert "ffplay" in response.json["error"]


def test_saved_token_is_not_exposed(client, application, monkeypatch):
    browser, headers = client
    secret = "THIS_IS_A_FAKE_SECRET_FOR_TESTS"
    monkeypatch.setattr(portal_app, "validate_token", lambda token: {"account_name": "Test Resident"})
    response = browser.post("/api/life360/token", json={"token": "Bearer " + secret}, headers=headers)
    assert response.status_code == 200
    assert response.json["state"]["life360"]["configured"] is True
    assert secret not in response.get_data(as_text=True)
    assert secret not in browser.get("/api/state").get_data(as_text=True)
    assert application.extensions["store"].token() == secret
    path = application.extensions["store"].root / "credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_bad_replacement_preserves_previous_token(client, application, monkeypatch):
    browser, headers = client
    application.extensions["store"].set_token("OLD_TOKEN_KEEP_ME")
    def rejected(token):
        raise Life360Error("Rejected token", "reauth_required", 401)
    monkeypatch.setattr(portal_app, "validate_token", rejected)
    response = browser.post("/api/life360/token", json={"token": "NEW_BUT_INVALID_TOKEN"}, headers=headers)
    assert response.status_code == 502
    assert application.extensions["store"].token() == "OLD_TOKEN_KEEP_ME"


@pytest.mark.parametrize("status,http_code", [("reauth_required", 401), ("blocked", 403), ("rate_limited", 429)])
def test_saved_token_failure_keeps_secret_and_updates_status(client, application, monkeypatch, status, http_code):
    browser, headers = client
    application.extensions["store"].set_token("PRESERVED_TEST_TOKEN")
    def rejected(token):
        raise Life360Error("A temporary test error", status, http_code, "60")
    monkeypatch.setattr(portal_app, "validate_token", rejected)
    response = browser.post("/api/life360/test", json={}, headers=headers)
    assert response.status_code == 502
    assert response.json["state"]["life360"]["status"] == status
    assert application.extensions["store"].token() == "PRESERVED_TEST_TOKEN"
    again = browser.post("/api/life360/test", json={}, headers=headers)
    assert again.status_code == 429


def test_audio_only_preview_does_not_require_cookie_and_blocks_credentials(tmp_path):
    application = portal_app.create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "PORTAL_PASSWORD": "", "SEED_DEMO": False})
    browser = application.test_client(use_cookies=False)
    page = browser.get("/").get_data(as_text=True)
    csrf = re.search(r'name="portal-csrf" content="([^"]+)"', page).group(1)
    headers = {"X-Portal-CSRF": csrf}
    assert browser.post("/api/settings", json={"volume": 60}, headers=headers).status_code == 200
    response = browser.post("/api/life360/token", json={"token": "A_FAKE_TEST_TOKEN"}, headers=headers)
    assert response.status_code == 403
    assert response.json["code"] == "password_required"


def test_persistence_and_token_removal(client, application):
    browser, headers = client
    track = upload(client)
    browser.post("/api/settings", json={"volume": 35}, headers=headers)
    application.extensions["store"].set_token("A_FAKE_TEST_TOKEN")
    reloaded = portal_app.create_app(application.config)
    client2, headers2 = login(reloaded)
    data = client2.get("/api/state").json["state"]
    assert data["selected_id"] == track["id"]
    assert data["volume"] == 35
    assert data["life360"]["configured"] is True
    assert client2.delete("/api/life360/token", headers=headers2).json["state"]["life360"]["configured"] is False
    assert reloaded.extensions["store"].token() == ""


def test_json_arrays_and_bad_volume_rejected(client):
    browser, headers = client
    assert browser.post("/api/settings", json=[], headers=headers).status_code == 400
    assert browser.post("/api/settings", json={"volume": 101}, headers=headers).status_code == 400
    assert browser.post("/api/settings", json={"volume": "loud"}, headers=headers).status_code == 400


def test_token_normalization():
    assert normalize_token('  "Bearer abcdefghijklmnop"  ') == "abcdefghijklmnop"
    with pytest.raises(ValueError):
        normalize_token("token with spaces")
    with pytest.raises(ValueError):
        normalize_token(None)
