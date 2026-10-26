from email.message import Message
import io
import json
import ssl
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import HTTPSHandler

import pytest

from portal.life360 import BASE_URL, MAX_RESPONSE_BYTES, Life360Client, Life360Error, NoRedirect


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def read(self, limit):
        return self.payload[:limit]


@pytest.fixture
def client():
    client = Life360Client("FAKE_TOKEN_NEVER_SENT_TO_NETWORK")
    client.opener.open = Mock()
    return client


def test_transport_stays_on_fixed_https_host_and_uses_get(client):
    client.opener.open.return_value = Response({"circles": []})
    assert client.circles() == []
    request = client.opener.open.call_args.args[0]
    assert request.full_url == BASE_URL + "/v4/circles"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer FAKE_TOKEN_NEVER_SENT_TO_NETWORK"
    assert client.opener.open.call_args.kwargs["timeout"] == 20
    assert NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example") is None
    handler = next(handler for handler in client.opener.handlers if isinstance(handler, HTTPSHandler))
    assert handler._context.verify_mode == ssl.CERT_REQUIRED
    assert handler._context.check_hostname is True


@pytest.mark.parametrize("method,key,path", [
    ("members", "members", "/v3/circles/family/members"),
    ("places", "places", "/v3/circles/family/places"),
])
def test_member_and_place_paths_are_read_only(client, method, key, path):
    client.opener.open.return_value = Response({key: []})
    assert getattr(client, method)("family") == []
    assert client.opener.open.call_args.args[0].full_url == BASE_URL + path


def test_places_only_falls_back_to_json_suffix_on_404(client):
    client.opener.open.side_effect = [HTTPError(BASE_URL, 404, "missing", {}, io.BytesIO()), Response({"places": []})]
    assert client.places("family") == []
    assert client.opener.open.call_args.args[0].full_url.endswith("/places.json")


@pytest.mark.parametrize("code,status", [(401, "reauth_required"), (403, "blocked"), (429, "rate_limited"), (500, "error"), (302, "error")])
def test_errors_are_sanitized_and_rate_limit_is_preserved(client, code, status):
    headers = Message()
    headers["Retry-After"] = "7200"
    client.opener.open.side_effect = HTTPError(BASE_URL, code, "PRIVATE_SERVER_MESSAGE", headers, io.BytesIO(b"SECRET_BODY"))
    with pytest.raises(Life360Error) as error:
        client.places("family")
    assert error.value.status == status
    assert "PRIVATE_SERVER_MESSAGE" not in str(error.value)
    assert "SECRET_BODY" not in str(error.value)
    assert "FAKE_TOKEN" not in str(error.value)
    assert client.opener.open.call_count == 1
    if code == 429:
        assert error.value.retry_after == "7200"


@pytest.mark.parametrize("circle", ["../other", "family/../other", "https://other.example", "", None])
def test_external_ids_cannot_escape_the_api_path(client, circle):
    with pytest.raises(ValueError):
        client.members(circle)
    client.opener.open.assert_not_called()


def test_malformed_lists_and_oversized_responses_are_rejected(client):
    client.opener.open.return_value = Response({"members": "not a list"})
    with pytest.raises(Life360Error):
        client.members("family")
    response = Response({})
    response.payload = b" " * (MAX_RESPONSE_BYTES + 1)
    client.opener.open.return_value = response
    with pytest.raises(Life360Error):
        client.circles()


def test_profile_uses_same_secure_client_and_returns_no_credential(client):
    client.opener.open.return_value = Response({"user": {"id": "owner", "firstName": "Test", "lastName": "Resident"}})
    assert client.profile() == {"account_name": "Test Resident", "account_id": "owner"}
