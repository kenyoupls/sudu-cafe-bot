"""Opening hours from Google Places (no network)."""
import places

SAMPLE = {
    "displayName": {"text": "Sudu Bingsu"},
    "formattedAddress": "Jonker Street, Melaka",
    "currentOpeningHours": {"openNow": True, "weekdayDescriptions": [
        "Monday: 12:00 – 10:00 PM", "Tuesday: Closed"]},
    "regularOpeningHours": {"weekdayDescriptions": ["Monday: 12:00 – 10:00 PM", "Tuesday: 12:00 – 10:00 PM"]},
}


def test_parse_prefers_current_hours_with_specials():
    info = places.parse(SAMPLE)
    assert info["hours"][1] == "Tuesday: Closed" and info["open_now"] is True
    text = places.as_text(info)
    assert "Tuesday: Closed" in text and "Usual weekly hours" in text and "Open right now: yes" in text


def test_not_configured(monkeypatch):
    monkeypatch.setattr(places.config, "GOOGLE_PLACES_API_KEY", "")
    assert places.cafe_info() is None
    assert "not available" in places.as_text(None)
