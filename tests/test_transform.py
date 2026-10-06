"""Tests for the pure transform layer - no network, no credentials."""

import copy
import json
from datetime import date, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from garmin_mcp import server
from garmin_mcp.privacy import scrub
from garmin_mcp.server import _day, _flatten_detail, _summarise_activity


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


# Synthetic payload with the structure Garmin returns for ONE activity (get_activity):
# metrics under summaryDTO, type under activityTypeDTO. The list endpoint is flat.
DETAIL = {
    "activityId": 42,
    "activityName": "Test Run",
    "locationName": "Testville",
    "userProfileId": 999,
    "activityTypeDTO": {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
    "eventTypeDTO": {"typeKey": "training"},
    "metadataDTO": {"lapCount": 5},
    "summaryDTO": {
        "startTimeLocal": "2026-01-01T07:00:00.0",
        "startLatitude": 1.5,
        "startLongitude": 2.5,
        "distance": 10000.0,
        "duration": 3000.0,
        "elapsedDuration": 3100.0,
        "calories": 700.0,
        "averageHR": 150.0,
        "maxHR": 175.0,
        "elevationGain": 80.0,
        "trainingEffect": 3.2,
        "anaerobicTrainingEffect": 0.5,
    },
}


class DetailClient:
    def get_activity(self, activity_id):
        return copy.deepcopy(DETAIL)

    def __getattr__(self, name):
        return lambda *args, **kwargs: {}


class TestGetActivityDetail:
    @pytest.fixture(autouse=True)
    def _client(self, monkeypatch):
        monkeypatch.setattr(server, "connect", lambda: DetailClient())

    def test_summary_is_filled_from_the_nested_payload(self):
        summary = server.get_activity("42")["summary"]
        assert summary["name"] == "Test Run"
        assert summary["type"] == "running"
        assert summary["distance_km"] == 10.0
        assert summary["duration_min"] == 50.0
        assert summary["pace_per_km"] == "5:00"
        assert summary["avg_hr"] == 150.0
        assert summary["elevation_gain_m"] == 80.0
        assert summary["training_effect_aerobic"] == 3.2
        assert summary["location"] == "Testville"

    def test_gps_is_left_out_by_default_and_identifiers_never_appear(self):
        result = server.get_activity("42")
        text = json.dumps(result)
        assert "start_latitude" not in result["summary"]
        assert "startLatitude" not in text and "userProfileId" not in text

    def test_no_null_coordinates_for_an_activity_without_gps(self, monkeypatch):
        indoor = copy.deepcopy(DETAIL)
        for key in ("startLatitude", "startLongitude"):
            del indoor["summaryDTO"][key]

        class Indoor(DetailClient):
            def get_activity(self, activity_id):
                return copy.deepcopy(indoor)

        monkeypatch.setattr(server, "connect", lambda: Indoor())
        summary = server.get_activity("42", include_location=True)["summary"]
        assert "start_latitude" not in summary and "start_longitude" not in summary

    def test_gps_can_be_requested(self):
        summary = server.get_activity("42", include_location=True)["summary"]
        assert (summary["start_latitude"], summary["start_longitude"]) == (1.5, 2.5)


class TestFlattenDetail:
    def test_flat_list_shaped_payload_is_unchanged(self):
        flat = {"activityId": 1, "distance": 5.0, "activityType": {"typeKey": "cycling"}}
        assert _flatten_detail(flat) == flat

    def test_alternate_key_names_are_understood(self):
        out = _summarise_activity({"averagePower": 200, "normalizedPower": 230, "trainingEffect": 2.0})
        assert (out["avg_power_w"], out["normalized_power_w"], out["training_effect_aerobic"]) == (200, 230, 2.0)
