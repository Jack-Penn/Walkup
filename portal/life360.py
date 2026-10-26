"""Read-only Life360 token validation, using the same transport as the test script.

This is an unofficial API. No automatic login or refresh-token exchange is claimed.
Authorization is sent only to a fixed HTTPS host, and never follows redirects.
"""
import json
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, HTTPSHandler, HTTPRedirectHandler, build_opener

BASE_URL = "https://api-cloudfront.life360.com"
USER_AGENT = "com.life360.android.safetymapd/KOKO/23.50.0 android/13"


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


def validate_token(token):
    context = ssl.create_default_context()
    opener = build_opener(HTTPSHandler(context=context), NoRedirect())
    request = Request(BASE_URL + "/v3/users/me", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json", "Cache-Control": "no-cache",
        "User-Agent": USER_AGENT,
    }, method="GET")
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise Life360Error("Life360 returned an unexpectedly large response.")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise Life360Error("Life360 returned an unexpected response.")
        user = payload.get("user", payload)
        if not isinstance(user, dict) or not any(key in user for key in ("id", "firstName", "lastName")):
            raise Life360Error("Life360 did not return an account profile. The token was not saved.")
        name = " ".join(str(user.get(key) or "") for key in ("firstName", "lastName")).strip()
        return {"account_name": name[:120] or "Life360 account"}
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
            message = "Life360 is rate limiting requests. Wait before testing again."
            if retry:
                message += " The server supplied a Retry-After value."
            raise Life360Error(message, "rate_limited", code, retry) from None
        raise Life360Error(f"Life360 returned HTTP {code}. Your saved token has not been removed.",
                           "error", code) from None
    except (URLError, socket.timeout, TimeoutError, ssl.SSLError):
        raise Life360Error("Could not reach Life360. Check the server's internet connection and try again later.",
                           "offline") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Life360Error("Life360 returned an unreadable response. Your saved token has not been removed.") from None
