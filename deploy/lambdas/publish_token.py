"""
Signed publish links, shared by the Notifier (which puts a link in each
completion email) and the Publisher (which verifies it).

A token is  base64url(json {"v": video_id, "exp": unix_seconds}) + "." +
base64url(HMAC-SHA256 of that payload). Anyone can read it, but nobody can
forge or alter one without the signing key (Secrets Manager). A token only
ever authorizes publishing its own article, and the Publisher records
publication in DynamoDB so each article is published at most once.
"""
import base64
import hashlib
import hmac
import json
import time

DEFAULT_TTL_DAYS = 30


class InvalidToken(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def make_token(key: bytes, video_id: str, ttl_days: float = DEFAULT_TTL_DAYS, now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + ttl_days * 86400)
    payload = _b64(json.dumps({"v": video_id, "exp": exp}, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_sign(key, payload)}"


def read_token(key: bytes, token: str, now: float | None = None) -> str:
    """The video_id the token authorizes; raises InvalidToken otherwise."""
    try:
        payload, signature = (token or "").strip().split(".")
    except ValueError:
        raise InvalidToken("malformed link")
    if not hmac.compare_digest(signature, _sign(key, payload)):
        raise InvalidToken("this link isn't valid")
    try:
        data = json.loads(_unb64(payload))
        video_id, exp = str(data["v"]), int(data["exp"])
    except (ValueError, KeyError, TypeError):
        raise InvalidToken("malformed link")
    if (now if now is not None else time.time()) > exp:
        raise InvalidToken("this link has expired")
    return video_id
