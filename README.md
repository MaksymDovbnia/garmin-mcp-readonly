# garmin-mcp

An MCP server that gives Claude read access to your own Garmin Connect training
and health data.

Built as a replacement for the community `garmin-mcp.dxt` extension, with three
deliberate differences:

| | Upstream `.dxt` | This |
|---|---|---|
| Code source | `uvx --from git+…` — re-resolves an unpinned branch on every launch | Local checkout you control; `garminconnect` pinned to an exact version |
| Password | Accepted as a `GARMIN_PASSWORD` env var | Never accepted — token-only, no code path takes a password |
| Response size | Endpoint output forwarded as-is | Summarised to the fields that answer questions |
| Identifiers | Passed through | Profile keys, display names, device serials and GPS stripped by default |

## Setup

```bash
cd ~/dev/garmin-mcp
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

Then sign in once, in your own terminal:

```bash
.venv/bin/garmin-mcp-auth
```

It prompts for your email, your password (hidden), and an MFA code if your
account uses one. The password mints OAuth tokens and is then discarded — it is
never written to disk, config, or environment. Tokens land in
`~/.garminconnect` (override with `GARMINTOKENS`) and stay valid for months.

## Register with Claude Code

```bash
claude mcp add garmin ~/dev/garmin-mcp/.venv/bin/garmin-mcp
```

For Claude Desktop, `manifest.json` in this repo is a desktop-extension
descriptor pointing at the same binary.

## Tools

| Tool | Purpose |
|---|---|
| `check_connection` | Verify tokens work; report unit system and last device |
| `list_activities` | Recent activities, newest first, optionally filtered by type |
| `list_activities_by_date` | Activities within a date range |
| `get_activity` | One activity, with optional splits, HR zones, weather |
| `daily_summary` | Steps, resting HR, stress, body battery, sleep for a day |
| `get_sleep` | Sleep score and stage breakdown |
| `get_hrv` | Overnight HRV against your baseline |
| `get_training_readiness` | Readiness score and contributing factors |
| `get_training_status` | Training status, acute load, VO2 max |
| `get_body_composition` | Weight and composition over a range |
| `get_steps_range` | Daily step totals for trend questions |
| `call_readonly` | Escape hatch for any `garminconnect` `get_*` method |

Dates accept `YYYY-MM-DD`, `today`, or `yesterday`.

`call_readonly` rejects any method name not starting with `get_`, so it cannot
upload, modify or delete anything. The server as a whole performs no writes to
your Garmin account.

## Privacy

Every response passes through `privacy.scrub`, which removes account and device
identifiers, and precise GPS coordinates. Coordinates can be re-enabled per call
with `include_location=True` on `get_activity` when a route genuinely matters.

`.gitignore` excludes token stores, so tokens cannot be committed even if you
point `GARMINTOKENS` inside the repo.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Covers date parsing, the scrubber, and activity summarisation. No network or
credentials required.
