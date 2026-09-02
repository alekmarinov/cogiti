"""Time and date. The one provider family that needs nothing from anywhere.

Named `clock` rather than `time` so the module does not shadow the standard
library's inside this package — the same mistake that made `cogiti/tools/http.py`
break `urllib`, and worth not making twice.
"""

import time

from . import Result, provider


@provider("clock.now")
def now(**_args):
    t = time.localtime()
    return Result(
        values={
            "time_24h": time.strftime("%H:%M", t),
            # What a person says. "It's 14:05" is a clock reading; "it's five
            # past two" is an answer, and this is spoken aloud.
            "time_spoken": _spoken(t),
            "hour": t.tm_hour, "minute": t.tm_min,
        },
        # True for under a minute, and memory should know that before it
        # decides to remember it.
        ttl_s=30, source="device clock")


@provider("clock.today")
def today(**_args):
    t = time.localtime()
    return Result(values={"date_iso": time.strftime("%Y-%m-%d", t),
                          "date_spoken": time.strftime("%A, %-d %B", t),
                          "weekday": time.strftime("%A", t)},
                  ttl_s=3600, source="device clock")


def _spoken(t):
    h, m = t.tm_hour, t.tm_min
    h12 = h % 12 or 12
    if m == 0:
        return "%d o'clock" % h12
    if m == 15:
        return "quarter past %d" % h12
    if m == 30:
        return "half past %d" % h12
    if m == 45:
        return "quarter to %d" % ((h12 % 12) + 1)
    if m < 30:
        return "%d minute%s past %d" % (m, "" if m == 1 else "s", h12)
    m = 60 - m
    return "%d minute%s to %d" % (m, "" if m == 1 else "s", (h12 % 12) + 1)
