# garmin-mcp-readonly

**English** · [Українська](README.uk.md)

A small [MCP](https://modelcontextprotocol.io) server that lets an AI assistant **read** your own Garmin Connect training and health data: activities, sleep, HRV, training readiness, body composition, steps.

It runs as a local process on your machine, signs in with OAuth tokens you minted yourself, and never asks for or stores your password. It works with any MCP client that can launch a local stdio server — setup instructions below for **Claude Code, Claude Desktop, OpenAI Codex and Google Antigravity**.

> **Unofficial.** This project is not affiliated with or endorsed by Garmin. It talks to Garmin Connect through the community [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library, which uses undocumented endpoints. Those can change or break without notice, and you should check Garmin's terms of service yourself before using it.

## Contents

- [How it works](#how-it-works)
- [Install](#install)
- [Connect to your AI client](#connect-to-your-ai-client)
- [Tools](#tools)
- [Why read-only](#why-read-only)
- [Privacy and security](#privacy-and-security)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## How it works

```
You: "How did my run go yesterday?"
   │
   ▼
Your AI client (MCP client) ──stdin/stdout, JSON-RPC──▶ garmin-mcp (local Python process)
                                                          │ server.py    picks the tool, parses dates, summarises
                                                          │ client.py    reads OAuth tokens from ~/.garminconnect
                                                          │ garminconnect ──HTTPS──▶ *.garmin.com
                                                          │ privacy.py   strips identifiers and GPS
   ◀───────────────── compact result ─────────────────────┘
```

| File | Role |
|---|---|
| `src/garmin_mcp/auth.py` (`garmin-mcp-auth`) | One-time sign-in, run by you in a terminal. Prompts for email, password (hidden) and MFA code, exchanges them for OAuth tokens, then discards the password. |
| `src/garmin_mcp/client.py` | Loads the tokens, keeps one Garmin session for the life of the process, makes requests one at a time (a call waits at most 30 s for its turn), and turns known Garmin errors into short hints. It has no parameter, environment variable or config path that accepts a password. |
| `src/garmin_mcp/server.py` | The MCP tools. Normalises dates (`today`, `yesterday`, `YYYY-MM-DD`) and reduces Garmin's ~100 fields per activity to the ~15 that answer real questions. |
| `src/garmin_mcp/privacy.py` | `scrub()` recursively removes known account/device identifier keys and GPS coordinates from every payload. |
| `tests/` | Unit tests, including tests that back the safety claims in this README. No network or credentials needed. |

The transport is **stdio**: your AI client launches the process and talks to it over its standard input/output. The server opens no network port.

## Install

Requires Python 3.12+.

```bash
git clone https://github.com/MaksymDovbnia/garmin-mcp-readonly.git
cd garmin-mcp-readonly
```

Install with [uv](https://docs.astral.sh/uv/) (recommended — it installs the exact dependency versions recorded in `uv.lock`):

```bash
uv sync --locked
```

or with plain pip (which resolves dependency versions itself):

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

Either way the executables end up in `.venv/bin/`. The repository is called `garmin-mcp-readonly`, but the commands it installs are `garmin-mcp` (the server) and `garmin-mcp-auth` (the one-time sign-in).

Then sign in once, **in your own terminal** (never through an AI assistant):

```bash
.venv/bin/garmin-mcp-auth
```

It asks for your Garmin email, your password (hidden) and, if your account uses it, an MFA code. The password is used once to obtain OAuth tokens and is never written to disk, config or the environment. Tokens are saved to `~/.garminconnect` (override with `GARMINTOKENS`). Garmin controls how long they stay valid; if the server later reports that they were rejected, run `garmin-mcp-auth` again.

> Pin what you run. Clone a specific tag or commit rather than tracking a moving branch, and review the roughly 700 lines in `src/` before you trust it with a session for your Garmin account.

## Connect to your AI client

Every client needs the same two things:

- **`command`**: the **absolute** path to the `garmin-mcp` executable in your virtualenv — `/path/to/garmin-mcp-readonly/.venv/bin/garmin-mcp` below, where `/path/to/garmin-mcp-readonly` is wherever you cloned the repo. No arguments.
- **(optional) `GARMINTOKENS`**: only if your tokens are not in the default `~/.garminconnect`.

Name the server `garmin`. After connecting, ask the assistant to "check my Garmin connection" (the `check_connection` tool) to confirm it works.

> **What has been tested.** The project is developed and used with Claude Code and Claude Desktop on macOS. The Codex and Antigravity sections follow each vendor's documentation and have **not** been run by the author; if something differs on your version, please open an issue.

### Claude Code

```bash
claude mcp add --scope user garmin -- /path/to/garmin-mcp-readonly/.venv/bin/garmin-mcp
```

`--scope user` makes it available in every project; drop it to register for the current project only. Check it:

```bash
claude mcp list
```

Inside a Claude Code session, `/mcp` shows the server and its status. With a custom token directory, options go before the server name:

```bash
claude mcp add --scope user -e GARMINTOKENS=/path/to/token-dir garmin -- /path/to/garmin-mcp-readonly/.venv/bin/garmin-mcp
```

### Claude Desktop

Edit the config file (create it if it doesn't exist):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/path/to/garmin-mcp-readonly/.venv/bin/garmin-mcp"
    }
  }
}
```

On Windows the executable is typically `.venv\Scripts\garmin-mcp.exe`. Fully quit and reopen Claude Desktop afterwards. For a custom token directory add `"env": { "GARMINTOKENS": "/path/to/token-dir" }` next to `"command"`.

`manifest.json` is an experimental MCPB (Claude Desktop extension) descriptor. It passes the MCPB schema validator but has not been installed by the author, and it declares macOS only, the one platform the project has been exercised on. The config-file route above is the supported one.

### OpenAI Codex

The Codex CLI, the IDE extension and the ChatGPT desktop app share one configuration file, `~/.codex/config.toml`. Add the server with the CLI:

```bash
codex mcp add garmin -- /path/to/garmin-mcp-readonly/.venv/bin/garmin-mcp
codex mcp list
```

or edit `~/.codex/config.toml` directly:

```toml
[mcp_servers.garmin]
command = "/path/to/garmin-mcp-readonly/.venv/bin/garmin-mcp"
```

Notes:

- **Custom token directory:** `codex mcp add garmin --env GARMINTOKENS=/path/to/token-dir -- /path/to/garmin-mcp-readonly/.venv/bin/garmin-mcp`, or add a `[mcp_servers.garmin.env]` table with `GARMINTOKENS = "/path/to/token-dir"`.
- **Use the global file.** A project-scoped `.codex/config.toml` only applies to trusted projects, and the path to your virtualenv is personal.
- **Status:** in the Codex TUI, `/mcp` lists the active MCP servers.
- **Approvals:** according to Codex's documentation, `default_tools_approval_mode = "writes"` (set inside `[mcp_servers.garmin]`) prompts only for tools that are not marked read-only. Every tool of this server is marked read-only, so that mode skips the prompts while still asking for anything else.
- **Timeouts:** Codex defaults to 10 s for startup and 60 s per tool call (`startup_timeout_sec`, `tool_timeout_sec`). The server starts in about a second and signs in on the first tool call, not at startup.

### Google Antigravity

In the agent panel open the `…` menu → **MCP Servers** → **Manage MCP Servers** → **View raw config**, and add the server to the `mcp_config.json` that opens:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/path/to/garmin-mcp-readonly/.venv/bin/garmin-mcp"
    }
  }
}
```

Notes:

- **Where the file lives:** Antigravity's documentation names `~/.gemini/config/mcp_config.json` (global, shared by the IDE and the CLI) and `.agents/mcp_config.json` (per workspace). Opening it from the UI avoids guessing the path for your version.
- **Plain JSON:** no comments, no trailing commas.
- **Environment variables:** you normally don't need any — the default token directory is used. Antigravity's documentation doesn't describe `${VAR}` substitution in this file, and one user report says `env` values in the global config were not applied, so if you must set `GARMINTOKENS`, write a literal absolute path.
- **Reload:** reload the server list from the MCP manager (`/mcp`) or restart Antigravity after editing.
- **Which model sees your data:** results from the tools go to whichever model Antigravity is using for that conversation (see [Privacy and security](#privacy-and-security)).

### Other MCP clients

Any client that can launch a local stdio MCP server works. Most accept the same shape as the Claude Desktop and Antigravity examples above: a server named `garmin` with `command` set to the absolute path of `garmin-mcp`, and optionally an `env` with `GARMINTOKENS`.

### Try it

Ask your assistant things like:

- "Check my Garmin connection."
- "List my last 5 runs and compare their pace."
- "How did I sleep last night, and how is my HRV against baseline?"
- "Show my training readiness and training status for today."
- "Plot my step counts for the last 30 days."

## Tools

| Tool | Purpose |
|---|---|
| `check_connection` | Verify the tokens work; report unit system and last-used device model |
| `list_activities` | Recent activities, newest first, optionally filtered by type |
| `list_activities_by_date` | Activities within a date range |
| `get_activity` | One activity, with optional splits, HR zones, weather and GPS |
| `daily_summary` | Steps, resting HR, stress, body battery, sleep for a day |
| `get_sleep` | Sleep score and stage breakdown |
| `get_hrv` | Overnight HRV against your baseline |
| `get_training_readiness` | Readiness score and contributing factors |
| `get_training_status` | Training status, acute load, VO2 max |
| `get_body_composition` | Weight and composition over a range |
| `get_steps_range` | Daily step totals for trend questions |
| `call_readonly` | Escape hatch for any other `garminconnect` `get_*` method |

Dates accept `YYYY-MM-DD`, `today` or `yesterday`.

Every tool answers with a single compact JSON document. An empty result is `[]`, `{}` or `null`, never an empty reply, so a client can always tell "no data" from a failure. A response over 100 KB is replaced by a short notice asking you to narrow the request.

## Why read-only

Reading is the whole scope of this project. It has no tools that upload, edit, log or delete anything, and pull requests that add write access are out of scope. That is a deliberate design choice:

- **Injected text can't change your account.** Activity names and other free-text fields from Garmin end up in the model's context. If one of them ever contains instructions, a server without write tools still cannot modify or delete your data.
- **Mistakes are cheap.** A wrong guess by the model can't corrupt your training history.
- **It stays auditable.** With no write paths, the code you need to review is small (roughly 700 lines), and you can check the read-only claim yourself (see [Verify it yourself](#verify-it-yourself)).

How it is enforced, beyond a label:

- A test runs every tool against a stand-in client that records each method the tools touch, and fails if any is not a `get_*` method.
- `call_readonly` rejects every method name that doesn't start with `get_`.
- A test fails if any `get_*` method in the installed `garminconnect` (following helper methods and functions, and flagging any use of the client that would hide a call, such as aliasing it) can reach the Garmin API through anything other than `Client.connectapi`, or reach a write request. A second test checks that `Client.connectapi` is hard-coded to GET, and a third that the audit covers every `get_*` method `call_readonly` can actually reach.
- A test also scans the tool code for write-style call names (`upload_…`, `delete_…`, `set_…`, and so on), which covers branches the behavioural test doesn't execute.
- Every tool is annotated with the MCP `readOnlyHint`, and a test fails if one isn't. That annotation is a hint to clients, not the protection itself — but clients can use it, for example Codex's `writes` approval mode (see [OpenAI Codex](#openai-codex)).

What read-only does **not** mean:

- **It doesn't make your data private.** What the tools read becomes part of your conversation with the assistant.
- **It doesn't stop leaks through other tools.** If the same session can also send email or browse the web, text injected into your data could try to use those. Be careful about which tools you combine.
- **It isn't zero writes.** Refreshing tokens contacts Garmin's sign-in service and updates your local token file. Your account data is not changed.

If you need to upload workouts or log data, use a server built for that, for example [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp), and review its tool list before enabling it.

## Privacy and security

### Where data goes

| Destination | What is sent | Why |
|---|---|---|
| Garmin (`*.garmin.com`) | Your sign-in exchange (once, via `garmin-mcp-auth`), then authenticated read requests | To fetch your own data |
| The AI client that launched the server | The tool results (summarised, scrubbed) | That is the purpose of an MCP server |
| Anywhere else | **Nothing** | The server has no telemetry, analytics or third-party endpoints |

Be precise about the second row: **whatever a tool returns becomes part of your conversation with the assistant**, and from there goes to the model provider behind that client — for example Anthropic, OpenAI or Google, depending on the client and the model you choose — under that provider's terms. This server cannot prevent that; it can only limit what it returns. If you don't want a piece of data in a conversation, don't ask a question that fetches it.

### What the server does and doesn't do

- **Runs locally over stdio.** No listening port, no remote hosting.
- **Talks only to Garmin.** The project's own code imports no networking library. The `garminconnect` dependency contacts only Garmin hosts (`sso`, `diauth`, `connect`, `connectapi`, `mobile.integration` under `garmin.com`).
- **Exports no telemetry.** The MCP SDK ships OpenTelemetry instrumentation that is on by default but does nothing unless an OpenTelemetry SDK/exporter is installed. None is (only `opentelemetry-api` is), and a test fails if one ever appears in the dependency tree.
- **Never handles your password.** Only `garmin-mcp-auth`, run by you, sees it, and it goes only to Garmin's sign-in service. The server process has no code path that accepts one.
- **Makes no write requests to your Garmin account.** `call_readonly` rejects any method not starting with `get_`, and in the pinned `garminconnect` version every `get_*` method reaches the API only through `Client.connectapi`, which the library hard-codes to GET (see the audit below). Refreshing tokens does contact Garmin's sign-in service and updates your local token file; that is authentication, not a change to your account data.
- **Declares every tool read-only.** All 12 tools carry the MCP `readOnlyHint`, and a test fails if a tool is registered without it, so clients can see up front that nothing here modifies data.
- **Turns known Garmin errors into short hints.** Rejected tokens, rate limits, unreachable Garmin and not-found items come back as a one-line hint carrying only the exception type and at most 160 characters of the message, because a full upstream message can quote credentials. Other exceptions are not rewritten.
- **Caps response size.** A result over 100 KB comes back as a short notice with a preview, so a raw payload can't flood the model's context.
- **Summarises and scrubs responses.** Dedicated tools return a small set of chosen fields. Every payload is also passed through `scrub()`, which removes known identifier keys (profile IDs, display names, email, device IDs and serial numbers) and precise GPS. GPS can be turned back on per call with `include_location=True` on `get_activity`.
- **Keeps tokens out of git.** `.gitignore` excludes the `.garminconnect/` and `tokens/` directories and the `garmin_tokens.json` file that `garminconnect` writes, so tokens are not picked up even if you point `GARMINTOKENS` inside the repo. Still check `git status` before committing.

### Limitations — read these

- **The scrubber is a denylist.** It removes *known* keys. Tools that summarise (`list_activities`, `daily_summary`, `get_sleep`, `get_hrv`) return only fields chosen by the code, so they behave like an allowlist. Tools that return a scrubbed raw payload (`call_readonly`, `get_training_status`, `get_training_readiness`, `get_body_composition`, `get_steps_range`, and the splits/HR-zone/weather add-ons of `get_activity`) can still contain an identifier Garmin adds under a key the scrubber doesn't know.
- **Some free-text and coarse-location fields pass through**: activity names you typed, the `locationName` (typically a city) on activities, and the last-used device *model* name.
- **Health data is sensitive by nature.** Sleep, heart rate and weight are personal even without an identifier attached.
- **The token file is a credential.** `~/.garminconnect` holds long-lived OAuth tokens in plain JSON (`garmin-mcp-auth` restricts the directory to `0700`). Anything running as your user can read them. Treat them like a password and never commit or share them.
- **Unofficial API.** See the disclaimer at the top. The `garminconnect` library mimics official Garmin clients to sign in, which is part of why this is fragile.
- **The read-only guarantee is tied to the pinned version.** `garminconnect` is pinned to an exact version in `pyproject.toml`, and the full dependency tree is recorded in `uv.lock` (install with `uv sync --locked`; plain `pip install` does not honour the lock file). A test in the suite fails if a `get_*` method in the installed `garminconnect` can reach the API through anything but that GET-only primitive, so a dependency bump that breaks the guarantee is caught. If you bump `garminconnect`, also re-run the audit below.

### Verify it yourself

You shouldn't have to take any of this on trust. From the repo root, after installing:

**1. The project's own code contains no networking imports** (no output means none found):

```bash
grep -rnE "import (requests|httpx|urllib|socket|aiohttp)|from (requests|httpx|urllib)" src/
```

**2. Every host the Garmin library can contact:**

```bash
grep -rhoE 'https?://[a-zA-Z0-9./_{}-]+' \
  "$(.venv/bin/python -c 'import garminconnect,os;print(os.path.dirname(garminconnect.__file__))')" \
  --include='*.py' | sed -E 's#(https?://[^/]+).*#\1#' | sort -u
```

You should see only `*.garmin.com` (the `{domain}` placeholder defaults to `garmin.com`).

**3. Every `get_*` method reaches the API only through the GET-only primitive** — following helper methods and functions (needs the test extra, see [Development](#development)):

```bash
uv run --locked --extra test pytest tests/test_safety.py -q -k Garminconnect
```

**4. Watch it at runtime.** Use a network monitor (Little Snitch or LuLu on macOS, `ss`/`nethogs` on Linux) while asking your assistant a Garmin question, and confirm the only outbound connections from the server process go to `*.garmin.com`.

**5. Or run the whole safety suite.** `tests/test_safety.py` covers check 3 above, plus the `call_readonly` guard, a check that every tool is annotated read-only, only ever touches `get_*` client methods and names no write-style call, the rule that `GARMINTOKENS` is the only environment variable the project reads, and a check that no OpenTelemetry exporter is installed. See [Development](#development).

### Reporting a vulnerability

See [SECURITY.md](SECURITY.md).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `GARMINTOKENS` | `~/.garminconnect` | Directory where OAuth tokens are stored and read. `${HOME}` references are expanded; an unresolved `${...}` placeholder from a client means "use the default". No other variable is ever expanded. |

There is intentionally no variable for a password. To use several Garmin accounts, sign in once per account with a different `GARMINTOKENS` and register one server entry per account, each with its own name and `GARMINTOKENS`.

## Troubleshooting

- **"No usable Garmin tokens found"** — run `garmin-mcp-auth` in a terminal.
- **"Stored Garmin tokens were rejected"** — the tokens expired or were revoked; run `garmin-mcp-auth` again.
- **Server doesn't appear in Claude Desktop** — check the path in the config is absolute and points to the executable inside your virtualenv, then fully quit and reopen the app.
- **Server doesn't appear in Claude Code** — run `claude mcp list`; remove and re-add if the path is wrong (`claude mcp remove garmin`).
- **Server doesn't appear in Codex** — run `codex mcp list`, or `/mcp` in the TUI; check the `command` path in `~/.codex/config.toml` and that the TOML is valid.
- **Server doesn't appear in Antigravity** — check that `mcp_config.json` is valid JSON (no comments or trailing commas), then reload servers from the MCP manager or restart the app.
- **"Garmin rate limit hit"** — wait a few minutes, then retry.
- **"Another Garmin request is still running"** — requests to Garmin are made one at a time and a call waits at most 30 s for its turn; retry shortly.
- **A result with `"truncated": true`** — the response was over 100 KB and was replaced by a notice; ask for less (a shorter date range, or one activity).
- **Sign-in fails or is rate limited** — Garmin sometimes throttles or challenges automated sign-ins. Wait a while and retry, and complete MFA if prompted.

## Development

```bash
uv sync --locked --extra test
uv run pytest tests/ -q
```

or, without uv:

```bash
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python -m pytest tests/ -q
```

Tests cover date parsing, the scrubber, activity summarising (including the nested single-activity payload), the error-hint wrapper, token-directory resolution, concurrency (one sign-in, one request at a time), the one-JSON-block answer format, the response size cap, and the safety claims in this README (every tool read-only and touching only `get_*` methods, `call_readonly` guard, no networking imports in project code, only `GARMINTOKENS` read from the environment, `get_*` methods reaching the API only through the GET-only primitive). They need no network or credentials. CI runs them on every push and pull request.

## License

MIT
