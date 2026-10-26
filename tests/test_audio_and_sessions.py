from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
import re
import shutil
import threading
import wave
from unittest.mock import Mock

import pytest
import app as portal_app
from portal.audio import Player
from portal.life360 import Life360Error


def authenticated_client(application):
    client = application.test_client()
    page = client.get('/login').get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    page = client.post('/login', data={'password': application.config['PORTAL_PASSWORD'], 'csrf': csrf}, follow_redirects=True).get_data(as_text=True)
    csrf = re.search(r'name="portal-csrf" content="([^"]+)"', page).group(1)
    return client, {'X-Portal-CSRF': csrf}


def test_password_change_invalidates_previous_login(tmp_path):
    config = {'TESTING': True, 'DATA_DIR': str(tmp_path), 'PORTAL_PASSWORD': 'original-password', 'SEED_DEMO': False}
    first = portal_app.create_app(config)
    client, headers = authenticated_client(first)
    cookie = client.get_cookie('session').value
    config['PORTAL_PASSWORD'] = 'a-different-password'
    second = portal_app.create_app(config)
    other = second.test_client()
    other.set_cookie('session', cookie)
    assert other.get('/api/state').status_code == 401


def test_retry_after_http_date_is_respected(tmp_path, monkeypatch):
    application = portal_app.create_app({'TESTING': True, 'DATA_DIR': str(tmp_path), 'PORTAL_PASSWORD': 'test-password', 'SEED_DEMO': False})
    client, headers = authenticated_client(application)
    application.extensions['store'].set_token('FAKE_TOKEN_ONLY_FOR_TESTS')
    retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    def limited(token):
        raise Life360Error('Wait a little', 'rate_limited', 429, retry_at)
    monkeypatch.setattr(portal_app, 'validate_token', limited)
    assert client.post('/api/life360/test', json={}, headers=headers).status_code == 502
    response = client.post('/api/life360/test', json={}, headers=headers)
    assert response.status_code == 429
    assert response.json['retry_after'] >= 118


def test_ffplay_zero_exit_with_audio_failure_is_not_reported_as_success():
    events = []
    player = Player(lambda *args: events.append(args))
    process = Mock(returncode=0)
    process.communicate.return_value = (None, b'No more combinations to try, audio open failed')
    player.generation = 1
    player.process = process
    player._watch(process, 1)
    assert player.state()['error'] is not None
    assert events[-1][0] == 'error'


@pytest.mark.skipif(not shutil.which('ffplay'), reason='ffplay is optional on development computers')
def test_real_ffplay_process_with_dummy_audio_output(tmp_path, monkeypatch):
    monkeypatch.setenv('SDL_AUDIODRIVER', 'dummy')
    path = tmp_path / 'short.wav'
    with wave.open(str(path), 'wb') as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(b'\0\0' * 16000)
    finished = threading.Event()
    events = []
    def record(*args):
        events.append(args)
        if args[0] in {'complete', 'error'}:
            finished.set()
    player = Player(record)
    try:
        player.play(path, {'id': 'local-test', 'title': 'Local test'}, .2, .25, 50)
        assert finished.wait(timeout=8), 'ffplay did not finish'
        assert player.state()['error'] is None
        assert events[-1][0] == 'complete'
    finally:
        player.stop(log=False)
