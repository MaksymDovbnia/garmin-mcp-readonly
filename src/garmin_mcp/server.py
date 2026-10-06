"""MCP server exposing a focused, read-mostly view of Garmin Connect data.

Design notes:
  * Responses are summarised, not forwarded wholesale. Garmin returns 100+ keys
    per activity; a few dozen of them answer real questions and the rest just
    burn context.
  * Every payload passes through `privacy.scrub`, which drops account
    identifiers and precise GPS unless a caller opts back in.
  * Dates accept "today"/"yesterday" as well as YYYY-MM-DD, because that is how
    people actually ask.
"""

from __future__ import annotations

import functools
import inspect
import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import connect
from .privacy import scrub

server = MCPServer(
    name="garmin",
    instructions=(
        "Read the user's own Garmin Connect training and health data. "
        "Dates are YYYY-MM-DD, or the words 'today'/'yesterday'. "
        "Prefer list_activities + get_activity over call_readonly."
    ),
)

# Every tool here only reads. Say so to the client (read_only_hint) and send each
# result once as text: with structured output on, typed returns are also emitted
# as structuredContent, which clients that forward both blocks pay for twice.
_register = server.tool(
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    structured_output=False,
)


# Largest response sent to the model. A raw payload such as an activity's per-second
# chart data can run to megabytes, which floods the context and trips client limits.
MAX_RESPONSE_BYTES = 100_000


_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _repair(value: Any) -> Any:
    """Make data from Garmin encodable: NaN/Infinity become null, lone surrogates become U+FFFD.

    Garmin's JSON is external input. A name with half an emoji or one non-finite metric
    must not fail a whole result. Types JSON cannot hold at all are left alone, so they
    still raise: that would be a bug here, not bad data.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _LONE_SURROGATE.sub("\ufffd", value)
    if isinstance(value, dict):
        return {_repair(k): _repair(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_repair(v) for v in value]
    return value


def _dumps(value: Any) -> tuple[str, int]:
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text, len(text.encode("utf-8"))


def _to_json(value: Any) -> str:
    """One compact JSON document; oversized results become a notice with a short preview.

    Bad data from Garmin is repaired (see ``_repair``); values JSON cannot represent at
    all raise, because they mean a bug in this code.
    """
    try:
        text, size = _dumps(value)
    except ValueError:  # NaN/Infinity, or a lone surrogate failing UTF-8 encoding
        text, size = _dumps(_repair(value))
    if size <= MAX_RESPONSE_BYTES:
        return text
    notice = {
        "truncated": True,
        "total_bytes": size,
        "limit_bytes": MAX_RESPONSE_BYTES,
        "hint": "Result too large. Ask for less: a shorter date range, one activity, or a dedicated tool.",
        "preview": text[:2000],
    }
    return json.dumps(notice, separators=(",", ":"), ensure_ascii=False)


def read_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Register ``fn`` as a read-only tool that answers with ONE compact JSON text block.

    Handing a list or None to the SDK gives one block per element and no block at all
    for an empty result, which clients treat inconsistently; a single JSON string is
    unambiguous ("[]" and "null" included). ``fn`` itself is returned unchanged, so it
    stays directly callable from tests. Sync functions only: the wrapper is sync, so an
    ``async def`` would be serialised as a coroutine object instead of being awaited.
    """
    if inspect.iscoroutinefunction(fn):
        raise TypeError(f"read_tool wraps sync tools only; {fn.__name__} is async")

    @functools.wraps(fn)
    def as_json(*args: Any, **kwargs: Any) -> str:
        return _to_json(fn(*args, **kwargs))

    _register(as_json)
    return fn

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _day(value: str | None) -> str:
    """Normalise a user-supplied day to YYYY-MM-DD."""
    if value is None or value.strip().lower() == "today":
        return date.today().isoformat()
    if value.strip().lower() == "yesterday":
        return (date.today() - timedelta(days=1)).isoformat()
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ToolError(
            f"Could not read {value!r} as a date. Use YYYY-MM-DD, 'today' or 'yesterday'."
        ) from exc


def _round(value: Any, digits: int = 2) -> Any:
    return round(value, digits) if isinstance(value, (int, float)) else value


def _first(raw: dict[str, Any], *keys: str) -> Any:
    """First value among ``keys`` that is present (Garmin names the same metric differently per endpoint)."""
    return next((raw[k] for k in keys if raw.get(k) is not None), None)


def _flatten_detail(raw: dict[str, Any]) -> dict[str, Any]:
    """Give a single-activity payload the flat shape the list endpoints return.

    ``get_activity`` nests the metrics under ``summaryDTO`` and the type under
    ``activityTypeDTO``; ``_summarise_activity`` reads them from the top level.
    """
    flat = {k: v for k, v in raw.items() if k != "summaryDTO"}
    flat.update(raw.get("summaryDTO") or {})
    if "activityType" not in flat and flat.get("activityTypeDTO"):
        flat["activityType"] = flat["activityTypeDTO"]
    return flat


def _summarise_activity(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Garmin activity record to the fields people ask about."""
    distance_m = raw.get("distance") or 0
    duration_s = raw.get("duration") or 0
    type_key = (raw.get("activityType") or {}).get("typeKey")

    out: dict[str, Any] = {
        "activity_id": raw.get("activityId"),
        "name": raw.get("activityName"),
        "type": type_key,
        "start_local": raw.get("startTimeLocal"),
        "distance_km": _round(distance_m / 1000, 2) if distance_m else None,
        "duration_min": _round(duration_s / 60, 1) if duration_s else None,
        "elapsed_min": _round((raw.get("elapsedDuration") or 0) / 60, 1) or None,
        "calories": raw.get("calories"),
        "avg_hr": raw.get("averageHR"),
        "max_hr": raw.get("maxHR"),
        "elevation_gain_m": _round(raw.get("elevationGain"), 1),
        "avg_power_w": _first(raw, "avgPower", "averagePower"),
        "normalized_power_w": _first(raw, "normPower", "normalizedPower"),
        "training_effect_aerobic": _first(raw, "aerobicTrainingEffect", "trainingEffect"),
        "training_effect_anaerobic": raw.get("anaerobicTrainingEffect"),
        "location": raw.get("locationName"),
    }

    # Pace for foot sports, speed for everything else - whichever the athlete
    # would actually quote back to you.
    if distance_m and duration_s:
        if type_key and ("running" in type_key or "walking" in type_key or "hiking" in type_key):
            pace = duration_s / 60 / (distance_m / 1000)
            minutes, seconds = divmod(round(pace * 60), 60)
            out["pace_per_km"] = f"{minutes}:{seconds:02d}"
        else:
            out["speed_kmh"] = _round(distance_m / 1000 / (duration_s / 3600), 2)

    return {k: v for k, v in out.items() if v is not None}


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


@read_tool
def check_connection() -> dict[str, Any]:
    """Verify the stored Garmin tokens work. Returns unit system and last device."""
    garmin = connect()
    return {
        "connected": True,
        "unit_system": garmin.get_unit_system(),
        "last_device": (garmin.get_device_last_used() or {}).get("lastUsedDeviceName"),
    }


@read_tool
def list_activities(
    limit: int = 10,
    start: int = 0,
    activity_type: str | None = None,
) -> list[dict[str, Any]]:
    """List recent activities, newest first.

    Args:
        limit: How many to return (1-50).
        start: Offset for paging back through history.
        activity_type: Optional filter, e.g. "running", "cycling", "swimming".
    """
    limit = max(1, min(limit, 50))
    raw = connect().get_activities(start=start, limit=limit, activitytype=activity_type)
    records = raw if isinstance(raw, list) else raw.get("activityList", [])
    return [_summarise_activity(scrub(a)) for a in records]


@read_tool
def list_activities_by_date(
    start_date: str,
    end_date: str | None = None,
    activity_type: str | None = None,
) -> list[dict[str, Any]]:
    """List activities in a date range (inclusive).

    Args:
        start_date: First day, YYYY-MM-DD.
        end_date: Last day. Defaults to today.
        activity_type: Optional filter, e.g. "running".
    """
    records = connect().get_activities_by_date(
        _day(start_date), _day(end_date), activitytype=activity_type
    )
    return [_summarise_activity(scrub(a)) for a in records]


@read_tool
def get_activity(
    activity_id: str,
    include_splits: bool = False,
    include_hr_zones: bool = False,
    include_weather: bool = False,
    include_location: bool = False,
) -> dict[str, Any]:
    """Get one activity in detail.

    Args:
        activity_id: From list_activities.
        include_splits: Add per-lap/split breakdown.
        include_hr_zones: Add time spent in each heart-rate zone.
        include_weather: Add recorded conditions.
        include_location: Include GPS coordinates. Off by default.
    """
    garmin = connect()
    raw = _flatten_detail(scrub(garmin.get_activity(activity_id), drop_location=not include_location))
    result: dict[str, Any] = {"summary": _summarise_activity(raw)}

    if include_location:
        # Indoor activities have no GPS; leave the keys out rather than send nulls.
        for key, source in (("start_latitude", "startLatitude"), ("start_longitude", "startLongitude")):
            if raw.get(source) is not None:
                result["summary"][key] = raw[source]
    if include_splits:
        result["splits"] = scrub(
            garmin.get_activity_splits(activity_id), drop_location=not include_location
        )
    if include_hr_zones:
        result["hr_zones"] = scrub(garmin.get_activity_hr_in_timezones(activity_id))
    if include_weather:
        result["weather"] = scrub(garmin.get_activity_weather(activity_id))

    return result


@read_tool
def daily_summary(day: str | None = None) -> dict[str, Any]:
    """Headline health numbers for one day: steps, resting HR, stress, body battery, sleep."""
    cdate = _day(day)
    stats = scrub(connect().get_stats(cdate) or {})
    return {
        "date": cdate,
        "steps": stats.get("totalSteps"),
        "step_goal": stats.get("dailyStepGoal"),
        "distance_km": _round((stats.get("totalDistanceMeters") or 0) / 1000, 2),
        "floors_climbed": stats.get("floorsAscended"),
        "calories_total": stats.get("totalKilocalories"),
        "calories_active": stats.get("activeKilocalories"),
        "resting_hr": stats.get("restingHeartRate"),
        "min_hr": stats.get("minHeartRate"),
        "max_hr": stats.get("maxHeartRate"),
        "stress_avg": stats.get("averageStressLevel"),
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "sleep_hours": _round((stats.get("sleepingSeconds") or 0) / 3600, 1),
        "intensity_minutes_moderate": stats.get("moderateIntensityMinutes"),
        "intensity_minutes_vigorous": stats.get("vigorousIntensityMinutes"),
    }


@read_tool
def get_sleep(day: str | None = None) -> dict[str, Any]:
    """Sleep breakdown for one night: score, stages, and restlessness."""
    cdate = _day(day)
    data = scrub(connect().get_sleep_data(cdate) or {})
    dto = data.get("dailySleepDTO") or {}
    scores = dto.get("sleepScores") or {}
    return {
        "date": cdate,
        "score": (scores.get("overall") or {}).get("value"),
        "quality": (scores.get("overall") or {}).get("qualifierKey"),
        "total_hours": _round((dto.get("sleepTimeSeconds") or 0) / 3600, 1),
        "deep_hours": _round((dto.get("deepSleepSeconds") or 0) / 3600, 1),
        "light_hours": _round((dto.get("lightSleepSeconds") or 0) / 3600, 1),
        "rem_hours": _round((dto.get("remSleepSeconds") or 0) / 3600, 1),
        "awake_hours": _round((dto.get("awakeSleepSeconds") or 0) / 3600, 1),
        "avg_spo2": data.get("avgSleepSpO2"),
        "avg_respiration": data.get("avgRespirationValue"),
        "resting_hr": data.get("restingHeartRate"),
    }


@read_tool
def get_hrv(day: str | None = None) -> dict[str, Any]:
    """Overnight heart-rate variability summary and baseline status."""
    cdate = _day(day)
    data = scrub(connect().get_hrv_data(cdate) or {})
    summary = data.get("hrvSummary") or {}
    return {
        "date": cdate,
        "last_night_avg_ms": summary.get("lastNightAvg"),
        "last_night_5min_high_ms": summary.get("lastNight5MinHigh"),
        "status": summary.get("status"),
        "baseline": summary.get("baseline"),
    }


@read_tool
def get_training_readiness(day: str | None = None) -> list[dict[str, Any]]:
    """Training readiness score and the factors feeding it."""
    data = connect().get_training_readiness(_day(day)) or []
    return scrub(data)


@read_tool
def get_training_status(day: str | None = None) -> dict[str, Any]:
    """Training status, acute load, and VO2 max."""
    return scrub(connect().get_training_status(_day(day)) or {})


@read_tool
def get_body_composition(start_date: str, end_date: str | None = None) -> dict[str, Any]:
    """Weight and body-composition measurements over a date range."""
    return scrub(connect().get_body_composition(_day(start_date), _day(end_date)) or {})


@read_tool
def get_steps_range(start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    """Daily step totals across a date range, for trend questions."""
    return scrub(connect().get_daily_steps(_day(start_date), _day(end_date)) or [])


@read_tool
def call_readonly(method: str, params: dict[str, Any] | None = None) -> Any:
    """Escape hatch for Garmin endpoints without a dedicated tool.

    Only `get_*` methods are reachable, so this cannot upload, modify or delete
    anything. Use the dedicated tools first - their output is far more compact. A result
    over 100 KB comes back as a short truncation notice: ask for less.

    Args:
        method: A garminconnect method name, e.g. "get_personal_record".
        params: Keyword arguments for that method.
    """
    if not method.startswith("get_"):
        raise ToolError(
            f"{method!r} is not readable through this tool: only get_* methods are allowed."
        )
    garmin = connect()
    func = getattr(garmin, method, None)
    if func is None or not callable(func):
        raise ToolError(f"No such Garmin method: {method!r}")
    return scrub(func(**(params or {})))


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
