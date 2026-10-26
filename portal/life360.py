"""Read-only, fixed-host Life360 transport. The API is unofficial and may change.

Credentials are never put in URLs or returned to clients. TLS remains verified,
redirects are refused, and no location-update requests are sent to people's phones.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPSHandler, HTTPRedirectHandler, build_opener

BASE_URL = "https://api-cloudfront.life360.com"
USER_AGENT = "com.life360.android.safetymapd/KOKO/23.50.0 android/13"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class Life360Error(Exception):
    def __init__(self, message, status="error", http_status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_token(value):
    if not isinstance(value, str):
        raise ValueError("Paste an access token, not a password.")
    value = value.strip().strip("\"'").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not 10 <= len(value) <= 16384 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("Paste the token alone, optionally prefixed with Bearer.")
    return value


def identifier(value):
    """Only IDs returned by Life360 are used to construct its fixed API paths."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Life360 returned an invalid identifier.")
    return value


def retry_after_seconds(value, now=None, minimum=60):
    try:
        delay = float(value or minimum)
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - (datetime.now(timezone.utc).timestamp() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            delay = minimum
    return max(minimum, delay if math.isfinite(delay) else minimum)


class Life360Client:
    def __init__(self, token):
        self.token = normalize_token(token)
        # Do not share an SSLContext with unrelated clients that might mutate ALPN.
        self.opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirect())

    def get(self, path):
        if not path.startswith(("/v3/", "/v4/")) or any(part in path for part in ("..", "?", "#", "://")):
            raise ValueError("Invalid Life360 API path.")
        request = Request(BASE_URL + path, headers={
            "Authorization": "Bearer " + self.token,
            "Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": USER_AGENT,
        }, method="GET")
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise Life360Error("Life360 returned an unexpectedly large response.")
            payload = json.loads(raw)
            if not isinstance(payload, (dict, list)):
                raise Life360Error("Life360 returned an unexpected response.")
            return payload
        except HTTPError as error:
            code = error.code
            retry = error.headers.get("Retry-After") if error.headers else None
            error.close()
            if code == 401:
                raise Life360Error("This token was rejected. Sign in to Life360 and copy a new access token.",
                                   "reauth_required", code) from None
            if code == 403:
                raise Life360Error("Life360 refused this request (403). This can be temporary client blocking, not an expired token. Your saved token has not been removed.",
                                   "blocked", code) from None
            if code == 429:
                raise Life360Error("Life360 is rate limiting requests. Requests are paused before trying again.",
                                   "rate_limited", code, retry) from None
            raise Life360Error(f"Life360 returned HTTP {code}. Your saved token has not been removed.",
                               "error", code) from None
        except (URLError, socket.timeout, TimeoutError, ssl.SSLError, OSError):
            raise Life360Error("Could not reach Life360. Check the server's internet connection and try again later.",
                               "offline") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise Life360Error("Life360 returned an unreadable response. Your saved token has not been removed.") from None

    def profile(self):
        payload = self.get("/v3/users/me")
        user = payload.get("user", payload) if isinstance(payload, dict) else None
        if not isinstance(user, dict) or not any(key in user for key in ("id", "firstName", "lastName")):
            raise Life360Error("Life360 did not return an account profile. The token was not saved.")
        name = " ".join(str(user.get(key) or "") for key in ("firstName", "lastName")).strip()
        return {"account_name": name[:120] or "Life360 account", "account_id": str(user.get("id") or "")[:128]}

    def _list(self, path, key):
        payload = self.get(path)
        values = payload.get(key) if isinstance(payload, dict) else payload
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            raise Life360Error(f"Life360 returned an unexpected {key} list. No saved settings were replaced.")
        return values

    def circles(self):
        return self._list("/v4/circles", "circles")

    def members(self, circle_id):
        return self._list(f"/v3/circles/{identifier(circle_id)}/members", "members")

    def places(self, circle_id):
        path = f"/v3/circles/{identifier(circle_id)}/places"
        try:
            return self._list(path, "places")
        except Life360Error as error:
            if error.http_status != 404:
                raise
            # Older deployments expose this read-only resource with a .json suffix.
            return self._list(path + ".json", "places")


def validate_token(token):
    return Life360Client(token).profile()
