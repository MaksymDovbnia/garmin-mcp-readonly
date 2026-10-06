"""Tests for the error-hint wrapper around the Garmin client. No network or credentials."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp import client as client_module
from garmin_mcp.client import DEFAULT_TOKEN_DIR, _HintedClient, token_dir


class FakeClient:
    unit_system = "metric"

    def get_ok(self, a, b=0):
        return {"sum": a + b}

    def get_raises(self):
        raise self.exc


def hinted(exc=None):
    client = FakeClient()
    client.exc = exc
    return _HintedClient(client)


class TestPassthrough:
    def test_forwards_args_kwargs_and_return_value(self):
        assert hinted().get_ok(1, b=2) == {"sum": 3}

    def test_non_callable_attributes_pass_through(self):
        assert hinted().unit_system == "metric"

    def test_unknown_attribute_still_raises_attribute_error(self):
        # call_readonly relies on getattr(..., None) returning None for these.
        assert getattr(hinted(), "get_nothing", None) is None

    def test_unrecognised_exceptions_are_re_raised_untouched(self):
        original = ValueError("boom")
        with pytest.raises(ValueError) as info:
            hinted(original).get_raises()
        assert info.value is original and not original.__suppress_context__


class TestErrorTranslation:
    @pytest.mark.parametrize(
        "exc, expected",
        [
            (GarminConnectAuthenticationError("401"), "garmin-mcp-auth"),
            (GarminConnectTooManyRequestsError("429"), "rate limit"),
            (GarminConnectConnectionError("dns"), "unreachable"),
            (GarminConnectNotFoundError("404"), "no such item"),
        ],
    )
    def test_known_errors_become_tool_errors_with_a_hint(self, exc, expected):
        with pytest.raises(ToolError) as info:
            hinted(exc).get_raises()
        assert expected.lower() in str(info.value).lower()

    def test_not_found_is_not_reported_as_unreachable(self):
        # NotFound subclasses ConnectionError; the more specific hint must win.
        with pytest.raises(ToolError) as info:
            hinted(GarminConnectNotFoundError("404")).get_raises()
        assert "unreachable" not in str(info.value)

    def test_message_is_truncated_and_original_is_not_chained(self):
        with pytest.raises(ToolError) as info:
            hinted(GarminConnectAuthenticationError("SECRET" + "x" * 500)).get_raises()
        assert len(str(info.value)) < 300, "long upstream messages must be cut"
        assert info.value.__cause__ is None and info.value.__suppress_context__


class TestTokenDir:
    DEFAULT = Path(DEFAULT_TOKEN_DIR).expanduser()

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("GARMINTOKENS", raising=False)
        assert token_dir() == self.DEFAULT

    def test_default_when_empty(self, monkeypatch):
        monkeypatch.setenv("GARMINTOKENS", "")
        assert token_dir() == self.DEFAULT

    def test_explicit_path_is_used(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GARMINTOKENS", str(tmp_path / "tokens"))
        assert token_dir() == tmp_path / "tokens"

    def test_home_reference_is_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GARMINTOKENS", "${HOME}/tokens")
        assert token_dir() == tmp_path / "tokens"

    def test_unresolved_client_placeholder_means_default(self, monkeypatch):
        # Some MCP clients pass the literal placeholder when an optional setting is blank.
        monkeypatch.setenv("GARMINTOKENS", "${user_config.token_path}")
        assert token_dir() == self.DEFAULT

    def test_absolute_path_needs_no_home_directory(self, monkeypatch, tmp_path):
        def no_home():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(client_module.Path, "home", staticmethod(no_home))
        monkeypatch.setenv("GARMINTOKENS", str(tmp_path / "tokens"))
        assert token_dir() == tmp_path / "tokens"

    def test_only_home_is_expanded_never_other_variables(self, monkeypatch):
        monkeypatch.setenv("SOME_SECRET", "hunter2")
        monkeypatch.setenv("GARMINTOKENS", "${SOME_SECRET}/tokens")
        assert token_dir() == self.DEFAULT
        assert "hunter2" not in str(token_dir())


class TestQueueing:
    def test_a_call_that_cannot_get_its_turn_fails_clearly_and_releases_nothing_it_never_held(self):
        release = threading.Event()
        holding = threading.Event()

        class Blocker:
            def get_block(self):
                holding.set()  # runs only after the caller has taken its turn
                release.wait(5)

            def get_quick(self):
                return 1

        shared = _HintedClient(Blocker(), queue_wait=0.05)
        holder = threading.Thread(target=shared.get_block)
        holder.start()
        assert holding.wait(2), "the holder never started"
        try:
            with pytest.raises(ToolError, match="still running"):
                shared.get_quick()
        finally:
            release.set()
            holder.join()
        assert shared.get_quick() == 1

    def test_the_turn_is_released_after_an_error(self):
        failing = FakeClient()
        failing.exc = GarminConnectAuthenticationError("x")
        shared = _HintedClient(failing, queue_wait=0.2)
        with pytest.raises(ToolError):
            shared.get_raises()
        assert shared.get_ok(1) == {"sum": 1}


class SlowLoginGarmin:
    logins = 0

    def login(self, path):
        time.sleep(0.05)  # widen the window in which a second thread could also sign in
        type(self).logins += 1


class TestConcurrency:
    @pytest.fixture(autouse=True)
    def _isolated_module_state(self, monkeypatch, tmp_path):
        (tmp_path / "garmin_tokens.json").write_text("{}")
        monkeypatch.setenv("GARMINTOKENS", str(tmp_path))
        monkeypatch.setattr(client_module, "Garmin", SlowLoginGarmin)
        SlowLoginGarmin.logins = 0
        client_module.reset()
        yield
        client_module.reset()

    def test_simultaneous_first_calls_sign_in_exactly_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            clients = list(pool.map(lambda _: client_module.connect(), range(8)))
        assert SlowLoginGarmin.logins == 1
        assert all(c is clients[0] for c in clients)

    def test_garmin_requests_are_made_one_at_a_time(self):
        state = {"active": 0, "max": 0}
        guard = threading.Lock()

        class Slow:
            def get_slow(self):
                with guard:
                    state["active"] += 1
                    state["max"] = max(state["max"], state["active"])
                time.sleep(0.03)
                with guard:
                    state["active"] -= 1

        shared = _HintedClient(Slow())
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: shared.get_slow(), range(6)))
        assert state["max"] == 1
