"""Tests for the pure transform layer - no network, no credentials."""

from datetime import date, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp.privacy import scrub
from garmin_mcp.server import _day, _summarise_activity


class TestDay:
    def test_iso_passthrough(self):
        assert _day("2026-03-14") == "2026-03-14"

    def test_today_and_default(self):
        today = date.today().isoformat()
        assert _day("today") == today
        assert _day(None) == today
        assert _day("  Today ") == today

    def test_yesterday(self):
        assert _day("yesterday") == (date.today() - timedelta(days=1)).isoformat()

    @pytest.mark.parametrize("bad", ["last tuesday", "14/03/2026", "2026-13-01", ""])
    def test_rejects_junk(self, bad):
        with pytest.raises(ToolError):
            _day(bad)


class TestScrub:
    def test_drops_identity_keeps_metrics(self):
        out = scrub({"ownerDisplayName": "x", "averageHR": 150, "activityId": 7})
        assert out == {"averageHR": 150, "activityId": 7}

    def test_recurses_into_nested_structures(self):
        out = scrub({"laps": [{"email": "a@b.c", "distance": 400}]})
        assert out == {"laps": [{"distance": 400}]}

    def test_location_dropped_by_default_and_opt_in(self):
        raw = {"startLatitude": 50.4, "distance": 5000}
        assert "startLatitude" not in scrub(raw)
        assert scrub(raw, drop_location=False)["startLatitude"] == 50.4

    def test_case_insensitive_but_exact(self):
        # OWNERID goes; averageHR survives despite containing no boundary marker.
        out = scrub({"OWNERID": 1, "averageHR": 2, "ownerIdentifier": 3})
        assert "OWNERID" not in out
        assert out["averageHR"] == 2
        assert out["ownerIdentifier"] == 3, "substring matches must not trigger"


class TestSummariseActivity:
    def test_running_gets_pace(self):
        out = _summarise_activity(
            {
                "activityId": 1,
                "activityName": "Morning Run",
                "activityType": {"typeKey": "running"},
                "distance": 10000.0,
                "duration": 3000.0,
                "averageHR": 155,
            }
        )
        assert out["distance_km"] == 10.0
        assert out["duration_min"] == 50.0
        assert out["pace_per_km"] == "5:00"
        assert "speed_kmh" not in out

    def test_cycling_gets_speed(self):
        out = _summarise_activity(
            {
                "activityId": 2,
                "activityType": {"typeKey": "cycling"},
                "distance": 40000.0,
                "duration": 3600.0,
            }
        )
        assert out["speed_kmh"] == 40.0
        assert "pace_per_km" not in out

    def test_pace_seconds_are_zero_padded(self):
        out = _summarise_activity(
            {"activityType": {"typeKey": "running"}, "distance": 1000.0, "duration": 305.0}
        )
        assert out["pace_per_km"] == "5:05"

    def test_empty_fields_are_omitted_not_null(self):
        out = _summarise_activity({"activityId": 3, "activityType": {"typeKey": "yoga"}})
        assert out == {"activity_id": 3, "type": "yoga"}

    def test_missing_activity_type_does_not_crash(self):
        out = _summarise_activity({"activityId": 4, "distance": 1000.0, "duration": 600.0})
        assert out["speed_kmh"] == 6.0

    def test_zero_distance_skips_derived_metrics(self):
        out = _summarise_activity(
            {"activityId": 5, "activityType": {"typeKey": "running"}, "distance": 0, "duration": 600.0}
        )
        assert "pace_per_km" not in out and "speed_kmh" not in out
