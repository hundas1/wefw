"""CME equity-index holiday hours (NQ), so the bot is flat before early closes and idle on closed days.

Rule-based, so it keeps working in future years without edits. It follows CME's usual pattern
(times ET):
* closed all day: New Year's Day, Good Friday, Christmas;
* trading pauses at 13:00 on holiday Mondays (MLK, Presidents, Memorial, Labor), Juneteenth,
  Independence Day and Thanksgiving;
* early close at 13:15 on the day after Thanksgiving and on Christmas Eve.
CME confirms exact times about two weeks ahead; on an unplanned halt the executor's stale-data
guard still pulls working orders.
"""

from __future__ import annotations

import datetime as dt


def _Nth(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _Last(year: int, month: int, weekday: int) -> dt.date:
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _Observed(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=1) if d.weekday() == 5 else d + dt.timedelta(days=1) if d.weekday() == 6 else d


def _Easter(year: int) -> dt.date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def ClosedDays(year: int) -> set[dt.date]:
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() == 6:
        new_year += dt.timedelta(days=1)  # a Saturday New Year is not moved to Friday Dec 31
    return {new_year, _Easter(year) - dt.timedelta(days=2), _Observed(dt.date(year, 12, 25))}


def EarlyCloses(year: int) -> dict[dt.date, float]:
    """Date -> ET hour when trading stops for the day."""
    thanksgiving = _Nth(year, 11, 3, 4)
    out = {d: 13.0 for d in (_Nth(year, 1, 0, 3), _Nth(year, 2, 0, 3), _Last(year, 5, 0),
                            _Observed(dt.date(year, 6, 19)), _Observed(dt.date(year, 7, 4)),
                            _Nth(year, 9, 0, 1), thanksgiving)}
    out[thanksgiving + dt.timedelta(days=1)] = 13.25
    xmas_eve = dt.date(year, 12, 24)
    if xmas_eve.weekday() < 5 and xmas_eve not in ClosedDays(year):
        out[xmas_eve] = 13.25
    return out


def IsClosedDay(d: dt.date) -> bool:
    return d in ClosedDays(d.year)


def EarlyClose(d: dt.date) -> float | None:
    return EarlyCloses(d.year).get(d)
