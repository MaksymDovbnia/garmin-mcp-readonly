"""Tests that back the safety claims made in the README. No network or credentials."""

import ast
import asyncio
import inspect
import json
import re
from importlib import metadata
from pathlib import Path

import garminconnect
import garminconnect.client as gc_client
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp import server

SRC = Path(server.__file__).parent

# Modules that would let this project's own code talk to the network.
NETWORK_MODULES = {
    "aiohttp", "ftplib", "http", "httpx", "httpx2", "requests", "smtplib",
    "socket", "urllib", "urllib3", "websockets", "curl_cffi",
}

# --------------------------------------------------------------------------
# Static audits. Each has self-tests below, so a detector that stops detecting
# fails loudly instead of quietly passing everything.
# --------------------------------------------------------------------------

WRITE_ATTRS = {"post", "put", "delete", "patch"}
WRITE_VERBS = {"POST", "PUT", "DELETE", "PATCH"}

# The only thing a get_* method may call on the client. Client.connectapi hard-codes GET
# (see TestGarminconnectIsReadOnlyForGetMethods), so anything else is a new code path.
ALLOWED_PRIMITIVES = frozenset({"self.client.connectapi"})


def _writes_directly(fn: ast.AST) -> bool:
    """True if the function calls .post/.put/.delete/.patch or passes an HTTP write verb."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in WRITE_ATTRS:
            return True
        args = [*node.args, *(kw.value for kw in node.keywords)]
        if any(isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.upper() in WRITE_VERBS for a in args):
            return True
    return False


def _attr_chain(node: ast.AST) -> str | None:
    """'self.client.connectapi' for a call target rooted at `self`, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id == "self" and parts:
        return ".".join(["self", *reversed(parts)])
    return None


def unsafe_get_methods(source: str, class_name: str, allowed: frozenset[str] = ALLOWED_PRIMITIVES) -> dict[str, list[str]]:
    """`get_*` methods that reach a write request or any client call outside `allowed`.

    Follows `self.method()` calls within the class and calls to module-level functions
    (decorators included), so a write hidden behind a helper is still found.
    """
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    methods = {f.name: f for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    funcs = {f.name: f for f in tree.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def problems(fn: ast.AST, seen: set[int]) -> set[str]:
        if id(fn) in seen:
            return set()
        seen.add(id(fn))
        found = {"write request"} if _writes_directly(fn) else set()
        # `c = self.client` or getattr(self.client, ...) would hide calls from the chain
        # check below, so a bare use of the client (not followed by .attribute) is flagged.
        parents = {id(child): parent for parent in ast.walk(fn) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and _attr_chain(node) == "self.client":
                parent = parents.get(id(node))
                if not (isinstance(parent, ast.Attribute) and parent.value is node):
                    found.add("self.client used as a value")
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            chain = _attr_chain(node.func)
            if chain is None:
                if isinstance(node.func, ast.Name) and node.func.id in funcs:
                    found |= problems(funcs[node.func.id], seen)
            elif chain.count(".") == 1 and chain[len("self."):] in methods:
                found |= problems(methods[chain[len("self."):]], seen)
            elif chain not in allowed:
                found.add(chain)
        return found

    result = {}
    for name in methods:
        if name.startswith("get_") and (found := problems(methods[name], set())):
            result[name] = sorted(found)
    return result


def _is_os_attr(node: ast.AST, attr: str, os_names: set[str]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id in os_names
    )


def env_reads(source: str) -> set[str]:
    """Environment access in the code: variable names read, plus markers for anything else.

    A non-literal key shows up as '<dynamic>', any other use of os.environ (copying,
    iterating) as '<other use of os.environ>', an aliased `from os import ...` as
    '<aliased os import>', and os.path.expandvars (which reads whatever variables its
    argument names) as '<expandvars>'. `import os as o` is understood.
    """
    tree = ast.parse(source)
    os_names = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names |= {a.asname for a in node.names if a.name == "os" and a.asname}

    def is_environ(n: ast.AST) -> bool:
        return _is_os_attr(n, "environ", os_names) or (isinstance(n, ast.Name) and n.id == "environ")

    def is_getenv(n: ast.AST) -> bool:
        return _is_os_attr(n, "getenv", os_names) or (isinstance(n, ast.Name) and n.id == "getenv")

    def literal(node: ast.AST | None) -> str:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else "<dynamic>"

    names: set[str] = set()
    environ_refs = handled = 0
    for node in ast.walk(tree):
        if is_environ(node):
            environ_refs += 1
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(a.asname and a.name in {"environ", "getenv"} for a in node.names):
                names.add("<aliased os import>")
        elif isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr == "expandvars") or (isinstance(f, ast.Name) and f.id == "expandvars"):
                names.add("<expandvars>")
            via_environ = isinstance(f, ast.Attribute) and is_environ(f.value) and f.attr in {"get", "pop", "setdefault"}
            if via_environ or is_getenv(f):
                handled += via_environ
                names.add(literal(node.args[0] if node.args else None))
        elif isinstance(node, ast.Subscript) and is_environ(node.value):
            handled += 1
            names.add(literal(node.slice))
    if environ_refs != handled:
        names.add("<other use of os.environ>")
    return names


WRITE_STYLE_NAME = re.compile(
    r"^(upload|delete|remove|set|add|create|update|edit|modify|write|insert|append|store|save|log|schedule|unschedule"
    r"|import|post|put|patch|push|submit|publish|sync|share|rename|move|copy|merge|apply|reset|clear|link|unlink|send"
    r"|enable|disable|activate|deactivate|register|invite|accept|reject|revoke|grant|follow|unfollow|subscribe|mark)_"
)


def write_style_calls(source: str) -> set[str]:
    """Names of attribute calls (and getattr string constants) that look like write operations."""
    tree = ast.parse(source)
    names = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr":
            names |= {a.value for a in n.args[1:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)}
    return {n for n in names if WRITE_STYLE_NAME.match(n)}


class TestAuditDetectors:
    AUDIT_SOURCE = '''
class Garmin:
    def get_direct(self): return self.client.post("x")
    def get_via_helper(self): return self._send()
    def _send(self): return self.client.put("x")
    def get_via_verb(self): return self.connectapi("x", method="DELETE")
    def get_other_primitive(self): return self.client.upload_thing()
    def get_other_attribute(self): return self.session.get("x")
    def get_module_helper(self): return _post_it()
    def get_alias(self):
        c = self.client
        return c.upload_thing()
    def get_dynamic(self): return getattr(self.client, "upload_thing")()
    def get_reads_property(self): return self.client.is_authenticated
    def get_clean(self): return self.client.connectapi("x")
    def get_clean_via_helper(self): return self._read()
    def _read(self): return _validate(self.client.connectapi("x"))
    def upload_thing(self): return self.client.post("x")

def _post_it(): return requests.post("x")
def _validate(x): return x
'''

    def test_audit_flags_writes_helpers_and_unexpected_primitives(self):
        flagged = unsafe_get_methods(self.AUDIT_SOURCE, "Garmin")
        assert set(flagged) == {
            "get_direct", "get_via_helper", "get_via_verb", "get_other_primitive", "get_other_attribute",
            "get_module_helper", "get_alias", "get_dynamic",
        }
        assert flagged["get_alias"] == ["self.client used as a value"]
        assert "write request" in flagged["get_via_helper"]
        assert flagged["get_other_primitive"] == ["self.client.upload_thing"]
        assert "write request" in flagged["get_module_helper"]

    def test_audit_accepts_the_allowed_primitive(self):
        flagged = unsafe_get_methods(self.AUDIT_SOURCE, "Garmin")
        for clean in ("get_clean", "get_clean_via_helper", "get_reads_property"):
            assert clean not in flagged

    @pytest.mark.parametrize(
        "source, expected",
        [
            ("import os\nos.environ.get('A')", {"A"}),
            ("import os\nos.environ['B']", {"B"}),
            ("import os\nos.getenv('C', 'd')", {"C"}),
            ("from os import getenv\ngetenv('D')", {"D"}),
            ("import os as o\no.environ.get('E')", {"E"}),
            ("import os as o\no.environ.get(KEY)", {"<dynamic>"}),
            ("import os\nos.environ.get(KEY)", {"<dynamic>"}),
            ("import os\ndict(os.environ)", {"<other use of os.environ>"}),
            ("import os\nos.environ.items()", {"<other use of os.environ>"}),
            ("from os import environ as env\nenv['X']", {"<aliased os import>"}),
            ("import os\nos.path.expandvars('$X')", {"<expandvars>"}),
            ("from os.path import expandvars\nexpandvars(x)", {"<expandvars>"}),
            ("x = 1", set()),
        ],
    )
    def test_env_detector(self, source, expected):
        assert env_reads(source) == expected

    @pytest.mark.parametrize(
        "source, expected",
        [
            ("api.upload_workout({})", {"upload_workout"}),
            ("c = connect(); c.delete_activity(1)", {"delete_activity"}),
            ("getattr(x, 'set_blood_pressure')()", {"set_blood_pressure"}),
            ("c.modify_activity(1); c.sync_device(); c.share_activity(2)", {"modify_activity", "sync_device", "share_activity"}),
            ("x.get_things(); y.update(z); getattr(x, name)", set()),
        ],
    )
    def test_write_style_name_detector(self, source, expected):
        assert write_style_calls(source) == expected


# --------------------------------------------------------------------------
# call_readonly
# --------------------------------------------------------------------------


class FakeGarmin:
    def get_things(self, **kwargs):
        return {"displayName": "someone", "steps": 42, "echo": kwargs}

    get_not_callable = 1


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(server, "connect", lambda: FakeGarmin())


class TestCallReadonly:
    @pytest.mark.parametrize(
        "name",
        ["delete_activity", "upload_activity", "set_blood_pressure", "login", "get", "GET_things", "_get_things"],
    )
    def test_rejects_anything_not_get_prefixed(self, name):
        # Rejected before a client is even created, so this needs no tokens.
        with pytest.raises(ToolError, match="only get_"):
            server.call_readonly(name)

    def test_unknown_method_is_a_clean_error(self, fake_client):
        with pytest.raises(ToolError, match="No such Garmin method"):
            server.call_readonly("get_nothing_here")

    def test_non_callable_attribute_is_rejected(self, fake_client):
        with pytest.raises(ToolError, match="No such Garmin method"):
            server.call_readonly("get_not_callable")

    def test_result_is_scrubbed_and_params_forwarded(self, fake_client):
        out = server.call_readonly("get_things", {"day": "2026-01-01"})
        assert "displayName" not in out
        assert out["steps"] == 42
        assert out["echo"] == {"day": "2026-01-01"}


# --------------------------------------------------------------------------
# Every tool is read-only
# --------------------------------------------------------------------------

# Arguments for one call to each tool. A new tool must be added here, which puts it
# under test_tools_only_touch_get_methods (flags are switched on to cover every branch).
CALL_ARGS = {
    "check_connection": {},
    "list_activities": {},
    "list_activities_by_date": {"start_date": "2026-01-01"},
    "get_activity": {
        "activity_id": "1",
        "include_splits": True,
        "include_hr_zones": True,
        "include_weather": True,
        "include_location": True,
    },
    "daily_summary": {},
    "get_sleep": {},
    "get_hrv": {},
    "get_training_readiness": {},
    "get_training_status": {},
    "get_body_composition": {"start_date": "2026-01-01"},
    "get_steps_range": {"start_date": "2026-01-01"},
    "call_readonly": {"method": "get_things"},
}


class RecordingClient:
    """Stands in for the Garmin client and records every attribute the tools touch."""

    def __init__(self):
        self.accessed: set[str] = set()

    def __getattr__(self, name):
        self.accessed.add(name)
        return lambda *args, **kwargs: {}


@pytest.fixture(scope="module")
def tools():
    return asyncio.run(server.server.list_tools())


class TestEveryToolIsReadOnly:
    def test_tools_are_registered(self, tools):
        assert len(tools) >= 12

    def test_wrapping_keeps_every_description_and_parameter_schema(self, tools):
        for tool in tools:
            params = inspect.signature(getattr(server, tool.name)).parameters
            assert tool.description and tool.description.strip(), tool.name
            assert set(tool.input_schema.get("properties", {})) == set(params), tool.name
            required = {n for n, p in params.items() if p.default is inspect.Parameter.empty}
            assert set(tool.input_schema.get("required", [])) == required, tool.name

    def test_every_tool_is_annotated_read_only(self, tools):
        bad = [t.name for t in tools if not (t.annotations and t.annotations.read_only_hint is True)]
        assert not bad, f"tools without read_only_hint: {bad}"

    def test_no_tool_declares_structured_output(self, tools):
        # Structured output would send every result twice (text + structuredContent).
        assert not [t.name for t in tools if t.output_schema is not None]

    def test_every_tool_has_call_args(self, tools):
        assert set(CALL_ARGS) == {t.name for t in tools}, "add new tools to CALL_ARGS"

    def test_tools_only_touch_get_methods(self, monkeypatch):
        # Behavioural, so it does not depend on how a tool names its client variable
        # or whether it goes through a helper.
        recorder = RecordingClient()
        monkeypatch.setattr(server, "connect", lambda: recorder)
        for name, args in CALL_ARGS.items():
            getattr(server, name)(**args)
        assert len(recorder.accessed) >= 14, "expected the tools to reach their Garmin methods"
        assert all(n.startswith("get_") for n in recorder.accessed), sorted(recorder.accessed)

    def test_tool_code_names_no_write_style_call(self):
        # Complements the behavioural test above: this covers every branch, by name.
        assert write_style_calls((SRC / "server.py").read_text()) == set()

    def test_results_are_sent_once_as_text(self, monkeypatch):
        monkeypatch.setattr(server, "connect", lambda: FakeGarmin())
        result = asyncio.run(server.server.call_tool("call_readonly", {"method": "get_things"}))
        assert result.structured_content is None and len(result.content) == 1


class ValueClient:
    """Every Garmin call returns the same value."""

    def __init__(self, value):
        self.value = value

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.value


def call(monkeypatch, name, args, value):
    monkeypatch.setattr(server, "connect", lambda: ValueClient(value))
    return asyncio.run(server.server.call_tool(name, args))


class TestEveryCallAnswersWithOneJsonBlock:
    """Empty results must not vanish and lists must not fan out into one block per item."""

    def test_every_tool_answers_with_exactly_one_valid_json_block(self, monkeypatch):
        recorder = RecordingClient()
        monkeypatch.setattr(server, "connect", lambda: recorder)
        for name, args in CALL_ARGS.items():
            result = asyncio.run(server.server.call_tool(name, args))
            assert len(result.content) == 1, name
            json.loads(result.content[0].text)
            assert result.structured_content is None, name

    @pytest.mark.parametrize(
        "name, args, value, expected",
        [
            ("get_steps_range", {"start_date": "2026-01-01"}, [], "[]"),
            ("get_training_readiness", {}, [], "[]"),
            ("call_readonly", {"method": "get_x"}, None, "null"),
            ("call_readonly", {"method": "get_x"}, [], "[]"),
        ],
    )
    def test_empty_results_are_explicit(self, monkeypatch, name, args, value, expected):
        result = call(monkeypatch, name, args, value)
        assert [b.text for b in result.content] == [expected]

    def test_a_list_is_one_compact_block_and_keeps_non_ascii(self, monkeypatch):
        acts = [
            {"activityId": i, "activityName": "Пробіжка", "distance": 5000.0, "duration": 1500.0,
             "activityType": {"typeKey": "running"}}
            for i in range(3)
        ]
        result = call(monkeypatch, "list_activities", {}, acts)
        assert len(result.content) == 1
        text = result.content[0].text
        assert len(json.loads(text)) == 3
        assert "Пробіжка" in text and "\n" not in text and ": " not in text


# --------------------------------------------------------------------------
# No network, no other env vars, no telemetry, no writes in the dependency
# --------------------------------------------------------------------------


class TestNoNetworkInOwnCode:
    def test_no_networking_imports(self):
        found = []
        for path in SRC.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    mods = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                else:
                    continue
                found += [f"{path.name}: {m}" for m in mods if m.split(".")[0] in NETWORK_MODULES]
        assert not found, f"networking imports in project code: {found}"

    def test_only_documented_env_var_is_read(self):
        names = set().union(*(env_reads(path.read_text()) for path in SRC.glob("*.py")))
        assert names == {"GARMINTOKENS"}


class TestGarminconnectIsReadOnlyForGetMethods:
    """call_readonly relies on `get_*` meaning read-only in the pinned garminconnect.

    If this fails after a dependency bump, audit the flagged methods before
    releasing: call_readonly may now be able to modify a Garmin account.
    """

    def test_get_methods_reach_the_api_only_through_the_get_primitive(self):
        source = Path(garminconnect.__file__).read_text()
        assert unsafe_get_methods(source, "Garmin") == {}

    def test_every_reachable_get_method_is_covered_by_the_audit(self):
        # call_readonly reaches whatever getattr finds, including inherited or dynamically
        # assigned methods; the audit only reads the class body, so the two must agree.
        source = Path(garminconnect.__file__).read_text()
        cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "Garmin")
        audited = {f.name for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name.startswith("get_")}
        reachable = {n for n in dir(garminconnect.Garmin) if n.startswith("get_")}
        assert reachable == audited, sorted(reachable ^ audited)

    def test_the_allowed_primitive_is_hard_coded_to_get(self):
        source = Path(gc_client.__file__).read_text()
        cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef) and n.name == "Client")
        connectapi = next(f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "connectapi")
        verbs = [
            n.args[0].value
            for n in ast.walk(connectapi)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "_run_request"
            and n.args and isinstance(n.args[0], ast.Constant)
        ]
        assert verbs == ["GET"], "Client.connectapi must issue exactly one request, and it must be a GET"


class TestNoTelemetryExport:
    """The MCP SDK enables OpenTelemetry spans by default; they go nowhere unless an
    OpenTelemetry SDK/exporter is installed. Fail if one ever enters the tree."""

    def test_no_opentelemetry_sdk_or_exporter_installed(self):
        names = {(d.metadata["Name"] or "").lower().replace("_", "-") for d in metadata.distributions()}
        exporting = sorted(n for n in names if n.startswith(("opentelemetry-sdk", "opentelemetry-exporter")))
        assert not exporting, f"telemetry export packages present: {exporting}"


class TestJsonResponse:
    def test_small_results_are_compact_json(self):
        assert server._to_json({"a": [1, 2], "b": None}) == '{"a":[1,2],"b":null}'

    def test_types_json_cannot_hold_still_fail_loudly(self):
        with pytest.raises(TypeError):
            server._to_json({"x": object()})

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_numbers_from_garmin_become_null(self, bad):
        assert server._to_json({"x": bad, "ok": 1.5, "nested": [bad]}) == '{"x":null,"ok":1.5,"nested":[null]}'

    def test_a_lone_surrogate_is_replaced_not_fatal(self):
        half_emoji = json.loads('"Run \\ud83d"')  # what Garmin JSON can hold, e.g. a cut-off emoji
        out = server._to_json({half_emoji: half_emoji})
        out.encode("utf-8")  # must be encodable
        assert json.loads(out) == {"Run \ufffd": "Run \ufffd"}

    def test_oversized_results_become_a_notice_with_a_preview(self):
        out = json.loads(server._to_json(list(range(40_000))))
        assert out["truncated"] is True
        assert out["total_bytes"] > out["limit_bytes"] == server.MAX_RESPONSE_BYTES
        assert out["preview"].startswith("[0,1,2") and len(json.dumps(out)) < 5_000

    def test_the_limit_counts_bytes_not_characters(self):
        text = "я" * 60_000  # 60k characters but 120k bytes in UTF-8
        assert json.loads(server._to_json(text))["truncated"] is True

    def test_a_tool_result_over_the_limit_arrives_as_one_notice_block(self, monkeypatch):
        result = call(monkeypatch, "call_readonly", {"method": "get_x"}, list(range(40_000)))
        assert len(result.content) == 1 and json.loads(result.content[0].text)["truncated"] is True

    def test_async_tools_are_refused_at_registration(self):
        async def not_supported():
            return 1

        with pytest.raises(TypeError, match="sync tools only"):
            server.read_tool(not_supported)
