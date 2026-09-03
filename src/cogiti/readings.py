"""What a service may ask the device about itself.

A pinned panel wants one short string — "38%", "770 GB free", "26 hours" —
and the providers produce a dict of values for speech and templates. This is
the mapping between them, and it is also the whitelist: a service may read
what is named here and nothing else.

**A service does not read the machine; it asks.** The alternative was letting
the generated code call `os` and `shutil` itself, which would have meant
widening the static checks to allow exactly the calls that make them
pointless. Asking costs a unix round trip every refresh interval — a service
polls every ten seconds at the fastest — and buys a service that can be given
a new reading without gaining a new capability.

The strings are short because they go in a corner. `docs/services.md` §1
gives a service the periphery and nothing else, and a periphery panel that
needs a sentence is a panel in the wrong place.
"""

#: name -> (provider, how to render its values). The name is what a manifest
#: asks for and what the review gate reads aloud, so it is a word a person
#: would use: "memory", not "device.memory".
READINGS = {
    "uptime":   ("device.uptime",
                 lambda v: v.get("uptime_spoken", "")),
    "disk":     ("device.disk",
                 lambda v: "%s GB free" % v.get("free_gb", "?")),
    "memory":   ("device.memory",
                 lambda v: "%d%% used" % v.get("percent_used", 0)),
    "load":     ("device.load",
                 lambda v: "load %.2f" % v.get("load1", 0.0)),
    "ip":       ("device.ip",
                 lambda v: v.get("ip", "")),
    "hostname": ("device.hostname",
                 lambda v: v.get("hostname", "")),
}


def names():
    return sorted(READINGS)


def read(name):
    """The reading as a short string, or None if it is not available.

    None rather than an exception or a placeholder: a service that cannot get
    a reading keeps whatever is already on screen, which beats replacing a
    real number with "unknown" because /proc was busy for a moment.
    """
    entry = READINGS.get(name)
    if entry is None:
        return None
    provider_name, render = entry
    from . import providers
    fn = providers.get(provider_name)
    if fn is None:
        return None
    try:
        result = fn()
    except Exception:                                         # noqa: BLE001
        return None
    if not getattr(result, "ok", False):
        return None
    try:
        text = render(result.values)
    except Exception:                                         # noqa: BLE001
        return None
    return text or None
