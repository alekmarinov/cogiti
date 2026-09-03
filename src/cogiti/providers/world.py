"""Things the device has to go and find out.

Every provider here answers a question the device was previously answering
*from a language model*, which for a live number is not an approximation but a
fabrication: "what's the bitcoin price" resolved, found no command, escalated,
and came back with a confident figure out of training data and nothing to say
it was a guess.

**A provider that cannot get the answer says so.** That is the whole reason
these exist, so none of them falls back to anything — a wrong price stated
plainly is worse than no price, because the person acts on it.

The hosts are constants in this file, never anything a user or a model said,
and the fetch still goes through `trust.check` — which refuses a name that
resolves into a private range, the standard shape of a request-forgery, and
costs one function call to keep.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from . import Result, provider
from .. import trust

TIMEOUT_S = 6.0

#: Coinbase, keyless, spot price in USD. Crypto only, deliberately: it answers
#: 404 for AAPL and this says so rather than reaching for a second source with
#: an API key and a rate limit.
PRICE_HOST = "api.coinbase.com"
PRICE_URL = "https://api.coinbase.com/v2/prices/%s-USD/spot"

#: open-meteo, keyless, and already proven through the service path.
GEO_HOST = "geocoding-api.open-meteo.com"
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1"
WEATHER_HOST = "api.open-meteo.com"
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast"
               "?latitude=%.4f&longitude=%.4f&current_weather=true")

#: open-meteo's WMO codes, as a person would say them.
SKY = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
       45: "foggy", 48: "foggy", 51: "drizzling", 53: "drizzling",
       55: "drizzling", 61: "raining", 63: "raining", 65: "raining hard",
       71: "snowing", 73: "snowing", 75: "snowing hard", 77: "snowing",
       80: "showery", 81: "showery", 82: "showery", 85: "snowing",
       86: "snowing", 95: "thundery", 96: "thundery", 99: "thundery"}


def _get(url, host):
    """Fetch and decode.

    Returns the document, or `{}` when the far end answered and had nothing,
    or None when it could not be reached. The distinction is the difference
    between "I couldn't find that" and "I can't reach that right now", and
    collapsing it made a share symbol — which Coinbase answers 404 for —
    report itself as a network problem.
    """
    try:
        trust.check(url, [host], allow_private=False)
    except trust.EgressDenied:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "inteliboy"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read(64 * 1024).decode("utf-8", "replace"))
    except urllib.error.HTTPError:
        return {}                      # reached it; it does not have this
    except (urllib.error.URLError, OSError, ValueError):
        return None


@provider("price.spot")
def spot(symbol=None, **_args):
    """What one of something costs, right now, in dollars.

    Crypto only. A share symbol comes back 404 and is reported as "I can only
    get crypto prices" — which is a true sentence, unlike the number a model
    produces when asked the same thing.
    """
    if not symbol:
        return Result.failed("unavailable")
    doc = _get(PRICE_URL % symbol.upper(), PRICE_HOST)
    if doc is None:
        return Result.failed("offline")        # "I can't reach that right now."
    if "data" not in doc:
        # A symbol the source does not carry — every share, since this one is
        # crypto only. "I couldn't find that" is true; a number would not be.
        return Result.failed("not_found")
    try:
        amount = float(doc["data"]["amount"])
    except (KeyError, TypeError, ValueError):
        return Result.failed("unavailable")

    # Spoken, so it is rounded the way a person would say it: nobody reads out
    # 81090.03 and the last two digits are noise by the time it is heard.
    if amount >= 100:
        spoken = "%s dollars" % format(int(round(amount)), ",")
    elif amount >= 1:
        spoken = "%.2f dollars" % amount
    else:
        spoken = "%.4f dollars" % amount
    return Result(values={
        "symbol": symbol.upper(),
        "amount": amount,
        "amount_spoken": spoken,
        "amount_shown": ("$%s" % format(int(round(amount)), ",")
                         if amount >= 100 else "$%.2f" % amount),
    }, ttl_s=30, source=PRICE_HOST)


@provider("weather.now")
def now(location=None, **_args):
    """What it is doing outside, where the device is or where it was asked.

    Two calls, because a place name is not a coordinate: the first turns
    "Sofia" into a latitude, the second asks what the sky is doing there.
    """
    if not location:
        return Result.failed("unavailable")
    place = _get(GEO_URL % urllib.parse.quote(location), GEO_HOST)
    if place is None:
        return Result.failed("offline")
    results = place.get("results") or []
    if not results:
        return Result.failed("not_found")       # no such place
    lat = results[0].get("latitude")
    lon = results[0].get("longitude")
    name = results[0].get("name") or location
    if lat is None or lon is None:
        return Result.failed("not_found")

    doc = _get(WEATHER_URL % (lat, lon), WEATHER_HOST)
    if doc is None:
        return Result.failed("offline")
    cur = doc.get("current_weather") or {}
    if "temperature" not in cur:
        return Result.failed("unavailable")
    temp = cur["temperature"]
    sky = SKY.get(cur.get("weathercode"), "")
    return Result(values={
        "place": name,
        "temp": round(temp),
        "temp_spoken": "%d degrees" % round(temp),
        "sky": sky,
        "shown": "%d°  %s" % (round(temp), sky),
    }, ttl_s=600, source=WEATHER_HOST)
