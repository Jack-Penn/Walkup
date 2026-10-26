"""Optional browser tests with a local, simulated Life360 account; no real API calls."""
import io
import os
import threading
import time
import wave

import pytest
from werkzeug.serving import make_server

if os.environ.get("THRESHOLD_BROWSER_TESTS") != "1":
    pytest.skip("Set THRESHOLD_BROWSER_TESTS=1 to run real-browser tests.", allow_module_level=True)

playwright = pytest.importorskip("playwright.sync_api")
from app import create_app
from test_arrivals import Clock, DirectoryClient, raw_member


class RecordingSpeaker:
    def __init__(self):
        self.records = []
        self.playing = False
        self.started = 0
        self.played = threading.Event()
    def play(self, path, track, start, duration, volume, **fades):
        self.records.append((path, track, start, duration, volume, fades))
        self.started = time.monotonic()
        self.playing = True
        self.played.set()
    def state(self):
        elapsed = time.monotonic() - self.started
        if self.playing and elapsed >= self.records[-1][3]:
            self.playing = False
        return {"playing": self.playing, "track_id": self.records[-1][1]["id"] if self.playing else None,
                "duration": self.records[-1][3] if self.playing else 0,
                "offset": self.records[-1][2] if self.playing else 0,
                "elapsed": elapsed if self.playing else 0, "error": None}
    def stop(self, log=True):
        self.playing = False


@pytest.fixture
def home_portal(tmp_path):
    clock = Clock()
    clock.now = time.time()
    provider = DirectoryClient(clock)
    app = create_app({"TESTING": True, "DATA_DIR": str(tmp_path / "data"), "PORTAL_PASSWORD": "browser-test-password",
                      "SEED_DEMO": True, "LIFE360_CLIENT_FACTORY": lambda token: provider})
    store = app.extensions["store"]
    store.set_token("FAKE_BROWSER_TEST_TOKEN")
    store.mutate(lambda state: state["life360"].update(status="connected", account_name="Test Owner"))
    engine = app.extensions["arrivals"]
    engine.clock = clock.time
    engine.monotonic = clock.monotonic
    engine._call = lambda function, *args: function(*args)
    engine.request_sync()
    assert engine.run_once()
    player = app.extensions["player"]
    capabilities = player.capabilities()
    player.capabilities = lambda: capabilities | {"server_available": True, "hardware_detected": False}
    speaker = RecordingSpeaker()
    player.play, player.state, player.stop = speaker.play, speaker.state, speaker.stop
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(base_url=f"http://127.0.0.1:{server.server_port}", viewport={"width": 1440, "height": 1050})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto("/", wait_until="networkidle")
            page.locator('input[name="password"]').fill("browser-test-password")
            page.locator('button[type="submit"]').click()
            playwright.expect(page.locator("#selected-title")).to_have_text("Soft landing")
            playwright.expect(page.locator(".person-row")).to_have_count(3)
            yield page, app, clock, provider, speaker
            assert not errors, errors
        finally:
            engine.stop()
            browser.close()
            server.shutdown()
            thread.join(timeout=3)


def long_audio():
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x20" * 60 * 8000)
    return {"name": "New entrance.wav", "mimeType": "audio/wav", "buffer": data.getvalue()}


def enable_arrivals(page):
    page.locator('[data-person-assign="ada"]').click()
    playwright.expect(page.locator('[data-person-load="ada"]')).to_be_visible()
    page.locator("#arrival-setup-button").click()
    page.locator("#home-place").select_option("family:house")
    playwright.expect(page.locator("#arrivals-enabled")).to_be_enabled()
    page.locator("#arrivals-enabled").check()
    page.locator("#save-arrival-settings").click()
    playwright.expect(page.locator("#arrival-settings-dialog")).not_to_be_visible()
    playwright.expect(page.locator("#arrival-mode-label")).to_have_text("Proximity on")


def test_circle_filters_and_saved_place_only_setup(home_portal):
    page, app, clock, provider, speaker = home_portal
    playwright.expect(page.locator("#people-count")).to_have_text("3")
    ada = page.locator('[data-person="ada"]')
    playwright.expect(ada).to_contain_text("Family · Friends")
    page.locator("#people-circle").select_option("friends")
    playwright.expect(page.locator(".person-row")).to_have_count(2)
    page.locator("#people-search").fill("Ada")
    playwright.expect(page.locator(".person-row")).to_have_count(1)
    page.locator("#arrival-setup-button").click()
    playwright.expect(page.locator("#home-place option")).to_have_count(3)
    playwright.expect(page.locator("#arrivals-enabled")).to_be_disabled()
    assert page.locator('#arrival-settings-dialog input[name="latitude"]').count() == 0
    page.locator("#home-place").select_option("family:house")
    page.locator("#home-radius").fill("75")
    page.locator("#save-arrival-settings").click()
    playwright.expect(page.locator("#home-summary-detail")).to_contain_text("75 m proximity")
    assert app.extensions["store"].snapshot()["arrivals"]["home"]["key"] == "family:house"


def test_assign_cap_load_and_unassign_do_not_limit_manual_playback(home_portal):
    page, app, clock, provider, speaker = home_portal
    page.locator("#file-input").set_input_files(long_audio())
    playwright.expect(page.locator("#selected-title")).to_have_text("New entrance")
    page.locator("#clip-start").fill("5")
    page.locator("#clip-duration").fill("45")
    page.locator("#fade-in").fill("2")
    page.locator("#fade-out").fill("3")
    page.locator("#fade-out").press("Tab")
    playwright.expect(page.locator("#arrival-selection-note")).to_contain_text("first 30s only for arrivals")
    page.locator('[data-person-assign="ada"]').click()
    playwright.expect(page.locator('[data-person-load="ada"]')).to_contain_text("30s")
    playwright.expect(page.locator("#clip-duration")).to_have_value("45")
    config = app.extensions["store"].snapshot()["arrivals"]
    assert config["assignments"]["ada"]["duration"] == 30
    assert config["assignments"]["ada"]["start"] == 5
    assert config["assignments"]["ada"]["fade_out"] == 3
    page.reload(wait_until="networkidle")
    playwright.expect(page.locator("#clip-duration")).to_have_value("45")
    playwright.expect(page.locator('[data-person-load="ada"]')).to_contain_text("30s")
    page.locator('[data-person-load="ada"]').click()
    playwright.expect(page.locator("#clip-duration")).to_have_value("30")
    page.locator('[data-person-clear="ada"]').click()
    playwright.expect(page.locator('[data-person="ada"]')).to_contain_text("No welcome clip assigned")
    playwright.expect(page.locator(".track-row")).to_have_count(2)


def test_can_enable_pause_and_test_person_without_simulating_an_arrival(home_portal):
    page, app, clock, provider, speaker = home_portal
    enable_arrivals(page)
    page.locator('[data-person-test="ada"]').click()
    assert speaker.played.wait(3)
    assert speaker.records[-1][3] <= 30
    assert app.extensions["store"].snapshot()["arrivals"]["last_triggered"] == {}
    page.locator("#stop-button").click()
    assert not speaker.playing
    page.locator("#arrival-mode-button").click()
    playwright.expect(page.locator("#arrivals-enabled")).to_be_checked()
    page.locator("#arrivals-enabled").uncheck()
    page.locator("#save-arrival-settings").click()
    playwright.expect(page.locator("#arrival-mode-label")).to_have_text("Arrivals off")
    assert not app.extensions["store"].snapshot()["arrivals"]["enabled"]


def test_sync_updates_people_and_preserves_selection(home_portal):
    page, app, clock, provider, speaker = home_portal
    provider.values["family"].append(raw_member("dee", clock, name="Dee"))
    page.locator("#sync-people").click()
    playwright.expect(page.locator("#sync-people-label")).to_have_text("Syncing…")
    clock.advance(1)
    assert app.extensions["arrivals"].run_once()
    page.evaluate("() => refreshState()")
    playwright.expect(page.locator(".person-row")).to_have_count(4)
    playwright.expect(page.locator("#sync-people")).to_be_enabled()
    playwright.expect(page.locator("#selected-title")).to_have_text("Soft landing")


def test_background_arrival_still_plays_after_browser_is_closed(home_portal):
    page, app, clock, provider, speaker = home_portal
    enable_arrivals(page)
    engine = app.extensions["arrivals"]
    processed = threading.Event()
    original = engine.process_samples
    def consume(*args, **kwargs):
        original(*args, **kwargs)
        processed.set()
    engine.process_samples = consume
    clock.advance(1)
    engine.start()
    assert processed.wait(3), "background worker never read the outside fix"
    assert engine.observations["ada"]["stable"] == "away"
    page.close()
    clock.advance(60)
    provider.values["family"][0] = raw_member("ada", clock, 5)
    provider.values["friends"][0] = raw_member("ada", clock, 5)
    with engine.lock:
        engine.next_poll = 0
    assert speaker.played.wait(4), "arrival did not play independently of the browser"
    assert len(speaker.records) == 1
    assert speaker.records[0][3] <= 30
    assert engine.active["member_id"] == "ada"


@pytest.mark.parametrize("width", [360, 390, 768, 1024, 1440])
def test_people_controls_fit_and_no_removed_symbol_is_rendered(home_portal, width):
    page, app, clock, provider, speaker = home_portal
    page.set_viewport_size({"width": width, "height": 1000})
    assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth")
    assert "\u2733" not in page.locator("body").inner_text()
    card = page.locator("#people").bounding_box()
    for row in page.locator(".person-actions").all():
        box = row.bounding_box()
        assert box["x"] >= card["x"]
        assert box["x"] + box["width"] <= card["x"] + card["width"]
