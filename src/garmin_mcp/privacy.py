"""Field-level minimisation for Garmin API responses.

Garmin's endpoints return far more identity than any training question needs:
profile keys, display names, device serials and exact GPS coordinates ride along
with every activity. We strip those before the payload reaches the model, so the
transcript carries training data rather than an identity dossier.
"""

from __future__ import annotations

from typing import Any

# Account/device identifiers. Nothing here answers a fitness question.
IDENTITY_KEYS = frozenset(
    {
        "userprofilepk",
        "userprofileid",
        "profileid",
        "ownerid",
        "ownerdisplayname",
        "ownerfullname",
        "ownerprofileimageurlsmall",
        "ownerprofileimageurlmedium",
        "ownerprofileimageurllarge",
        "displayname",
        "username",
        "email",
        "emailaddress",
        "deviceid",
        "unitid",
        "serialnumber",
        "devicemetadata",
    }
)

# Precise location. Dropped by default; opt back in per call when a route
# actually matters (elevation profile, segment comparison).
LOCATION_KEYS = frozenset(
    {
        "startlatitude",
        "startlongitude",
        "endlatitude",
        "endlongitude",
        "latitude",
        "longitude",
        "lat",
        "lon",
        "polyline",
        "encodedpolyline",
    }
)


def scrub(value: Any, *, drop_location: bool = True) -> Any:
    """Recursively remove identity (and optionally location) fields.

    Matching is case-insensitive on the exact key name, so a legitimate field
    like ``averageHR`` is never caught by accident.
    """
    drop = IDENTITY_KEYS | LOCATION_KEYS if drop_location else IDENTITY_KEYS

    if isinstance(value, dict):
        return {
            k: scrub(v, drop_location=drop_location)
            for k, v in value.items()
            if k.lower() not in drop
        }
    if isinstance(value, list):
        return [scrub(v, drop_location=drop_location) for v in value]
    return value
