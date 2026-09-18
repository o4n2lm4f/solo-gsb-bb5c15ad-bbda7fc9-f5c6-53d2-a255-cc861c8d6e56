"""VFREEBUSY export per calendar.

Each input calendar (raw ICS plus its own IANA timezone) is expanded with
the same recurrence engine used by ``/expand``. The surviving instances are
turned into busy time:

* ``STATUS:CANCELLED`` instances (cancelled master or override) never block
  busy time;
* ``TRANSP:TRANSPARENT`` instances do not block busy time. Recurring
  instances inherit the master event's ``TRANSP``; a ``RECURRENCE-ID``
  override that explicitly declares ``TRANSP`` overrides the inherited
  value for that occurrence, while one without keeps inheriting;
* timed instances contribute their UTC ``[start, end)`` span;
* ``DATE`` all-day instances are reinterpreted as whole *local* days in the
  calendar's own timezone (their authored dates are recovered from the
  expander's floating midnights and re-localized per date, so DST
  transitions produce 23/25-hour busy spans automatically).

Busy spans are clipped to the UTC query window and merged half-open:
overlapping or merely touching (adjacent) spans collapse, since
``[a, b)`` + ``[b, c)`` leave no free instant between them. The result is
rendered as one ``VFREEBUSY`` component inside a minimal ``VCALENDAR``
wrapper, with the merged ``FREEBUSY`` periods stably sorted by start/end.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .availability import _merge_intervals
from .expander import ExpansionError, Instance, expand_calendar

UTC = dt.timezone.utc
DAY = dt.timedelta(days=1)
CRLF = "\r\n"
PRODID = "-//freebusy-export//EN"

# The expander frames all-day DATEs as floating midnights normalized to UTC;
# when reinterpreted in the calendar timezone their local span can differ
# from UTC by up to the largest civil UTC offset (14h), so the expansion
# window is padded by a full day on each side to keep every all-day event
# overlapping the real window in frame. Timed instances are unaffected
# (their final clip removes anything outside the window).
ALL_DAY_PADDING = DAY


class FreeBusyError(Exception):
    """Invalid free/busy input; surfaced to the client as HTTP 422."""


@dataclass(frozen=True)
class FreeBusyCalendar:
    """One calendar's VFREEBUSY export over the shared query window."""

    calendar_id: str
    window_start: dt.datetime  # aware UTC
    window_end: dt.datetime  # aware UTC
    busy: tuple[tuple[dt.datetime, dt.datetime], ...]
    text: str  # complete VCALENDAR/VFREEBUSY text, CRLF-delimited


def export_freebusy(
    calendar_id: str,
    ics_text: str,
    tzname: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> FreeBusyCalendar:
    """Expand one calendar and return its merged, windowed busy periods."""
    try:
        tz = ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise FreeBusyError(f"unknown timezone: {tzname!r}") from exc

    try:
        instances = expand_calendar(
            ics_text,
            window_start - ALL_DAY_PADDING,
            window_end + ALL_DAY_PADDING,
        )
    except ExpansionError as exc:
        raise FreeBusyError(str(exc)) from exc

    spans: list[tuple[dt.datetime, dt.datetime]] = []
    for instance in instances:
        if instance.status == "CANCELLED" or instance.transp == "TRANSPARENT":
            continue
        if instance.all_day:
            start, end = _all_day_span(instance, tz)
        else:
            start, end = instance.start, instance.end
        start = max(start, window_start)
        end = min(end, window_end)
        if end > start:  # non-positive-duration instances contribute nothing
            spans.append((start, end))

    busy = tuple(_merge_intervals(spans))
    return FreeBusyCalendar(
        calendar_id=calendar_id,
        window_start=window_start,
        window_end=window_end,
        busy=busy,
        text=_render_vfreebusy(calendar_id, window_start, window_end, busy),
    )


# ---------------------------------------------------------------------------
# All-day DATE handling
# ---------------------------------------------------------------------------


def _all_day_span(
    instance: Instance, tz: ZoneInfo
) -> tuple[dt.datetime, dt.datetime]:
    """Whole local-day span of a DATE-valued instance in timezone ``tz``.

    The expander keeps authored DATEs as floating midnights normalized to
    UTC, so their ``.date()`` recovers exactly the calendar dates the event
    author wrote. Each boundary is built as a local midnight and converted
    per date, which lets zoneinfo apply the offset in force on that day
    (23/25-hour spans across DST transitions instead of a flat 24 hours).
    """
    first_date = instance.start.astimezone(UTC).date()
    last_date = instance.end.astimezone(UTC).date() - DAY
    start = dt.datetime.combine(first_date, dt.time.min, tzinfo=tz).astimezone(UTC)
    end = dt.datetime.combine(
        last_date + DAY, dt.time.min, tzinfo=tz
    ).astimezone(UTC)
    return start, end


# ---------------------------------------------------------------------------
# VFREEBUSY rendering
# ---------------------------------------------------------------------------


def _stamp(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _render_vfreebusy(
    calendar_id: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    busy: tuple[tuple[dt.datetime, dt.datetime], ...],
) -> str:
    """Render a minimal RFC 5545 VFREEBUSY document.

    ``FREEBUSY`` lines (default type BUSY) are emitted in the stable order
    produced by the merge: ascending start, then ascending end. Lines are
    CRLF-delimited; all values are UTC ``DATE-TIME`` stamps.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "BEGIN:VFREEBUSY",
        f"DTSTART:{_stamp(window_start)}",
        f"DTEND:{_stamp(window_end)}",
    ]
    for start, end in busy:
        lines.append(f"FREEBUSY:{_stamp(start)}/{_stamp(end)}")
    lines.extend(["END:VFREEBUSY", "END:VCALENDAR", ""])
    return CRLF.join(lines)
