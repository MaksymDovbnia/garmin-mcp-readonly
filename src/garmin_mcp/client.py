"""Garmin Connect session handling.

Authentication here is token-only by design: this module has no parameter, env
var or config path that accepts a password. Tokens are minted once by
``garmin-mcp-auth``, which the user runs themselves in their own terminal, and
are read back from the directory named by ``GARMINTOKENS``.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_TOKEN_DIR = "~/.garminconnect"

# How long a call waits for its turn before giving up. Kept well under the 60 s tool
# timeout common in MCP clients, so a queued call fails with our message, not theirs.
_QUEUE_WAIT_S = 30.0

# Most specific first: NotFound is a subclass of ConnectionError.
_ERROR_HINTS: tuple[tuple[type[Exception], str, str], ...] = (
    (GarminConnectNotFoundError, "Garmin has no such item", "Check the id or date."),
    (
        GarminConnectAuthenticationError,
        "Garmin rejected the stored tokens",
        "Re-run `garmin-mcp-auth` to refresh them.",
    ),
    (GarminConnectTooManyRequestsError, "Garmin rate limit hit", "Wait a few minutes, then retry."),
    (
        GarminConnectConnectionError,
        "Garmin Connect request failed",
        "Garmin may be unreachable; check your network or retry later.",
    ),
)

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
    """Directory holding the OAuth tokens.

    ``${HOME}`` is expanded here because not every MCP client does it; no other
    ``${...}`` reference is ever expanded (a leading ``~`` is resolved by pathlib as
    usual). A value still containing ``${`` afterwards is an unresolved placeholder for
    an unset optional setting, so it means "use the default", not a literal path.
    """
    raw = os.environ.get("GARMINTOKENS", "")
    if "${HOME}" in raw:  # only then is a home directory needed at all
        raw = raw.replace("${HOME}", str(Path.home()))
    if not raw or "${" in raw:
        raw = DEFAULT_TOKEN_DIR
    return Path(raw).expanduser()


def _brief(exc: Exception) -> str:
    """Exception type plus a short message; a full upstream message can quote token material."""
    return f"{type(exc).__name__}: {str(exc)[:160]}"


class _HintedClient:
    """Wraps the Garmin client so its known errors reach the model with an actionable hint.

    Only the exceptions listed in ``_ERROR_HINTS`` are rewritten, and only their type
    and a short message are forwarded. Any other exception is re-raised untouched.
    Request time limits are the library's own (15 s per API request).

    The SDK runs tools on worker threads and agentic clients call several at once, but
    the Garmin session is shared, so requests are made one at a time. A call that cannot
    get its turn within ``queue_wait`` seconds fails with a clear message.
    """

    def __init__(self, client: Garmin, queue_wait: float = _QUEUE_WAIT_S) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._queue_wait = queue_wait

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)  # AttributeError for unknown names is kept
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if not self._lock.acquire(timeout=self._queue_wait):
                raise ToolError(
                    f"Another Garmin request is still running (waited {self._queue_wait:g}s "
                    "for its turn). Retry shortly."
                )
            try:
                return attr(*args, **kwargs)
            except Exception as exc:
                for exc_type, prefix, hint in _ERROR_HINTS:
                    if isinstance(exc, exc_type):
                        raise ToolError(f"{prefix} ({_brief(exc)}). {hint}") from None
                raise
            finally:
                self._lock.release()

        return call


_client: _HintedClient | None = None
_connect_lock = threading.Lock()


def connect() -> _HintedClient:
    """Return a logged-in client, reusing the session across tool calls.

    Tools run on worker threads, so the first calls can arrive together; the lock makes
    exactly one of them sign in and write the token file.
    """
    global _client
    with _connect_lock:
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
                f"Stored Garmin tokens were rejected ({_brief(exc)}).\n"
                "Re-run `garmin-mcp-auth` to refresh them."
            ) from exc

        _client = _HintedClient(garmin)
        return _client


def reset() -> None:
    """Drop the cached session so the next call re-reads the token store."""
    global _client
    with _connect_lock:
        _client = None
