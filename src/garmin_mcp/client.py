"""Garmin Connect session handling.

Authentication here is token-only by design: this module has no parameter, env
var or config path that accepts a password. Tokens are minted once by
``garmin-mcp-auth``, which the user runs themselves in their own terminal, and
are read back from the directory named by ``GARMINTOKENS``.
"""

from __future__ import annotations

import os
from pathlib import Path

from garminconnect import Garmin
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_TOKEN_DIR = "~/.garminconnect"

_SETUP_HINT = (
    "No usable Garmin tokens found in {path}.\n"
    "Run `garmin-mcp-auth` in a terminal once to sign in and store tokens, "
    "then retry. The server never handles your password itself."
)


class AuthError(ToolError):
    """Raised when no usable token store is available.

    Subclasses ToolError so the setup hint reaches the caller intact instead of
    being flattened into a generic "unexpected exception".
    """


def token_dir() -> Path:
    """Directory holding the OAuth tokens."""
    return Path(os.environ.get("GARMINTOKENS") or DEFAULT_TOKEN_DIR).expanduser()


_client: Garmin | None = None


def connect() -> Garmin:
    """Return a logged-in client, reusing the session across tool calls."""
    global _client
    if _client is not None:
        return _client

    path = token_dir()
    if not path.is_dir() or not any(path.iterdir()):
        raise AuthError(_SETUP_HINT.format(path=path))

    garmin = Garmin()
    try:
        garmin.login(str(path))
    except Exception as exc:  # noqa: BLE001 - surfaced as a user-facing hint
        # Deliberately report only the exception type and a short message:
        # the underlying error can quote token material.
        raise AuthError(
            f"Stored Garmin tokens were rejected ({type(exc).__name__}: "
            f"{str(exc)[:160]}).\nRe-run `garmin-mcp-auth` to refresh them."
        ) from exc

    _client = garmin
    return _client


def reset() -> None:
    """Drop the cached session so the next call re-reads the token store."""
    global _client
    _client = None
