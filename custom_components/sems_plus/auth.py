"""SEMS+ request signing and authentication headers."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

_EmptyLoginToken = '{"uid":"","timestamp":0,"token":"","client":"semsPlusWeb","version":"","language":"en"}'


def hash_password(password: str) -> str:
    """Return the SEMS+ password encoding (base64 of the MD5 hex digest)."""
    # MD5 is required by the SEMS+ API protocol; usedforsecurity=False avoids
    # failures on FIPS-enabled systems where MD5 is disabled for security use.
    md5_password = hashlib.md5(
        password.encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return base64.b64encode(md5_password.encode("utf-8")).decode("utf-8")


def generate_signature(token_data: dict[str, Any]) -> str:
    """Return the X-Signature header value for a request."""
    epoch_ms = round(time.time() * 1000)
    digest = hashlib.sha256(
        f"{epoch_ms}@{token_data.get('uid', '')}@{token_data.get('token', '')}".encode()
    ).hexdigest()
    sig = f"{digest}@{epoch_ms}"
    return base64.b64encode(sig.encode()).decode()


def login_headers() -> dict[str, str]:
    """Build request headers for the login call."""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, */*;q=0.5",
        "Token": _EmptyLoginToken,
        "X-Signature": generate_signature({}),
    }


def authenticated_headers(token_data: dict[str, Any]) -> dict[str, str]:
    """Build request headers for authenticated gateway calls."""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "token": json.dumps(token_data),
        "X-Signature": generate_signature(token_data),
    }
