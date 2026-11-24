"""
Live café info from Google (Places API): opening hours, open now, address, phone.

Uses GOOGLE_PLACES_API_KEY + GOOGLE_PLACE_ID from .env. Cached for 2 hours so a
whole month stays well inside Google's free monthly allowance.
"""
from __future__ import annotations

import logging
import time

import config

log = logging.getLogger(__name__)
CACHE_SECONDS = 2 * 60 * 60
_cache: dict = {"at": 0.0, "info": None}
FIELDS = ("displayName,formattedAddress,nationalPhoneNumber,currentOpeningHours,"
          "regularOpeningHours,rating,userRatingCount")


def cafe_info() -> dict | None:
    """{'name','address','phone','open_now','hours': [7 lines], 'regular': [7 lines], 'rating'} or None."""
    if not (config.GOOGLE_PLACES_API_KEY and config.GOOGLE_PLACE_ID):
        return None
    if _cache["info"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["info"]
    try:
        import requests
        resp = requests.get(f"https://places.googleapis.com/v1/places/{config.GOOGLE_PLACE_ID}",
                            headers={"X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
                                     "X-Goog-FieldMask": FIELDS}, timeout=10)
        if resp.status_code != 200:
            log.warning("Google Places error %s: %s", resp.status_code, resp.text[:200])
            return _cache["info"]
        _cache["info"], _cache["at"] = parse(resp.json()), time.time()
        return _cache["info"]
    except Exception as e:
        log.warning("Google Places failed: %s", e)
        return _cache["info"]


def parse(data: dict) -> dict:
    current = data.get("currentOpeningHours") or {}
    regular = data.get("regularOpeningHours") or {}
    return {
        "name": (data.get("displayName") or {}).get("text", config.CAFE_NAME),
        "address": data.get("formattedAddress", ""),
        "phone": data.get("nationalPhoneNumber", ""),
        "rating": data.get("rating"),
        "reviews": data.get("userRatingCount"),
        "open_now": current.get("openNow", regular.get("openNow")),
        # current = the next 7 days INCLUDING holiday/special hours set on Google
        "hours": current.get("weekdayDescriptions") or regular.get("weekdayDescriptions") or [],
        "regular": regular.get("weekdayDescriptions") or [],
    }


def as_text(info: dict | None) -> str:
    """For the AI's context and the /hours command."""
    if not info:
        return "Opening hours: not available (Google Places not connected)."
    lines = [f"Opening hours on Google ({info['name']}), next 7 days incl. special/holiday hours:"]
    lines += [f"  {h}" for h in info["hours"]]
    if info["regular"] and info["regular"] != info["hours"]:
        lines += ["Usual weekly hours:"] + [f"  {h}" for h in info["regular"]]
    if info["open_now"] is not None:
        lines.append(f"Open right now: {'yes' if info['open_now'] else 'no'}")
    if info["address"]:
        lines.append(f"Address: {info['address']}")
    if info["phone"]:
        lines.append(f"Phone: {info['phone']}")
    return "\n".join(lines)
