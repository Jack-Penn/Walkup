"""Optional real-Chromium regression tests; see README for the one-time browser setup."""
import io
import json
import os
from pathlib import Path
import threading
import wave

import pytest
from werkzeug.serving import make_server

if os.environ.get("THRESHOLD_BROWSER_TESTS") != "1":
    pytest.skip("Set THRESHOLD_BROWSER_TESTS=1 to run real-browser tests.", allow_module_level=True)

playwright = pytest.importorskip("playwright.sync_api")
from app import create_app


@pytest.fixture
def portal_page(tmp_path):
    # A temporary, isolated library: these tests never touch the user's audio or credentials.
    app = create_app({"TESTING": True, "DATA_DIR": str(tmp_path / "data"), "PORTAL_PASSWORD": "", "SEED_DEMO": True})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(base_url=f"http://127.0.0.1:{server.server_port}", viewport={"width": 1440, "height": 1050})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="networkidle")
            playwright.expect(page.locator("#selected-title")).to_have_text("Soft landing")
            yield page
            assert not errors, errors
        finally:
            browser.close()
            server.shutdown()
            thread.join(timeout=3)


def upload_data(name="Fresh hello.wav"):
    content = io.BytesIO()
    with wave.open(content, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x20" * 32000)
    return {"name": name, "mimeType": "audio/wav", "buffer": content.getvalue()}


def configure_clip(page, start=2, duration=5, fade_in=1.5, fade_out=1.5):
    page.locator("#clip-start").fill(str(start))
    page.locator("#clip-duration").fill(str(duration))
    page.locator("#fade-in").fill(str(fade_in))
    page.locator("#fade-out").fill(str(fade_out))
    page.locator("#fade-out").press("Tab")
    page.evaluate("() => saveClip()")


def test_upload_selects_visible_track_and_ignores_stale_poll(portal_page):
    page = portal_page
    held = {}
    def hold_old_state(route):
        response = route.fetch()
        held.update(route=route, body=response.body(), headers=response.headers)
        # Deliberately hold an old poll until AFTER the new upload is selected.
    page.route("**/api/state", hold_old_state)
    with page.expect_request("**/api/state"):
        page.evaluate("() => { refreshState(); }")
    page.locator("#track-search").fill("nothing matches this")
    page.locator("#track-sort").select_option("name")
    page.locator("#fade-in").fill("0.8")  # Flush this pending autosave before uploading.
    page.locator("#file-input").set_input_files(upload_data())
    playwright.expect(page.locator("#selected-title")).to_have_text("Fresh hello")
    playwright.expect(page.locator("#track-search")).to_have_value("")
    playwright.expect(page.locator("#track-sort")).to_have_value("recent")
    selected = page.locator(".track-row.selected")
    playwright.expect(selected).to_have_count(1)
    playwright.expect(selected).to_contain_text("Fresh hello")
    playwright.expect(selected.locator(".track-selected")).to_be_visible()
    playwright.expect(page.locator("#selected-meta")).to_contain_text("WAV audio · 0:04 total")
    playwright.expect(page.locator("#play-button")).to_be_enabled()
    playwright.expect(page.locator("#fade-in")).to_have_value("0")
    assert page.evaluate("() => audio.paused") is True
    assert held
    with page.expect_response("**/api/state"):
        held["route"].fulfill(status=200, body=held["body"], headers=held["headers"])
    page.unroute("**/api/state", hold_old_state)
    playwright.expect(page.locator("#selected-title")).to_have_text("Fresh hello")
    state = page.request.get("/api/state").json()["state"]
    demo = next(track for track in state["tracks"] if track.get("demo"))
    assert state["clips"][demo["id"]]["fade_in"] == .8
    assert state["selected_id"] == selected.get_attribute("data-track")


def test_multiple_uploads_select_last_success_and_bad_upload_unlocks_ui(portal_page):
    page = portal_page
    bad = {"name": "broken.mp3", "mimeType": "audio/mpeg", "buffer": b"not an mp3"}
    page.locator("#file-input").set_input_files([upload_data("First.wav"), bad, upload_data("Last.wav")])
    playwright.expect(page.locator("#selected-title")).to_have_text("Last")
    playwright.expect(page.locator("#upload-button")).to_be_enabled()
    playwright.expect(page.locator(".track-row")).to_have_count(3)
    playwright.expect(page.locator(".track-row.selected")).to_contain_text("Last")
    playwright.expect(page.locator(".toast.error")).to_have_count(1)
    page.locator("#file-input").set_input_files(bad)
    playwright.expect(page.locator(".toast.error")).to_have_count(2)
    playwright.expect(page.locator("#upload-button")).to_be_enabled()
    playwright.expect(page.locator("#fade-in")).to_be_enabled()
    playwright.expect(page.locator("#selected-title")).to_have_text("Last")


def test_fades_persist_switch_tracks_and_shorten_safely(portal_page):
    page = portal_page
    configure_clip(page, duration=6, fade_in=2, fade_out=2)
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#fade-in")).to_have_value("2")
    playwright.expect(page.locator("#fade-out")).to_have_value("2")
    page.locator("#clip-duration").fill("1")
    page.locator("#clip-duration").press("Tab")
    playwright.expect(page.locator("#fade-in")).to_have_value("0.5")
    playwright.expect(page.locator("#fade-out")).to_have_value("0.5")
    page.locator("#file-input").set_input_files(upload_data())
    playwright.expect(page.locator("#selected-title")).to_have_text("Fresh hello")
    playwright.expect(page.locator("#fade-in")).to_have_value("0")
    page.locator(".track-row", has_text="Soft landing").click()
    playwright.expect(page.locator("#selected-title")).to_have_text("Soft landing")
    playwright.expect(page.locator("#clip-start")).to_have_value("2")
    playwright.expect(page.locator("#fade-in")).to_have_value("0.5")
    playwright.expect(page.locator("#fade-out")).to_have_value("0.5")
    page.locator("#fade-out").fill("9")
    page.locator("#fade-out").press("Tab")
    playwright.expect(page.locator("#fade-out")).to_have_value("0.5")
    playwright.expect(page.locator(".toast.error")).to_have_count(0)


def test_browser_fades_use_audio_clock_and_separate_master_volume(portal_page):
    page = portal_page
    configure_clip(page)
    page.locator("#play-button").click()
    playwright.expect(page.locator("#playing-status")).to_contain_text("Playing in this browser")
    page.wait_for_timeout(250)
    def sample():
        return page.evaluate("() => ({elapsed: audio.currentTime - 2, gain: browserEnvelope.fadeGain.gain.value, master: browserEnvelope.volumeGain.gain.value, paused: audio.paused})")
    beginning = sample()
    assert 0 < beginning["elapsed"] < 1.5, beginning
    assert 0 < beginning["gain"] < .9, beginning
    page.evaluate("() => { const input = document.getElementById('volume'); input.value = 25; input.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.wait_for_timeout(1650)
    middle = sample()
    assert 1.5 < middle["elapsed"] < 3.5, middle
    assert .98 <= middle["gain"] <= 1, middle
    assert middle["master"] == pytest.approx(.25)
    page.wait_for_timeout(2050)
    end = sample()
    assert 3.5 < end["elapsed"] < 5, end
    assert 0 < end["gain"] < .9, end
    assert page.evaluate("() => audio.volume") == 1
    page.wait_for_timeout(1400)
    assert page.evaluate("() => audio.paused") is True
    playwright.expect(page.locator("#play-button-label")).to_have_text("Play clip")
    playwright.expect(page.locator(".toast.error")).to_have_count(0)


def test_full_clip_fade_in_has_a_real_ramp(portal_page):
    page = portal_page
    configure_clip(page, start=1, duration=2, fade_in=2, fade_out=0)
    page.locator("#play-button").click()
    playwright.expect(page.locator("#playing-status")).to_contain_text("Playing in this browser")
    page.wait_for_timeout(450)
    sample = page.evaluate("() => ({time: audio.currentTime, gain: browserEnvelope.fadeGain.gain.value})")
    assert 1 < sample["time"] < 2.5, sample
    assert .05 < sample["gain"] < .8, sample
    page.locator("#stop-button").click()
    assert page.evaluate("() => audio.paused") is True


@pytest.mark.parametrize("width", [360, 390, 675, 768, 810, 1024, 1440])
def test_simple_controls_fit_mobile_and_desktop(portal_page, width):
    page = portal_page
    page.set_viewport_size({"width": width, "height": 950})
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    for field in ["clip-start", "clip-duration", "fade-in", "fade-out"]:
        playwright.expect(page.locator(f"#{field}")).to_be_visible()
        assert page.locator(f"#{field}").bounding_box()["width"] >= 24
