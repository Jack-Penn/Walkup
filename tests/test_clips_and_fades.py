import array
import io
import json
import re
import shutil
import subprocess
import wave
from unittest.mock import Mock

import pytest

from app import create_app
from portal.clips import clip_values, default_clip, fade_filter
from portal.storage import Store


@pytest.fixture
def portal(tmp_path):
    app = create_app({"TESTING": True, "DATA_DIR": str(tmp_path),
                      "PORTAL_PASSWORD": "", "SEED_DEMO": False})
    client = app.test_client(use_cookies=False)
    html = client.get("/").get_data(as_text=True)
    csrf = re.search(r'name="portal-csrf" content="([^"]+)"', html).group(1)
    return app, client, {"X-Portal-CSRF": csrf}


def audio_file(seconds=6):
    content = io.BytesIO()
    with wave.open(content, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x40" * (seconds * 8000))
    content.seek(0)
    return content


def upload(portal, name="Hello.wav"):
    _, client, headers = portal
    response = client.post("/api/audio", data={"file": (audio_file(), name)}, headers=headers)
    assert response.status_code == 200, response.json
    return response.json


def test_upload_immediately_selects_new_track_with_fades_off(portal):
    first = upload(portal)
    second = upload(portal, "My new entrance.wav")
    state = second["state"]
    assert state["selected_id"] == second["track"]["id"] == second["selected_track_id"]
    assert state["tracks"][0]["title"] == "My new entrance"
    assert state["clips"][state["selected_id"]] == default_clip(6)
    assert state["revision"] > first["state"]["revision"]
    assert state["playback"]["playing"] is False


def test_late_autosave_does_not_steal_upload_selection(portal):
    _, client, headers = portal
    old = upload(portal)["track"]["id"]
    newest = upload(portal, "Newest.wav")["state"]
    response = client.post("/api/selection", json={"track_id": old, "start": 1, "duration": 3,
        "fade_in": .5, "fade_out": 1, "select": False}, headers=headers)
    assert response.status_code == 200
    state = response.json["state"]
    assert state["selected_id"] == newest["selected_id"]
    assert state["revision"] > newest["revision"]
    assert state["clips"][old] == {"start": 1, "duration": 3, "fade_in": .5, "fade_out": 1}
    selected = client.post("/api/selection", json={"track_id": old}, headers=headers).json["state"]
    assert selected["selected_id"] == old
    assert selected["clips"][old] == state["clips"][old]


def test_fades_persist_per_track_and_across_restart(portal):
    app, client, headers = portal
    first = upload(portal)["track"]["id"]
    clip = {"start": 1.25, "duration": 3.5, "fade_in": .75, "fade_out": 1.25}
    response = client.post("/api/selection", json={"track_id": first, **clip}, headers=headers)
    assert response.status_code == 200
    second = upload(portal, "Second.wav")["track"]["id"]
    reloaded = create_app(app.config)
    state = reloaded.test_client().get("/api/state").json["state"]
    assert state["selected_id"] == second
    assert state["clips"][first] == clip
    assert state["clips"][second]["fade_in"] == 0
    assert state["clips"][second]["fade_out"] == 0
    assert state["revision"] >= response.json["state"]["revision"]


@pytest.mark.parametrize("output", ["browser", "server"])
def test_play_passes_and_returns_saved_fades(portal, output):
    app, client, headers = portal
    track = upload(portal)["track"]["id"]
    player = app.extensions["player"]
    player.play = Mock()
    clip = {"start": 2, "duration": 3, "fade_in": .5, "fade_out": 1.25}
    response = client.post("/api/play", json={"track_id": track, "output": output, **clip}, headers=headers)
    assert response.status_code == 200, response.json
    assert {key: response.json[key] for key in clip} == clip
    assert response.json["state"]["clips"][track] == clip
    if output == "server":
        assert player.play.call_args.args[2:] == (2, 3, 75)
        assert player.play.call_args.kwargs == {"fade_in": .5, "fade_out": 1.25}
    else:
        player.play.assert_not_called()


@pytest.mark.parametrize("fades", [
    {"fade_in": -1}, {"fade_out": -1}, {"fade_in": float("nan")},
    {"fade_out": float("inf")}, {"fade_in": "not a number"}, {"fade_out": None},
    {"fade_in": True}, {"fade_in": 4}, {"fade_out": 3.001},
    {"fade_in": 2, "fade_out": 2},
])
def test_invalid_fades_rejected_without_changing_saved_clip(portal, fades):
    _, client, headers = portal
    added = upload(portal)
    track = added["track"]["id"]
    previous = added["state"]["clips"][track]
    response = client.post("/api/selection", json={"track_id": track, "start": 1, "duration": 3, **fades}, headers=headers)
    assert response.status_code == 400
    assert client.get("/api/state").json["state"]["clips"][track] == previous
    response = client.post("/api/play", json={"track_id": track, "output": "browser", "start": 1, "duration": 3, **fades}, headers=headers)
    assert response.status_code == 400


def test_invalid_selection_flag_is_rejected(portal):
    _, client, headers = portal
    track = upload(portal)["track"]["id"]
    assert client.post("/api/selection", json={"track_id": track, "select": "false"}, headers=headers).status_code == 400


def test_legacy_clips_gain_zero_fades_without_losing_data(tmp_path):
    store = Store(tmp_path)
    store.mutate(lambda state: state.update(selected_id="existing", clips={"existing": {"start": 2.5, "duration": 4}}, volume=42))
    reloaded = Store(tmp_path).snapshot()
    assert reloaded["clips"]["existing"] == {"start": 2.5, "duration": 4, "fade_in": 0, "fade_out": 0}
    assert reloaded["selected_id"] == "existing"
    assert reloaded["volume"] == 42
    assert reloaded["revision"] == store.snapshot()["revision"]


def test_shorter_clip_scales_existing_fades_and_zero_disables_them():
    existing = {"start": 0, "duration": 6, "fade_in": 1, "fade_out": 2}
    trimmed = clip_values({"duration": 1.5}, 6, existing)
    assert trimmed["fade_in"] == .5
    assert trimmed["fade_out"] == 1
    disabled = clip_values({"fade_in": 0, "fade_out": 0}, 6, trimmed)
    assert disabled["fade_in"] == disabled["fade_out"] == 0
    assert fade_filter(3) == ""
    assert fade_filter(3, .5, 1) == "asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.5:curve=tri,afade=t=out:st=2:d=1:curve=tri"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is optional")
def test_real_fade_audio_starts_and_ends_quiet_after_nonzero_seek(tmp_path):
    source = tmp_path / "constant.wav"
    source.write_bytes(audio_file().getvalue())
    filtered = subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.25", "-i", str(source),
        "-t", "3", "-af", fade_filter(3, 1, 1), "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1"],
        check=True, capture_output=True, timeout=15)
    samples = array.array("f")
    samples.frombytes(filtered.stdout)
    assert len(samples) == 24000
    average = lambda start, end: sum(abs(value) for value in samples[int(start * 8000):int(end * 8000)]) / int((end - start) * 8000)
    middle = average(1.2, 1.8)
    assert .48 < middle < .52
    assert average(.02, .08) < middle * .08
    assert average(2.92, 2.98) < middle * .08
