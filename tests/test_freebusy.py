"""Key tests for the /freebusy-export endpoint.

Run from the repository root:  python -m pytest tests/ -v
"""

from fastapi.testclient import TestClient
from icalendar import Calendar

from app.main import app

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//fb-api//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def event_utc(uid: str, start: str, end: str, *extra: str) -> str:
    return event(f"UID:{uid}", f"DTSTART:{start}", f"DTEND:{end}", *extra)


def freebusy(calendars, *, window_start="2026-03-01T00:00:00Z",
             window_end="2026-03-08T00:00:00Z"):
    return client.post(
        "/freebusy-export",
        json={
            "window_start": window_start,
            "window_end": window_end,
            "calendars": calendars,
        },
    )


def one(cid, ics, timezone="UTC"):
    return {"calendar_id": cid, "ics": ics, "timezone": timezone}


def busy_periods(text):
    parsed = Calendar.from_ical(text)
    fbs = list(parsed.walk("VFREEBUSY"))
    assert len(fbs) == 1
    props = fbs[0].get("FREEBUSY", [])
    if not isinstance(props, list):
        props = [props]
    return [
        (
            p.dt[0].astimezone(__import__("datetime").timezone.utc),
            p.dt[1].astimezone(__import__("datetime").timezone.utc),
        )
        for p in props
    ]


def stamps(resp, cid="alice"):
    text = freebusy_text(resp, cid)
    return [line for line in text.split("\r\n") if line.startswith("FREEBUSY")]


def freebusy_text(resp, cid="alice"):
    for entry in resp.json()["calendars"]:
        if entry["calendar_id"] == cid:
            return entry["vfreebusy"]
    raise KeyError(cid)


# ---------------------------------------------------------------------------
# Basic busy periods
# ---------------------------------------------------------------------------

def test_single_event_becomes_freebusy_period():
    ics = cal(event_utc("e1", "20260302T100000Z", "20260302T110000Z"))
    resp = freebusy([one("alice", ics)])
    assert resp.status_code == 200
    entry = resp.json()["calendars"][0]
    assert entry["count"] == 1
    assert stamps(resp) == ["FREEBUSY:20260302T100000Z/20260302T110000Z"]


def test_empty_calendar_has_no_freebusy_lines():
    resp = freebusy([one("alice", cal())])
    assert resp.status_code == 200
    text = resp.json()["calendars"][0]["vfreebusy"]
    assert "FREEBUSY:" not in text  # no busy periods; wrapper still present
    assert "BEGIN:VFREEBUSY" in text
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.endswith("END:VCALENDAR\r\n")
    # No bare LF: every line including the last is CRLF-delimited.
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")


def test_overlapping_events_merge_into_one_period():
    ics = cal(
        event_utc("a", "20260302T100000Z", "20260302T120000Z"),
        event_utc("b", "20260302T110000Z", "20260302T130000Z"),
    )
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == ["FREEBUSY:20260302T100000Z/20260302T130000Z"]


def test_adjacent_events_merge_touching_half_open():
    ics = cal(
        event_utc("a", "20260302T100000Z", "20260302T110000Z"),
        event_utc("b", "20260302T110000Z", "20260302T120000Z"),
    )
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == ["FREEBUSY:20260302T100000Z/20260302T120000Z"]


def test_disjoint_events_stay_separate_and_sorted():
    ics = cal(
        event_utc("later", "20260304T140000Z", "20260304T150000Z"),
        event_utc("earlier", "20260302T090000Z", "20260302T100000Z"),
    )
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == [
        "FREEBUSY:20260302T090000Z/20260302T100000Z",
        "FREEBUSY:20260304T140000Z/20260304T150000Z",
    ]


def test_busy_spans_are_clipped_to_the_window():
    ics = cal(
        event_utc("left", "20260228T200000Z", "20260301T020000Z"),
        event_utc("right", "20260307T220000Z", "20260308T030000Z"),
    )
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == [
        "FREEBUSY:20260301T000000Z/20260301T020000Z",
        "FREEBUSY:20260307T220000Z/20260308T000000Z",
    ]


def test_event_merely_touching_window_boundary_contributes_nothing():
    ics = cal(event_utc("edge", "20260308T000000Z", "20260308T010000Z"))
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == []


# ---------------------------------------------------------------------------
# STATUS and TRANSP
# ---------------------------------------------------------------------------

def test_cancelled_events_are_ignored():
    ics = cal(
        event_utc("dead", "20260302T100000Z", "20260302T110000Z",
                  "STATUS:CANCELLED"),
        event_utc("alive", "20260303T100000Z", "20260303T110000Z"),
    )
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == ["FREEBUSY:20260303T100000Z/20260303T110000Z"]


def test_transparent_event_does_not_block_busy_time():
    ics = cal(
        event_utc("free", "20260302T100000Z", "20260302T110000Z",
                  "TRANSP:TRANSPARENT"),
        event_utc("busy", "20260302T130000Z", "20260302T140000Z"),
    )
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == ["FREEBUSY:20260302T130000Z/20260302T140000Z"]


def test_recurring_instances_inherit_master_transp():
    ics = cal(event(
        "UID:r",
        "DTSTART:20260302T100000Z",
        "DTEND:20260302T110000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "TRANSP:TRANSPARENT",
    ))
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == []


def test_override_transp_overrides_inherited_value():
    # Master is OPAQUE: Mar 2 and Mar 4 are busy. The Mar 3 override
    # declares TRANSPARENT, so that occurrence drops out.
    ics = cal(event(
        "UID:r",
        "DTSTART:20260302T100000Z",
        "DTEND:20260302T110000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
    ), event(
        "UID:r",
        "RECURRENCE-ID:20260303T100000Z",
        "DTSTART:20260303T150000Z",
        "DTEND:20260303T160000Z",
        "TRANSP:TRANSPARENT",
    ))
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == [
        "FREEBUSY:20260302T100000Z/20260302T110000Z",
        "FREEBUSY:20260304T100000Z/20260304T110000Z",
    ]


def test_opaque_override_overrides_transparent_master():
    # Master is TRANSPARENT, but the Mar 3 override explicitly opts back in.
    ics = cal(event(
        "UID:r",
        "DTSTART:20260302T100000Z",
        "DTEND:20260302T110000Z",
        "RRULE:FREQ=DAILY;COUNT=3",
        "TRANSP:TRANSPARENT",
    ), event(
        "UID:r",
        "RECURRENCE-ID:20260303T100000Z",
        "DTSTART:20260303T100000Z",
        "DTEND:20260303T110000Z",
        "TRANSP:OPAQUE",
    ))
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == ["FREEBUSY:20260303T100000Z/20260303T110000Z"]


def test_override_without_transp_keeps_inheriting():
    ics = cal(event(
        "UID:r",
        "DTSTART:20260302T100000Z",
        "DTEND:20260302T110000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
        "TRANSP:TRANSPARENT",
    ), event(
        "UID:r",
        "RECURRENCE-ID:20260303T100000Z",
        "DTSTART:20260303T140000Z",
        "DTEND:20260303T150000Z",
        "SUMMARY:Moved but still transparent",
    ))
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == []


def test_cancelled_override_removes_instance():
    ics = cal(event(
        "UID:r",
        "DTSTART:20260302T100000Z",
        "DTEND:20260302T110000Z",
        "RRULE:FREQ=DAILY;COUNT=2",
    ), event(
        "UID:r",
        "RECURRENCE-ID:20260302T100000Z",
        "STATUS:CANCELLED",
    ))
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == ["FREEBUSY:20260303T100000Z/20260303T110000Z"]


def test_zero_duration_instance_contributes_nothing():
    ics = cal(event_utc("z", "20260302T120000Z", "20260302T120000Z"))
    resp = freebusy([one("alice", ics)])
    assert stamps(resp) == []


# ---------------------------------------------------------------------------
# DATE all-day events and DST
# ---------------------------------------------------------------------------

def test_all_day_event_is_local_whole_day():
    ics = cal(event(
        "UID:d",
        "DTSTART;VALUE=DATE:20260303",
        "DTEND;VALUE=DATE:20260304",
    ))
    resp = freebusy([one("alice", ics, timezone="Asia/Tokyo")])
    # Tokyo is UTC+9 all year: the local day is 2026-03-02 15:00Z to
    # 2026-03-03 15:00Z.
    assert stamps(resp) == ["FREEBUSY:20260302T150000Z/20260303T150000Z"]


def test_all_day_day_is_23_hours_when_springing_forward():
    # America/New_York springs forward on 2026-03-08 02:00 local.
    ics = cal(event(
        "UID:d",
        "DTSTART;VALUE=DATE:20260308",
        "DTEND;VALUE=DATE:20260309",
    ))
    resp = freebusy([one("alice", ics, timezone="America/New_York")],
                    window_start="2026-03-07T00:00:00Z",
                    window_end="2026-03-10T00:00:00Z")
    # EST midnight is 05:00Z; the EDT midnight after the skipped hour is 04:00Z.
    assert stamps(resp) == ["FREEBUSY:20260308T050000Z/20260309T040000Z"]


def test_all_day_day_is_25_hours_when_falling_back():
    # America/New_York falls back on 2026-11-01 02:00 local.
    ics = cal(event(
        "UID:d",
        "DTSTART;VALUE=DATE:20261101",
        "DTEND;VALUE=DATE:20261102",
    ))
    resp = freebusy([one("alice", ics, timezone="America/New_York")],
                    window_start="2026-10-31T00:00:00Z",
                    window_end="2026-11-03T00:00:00Z")
    # EDT midnight is 04:00Z; the EST midnight after the repeated hour is 05:00Z.
    assert stamps(resp) == ["FREEBUSY:20261101T040000Z/20261102T050000Z"]


def test_multi_day_all_day_event_spans_local_midnights():
    ics = cal(event(
        "UID:d",
        "DTSTART;VALUE=DATE:20260302",
        "DTEND;VALUE=DATE:20260305",  # Mar 2, 3, 4 local
    ))
    resp = freebusy([one("alice", ics, timezone="Europe/Berlin")])
    # Berlin is UTC+1 in early March: Mar 2 00:00 local = Mar 1 23:00Z,
    # Mar 5 00:00 local = Mar 4 23:00Z.
    assert stamps(resp) == ["FREEBUSY:20260301T230000Z/20260304T230000Z"]


def test_all_day_event_at_positive_offset_window_edge_is_not_missed():
    # Pacific/Apia is UTC+13 in January (southern-hemisphere summer): the
    # local day Jan 8 starts Jan 7 11:00Z and ends Jan 8 11:00Z. Its
    # floating-UTC frame (Jan 8 00:00Z-Jan 9 00:00Z) overlaps the window by
    # only an instant at the boundary, so the day-of padding must keep it in
    # scope and the local-day reinterpretation must do the rest.
    ics = cal(event(
        "UID:d",
        "DTSTART;VALUE=DATE:20260108",
        "DTEND;VALUE=DATE:20260109",
    ))
    resp = freebusy([one("alice", ics, timezone="Pacific/Apia")],
                    window_start="2026-01-08T00:00:00Z",
                    window_end="2026-01-09T00:00:00Z")
    # Jan 8 00:00 +13 = Jan 7 11:00Z; clipped to the window -> 11 busy hours.
    assert stamps(resp) == ["FREEBUSY:20260108T000000Z/20260108T110000Z"]


def test_all_day_event_at_negative_offset_window_edge_is_not_missed():
    # Pacific/Pago_Pago is UTC-11: the local day Jan 8 ends Jan 9 11:00Z,
    # while its floating-UTC frame ends at Jan 9 00:00Z (window end).
    ics = cal(event(
        "UID:d",
        "DTSTART;VALUE=DATE:20260108",
        "DTEND;VALUE=DATE:20260109",
    ))
    resp = freebusy([one("alice", ics, timezone="Pacific/Pago_Pago")],
                    window_start="2026-01-08T00:00:00Z",
                    window_end="2026-01-09T00:00:00Z")
    # Jan 8 00:00 -11 = Jan 8 11:00Z, day ends Jan 9 11:00Z (clipped).
    assert stamps(resp) == ["FREEBUSY:20260108T110000Z/20260109T000000Z"]


def test_recurring_all_day_series_follows_dst():
    ics = cal(event(
        "UID:d",
        "DTSTART;VALUE=DATE:20260307",
        "DTEND;VALUE=DATE:20260308",
        "RRULE:FREQ=DAILY;COUNT=3",  # Mar 7 (EST), Mar 8 (spring forward), Mar 9 (EDT)
    ))
    resp = freebusy([one("alice", ics, timezone="America/New_York")],
                    window_start="2026-03-06T00:00:00Z",
                    window_end="2026-03-11T00:00:00Z")
    # Consecutive local days are adjacent on the UTC timeline, so they merge
    # into one span. Its length is 24 + 23 + 24 = 71 hours: Mar 7 05:00Z to
    # Mar 10 04:00Z (a flat 72-hour span would ignore the skipped hour).
    assert stamps(resp) == ["FREEBUSY:20260307T050000Z/20260310T040000Z"]


def test_gap_in_all_day_series_exposes_the_23_hour_spring_forward_day():
    # Mar 7, Mar 9, Mar 11 (every other day): the middle occurrence is the
    # 23-hour spring-forward day, visible with distinct UTC boundaries while
    # its neighbors are ordinary 24-hour EST/EDT days.
    ics = cal(event(
        "UID:d",
        "DTSTART;VALUE=DATE:20260307",
        "DTEND;VALUE=DATE:20260308",
        "RRULE:FREQ=DAILY;COUNT=3;INTERVAL=2",
    ))
    resp = freebusy([one("alice", ics, timezone="America/New_York")],
                    window_start="2026-03-06T00:00:00Z",
                    window_end="2026-03-12T00:00:00Z")
    assert stamps(resp) == [
        "FREEBUSY:20260307T050000Z/20260308T050000Z",  # 24h EST day
        "FREEBUSY:20260309T040000Z/20260310T040000Z",  # 23h spring-forward day
        "FREEBUSY:20260311T040000Z/20260312T000000Z",  # 24h EDT day, clipped
    ]


# ---------------------------------------------------------------------------
# Multiple calendars, ordering, document shape
# ---------------------------------------------------------------------------

def test_multiple_calendars_each_get_their_own_document():
    alice = cal(event_utc("a", "20260302T100000Z", "20260302T110000Z"))
    bob = cal(event_utc("b", "20260303T100000Z", "20260303T110000Z"))
    resp = freebusy([one("alice", alice), one("bob", bob)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert [c["calendar_id"] for c in body["calendars"]] == ["alice", "bob"]
    assert stamps(resp, "alice") == ["FREEBUSY:20260302T100000Z/20260302T110000Z"]
    assert stamps(resp, "bob") == ["FREEBUSY:20260303T100000Z/20260303T110000Z"]
    for entry in body["calendars"]:
        assert entry["window_start"] == "2026-03-01T00:00:00Z"
        assert entry["window_end"] == "2026-03-08T00:00:00Z"


def test_each_calendar_uses_its_own_timezone_for_all_day():
    ics = cal(event(
        "UID:d",
        "DTSTART;VALUE=DATE:20260303",
        "DTEND;VALUE=DATE:20260304",
    ))
    resp = freebusy([
        one("tokyo", ics, timezone="Asia/Tokyo"),
        one("ny", ics, timezone="America/New_York"),
    ])
    assert stamps(resp, "tokyo") == ["FREEBUSY:20260302T150000Z/20260303T150000Z"]
    assert stamps(resp, "ny") == ["FREEBUSY:20260303T050000Z/20260304T050000Z"]


def test_document_is_valid_icalendar_with_window_and_crlf():
    ics = cal(event_utc("e1", "20260302T100000Z", "20260302T110000Z"))
    resp = freebusy([one("alice", ics)])
    text = resp.json()["calendars"][0]["vfreebusy"]
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")  # CRLF only, no bare LF
    for line in text.split("\r\n")[:-1]:
        assert line  # no empty interior lines
    parsed = Calendar.from_ical(text)
    fb = next(iter(parsed.walk("VFREEBUSY")))
    assert str(fb["DTSTART"].dt) == "2026-03-01 00:00:00+00:00"
    assert str(fb["DTEND"].dt) == "2026-03-08 00:00:00+00:00"
    periods = busy_periods(text)
    assert len(periods) == 1
    assert periods[0][1] > periods[0][0]


# ---------------------------------------------------------------------------
# Errors: 422
# ---------------------------------------------------------------------------

def test_inverted_window_returns_422():
    ics = cal()
    resp = freebusy([one("alice", ics)],
                    window_start="2026-03-08T00:00:00Z",
                    window_end="2026-03-01T00:00:00Z")
    assert resp.status_code == 422
    assert "window_start" in resp.json()["detail"]


def test_unknown_calendar_timezone_returns_422():
    resp = freebusy([one("alice", cal(), timezone="Mars/Olympus_Mons")])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "alice" in detail
    assert "unknown timezone" in detail


def test_invalid_ics_returns_422():
    resp = freebusy([one("alice", "not an iCalendar document")])
    assert resp.status_code == 422
    assert "invalid iCalendar" in resp.json()["detail"]
    assert "alice" in resp.json()["detail"]


def test_unknown_tzid_inside_calendar_returns_422():
    ics = cal(event(
        "UID:a",
        "DTSTART;TZID=Mars/Olympus_Mons:20260302T100000",
        "DTEND;TZID=Mars/Olympus_Mons:20260302T110000",
    ))
    resp = freebusy([one("alice", ics)])
    assert resp.status_code == 422
    assert "unknown timezone" in resp.json()["detail"]


def test_invalid_transp_value_returns_422():
    ics = cal(event_utc("a", "20260302T100000Z", "20260302T110000Z",
                        "TRANSP:MAYBE"))
    resp = freebusy([one("alice", ics)])
    assert resp.status_code == 422
    assert "TRANSP" in resp.json()["detail"]


def test_duplicate_calendar_id_returns_422():
    ics = cal()
    resp = freebusy([one("alice", ics), one("alice", ics)])
    assert resp.status_code == 422
    assert "duplicate calendar_id" in resp.json()["detail"]


def test_empty_calendars_list_returns_422():
    resp = client.post("/freebusy-export", json={
        "window_start": "2026-03-01T00:00:00Z",
        "window_end": "2026-03-08T00:00:00Z",
        "calendars": [],
    })
    assert resp.status_code == 422


def test_missing_field_returns_422():
    resp = client.post("/freebusy-export", json={
        "window_start": "2026-03-01T00:00:00Z",
        "window_end": "2026-03-08T00:00:00Z",
        "calendars": [{"calendar_id": "alice", "ics": cal()}],  # no timezone
    })
    assert resp.status_code == 422


def test_blank_calendar_id_returns_422():
    resp = freebusy([one("", cal())])
    assert resp.status_code == 422
