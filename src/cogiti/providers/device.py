"""What the device knows about itself.

Every one of these was, until now, a question that went to a language model —
which cannot know this device's address, how long it has been running, or
whether it heard you. Asking a model was not merely wasteful; the answer was
wrong, and confidently so.

Everything here reads the running system and nothing else. No network, no
model, no configuration to drift: the device is the source of truth about
itself.
"""

import os
import shutil
import socket
import subprocess
import time

from . import Result, provider


@provider("device.ip")
def ip(**_args):
    """The address somebody could actually reach this device on.

    Found by asking the routing table which source address a packet to the
    outside would carry — not by listing interfaces, which returns loopback,
    docker bridges and whatever else exists, and leaves the caller to guess.
    The socket is never connected to anything; a UDP connect only fixes the
    route.
    """
    address = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))       # TEST-NET-1: routed, never answers
            address = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        pass

    if not address or address.startswith("127."):
        return Result.failed("unavailable")
    # Spoken aloud, so the dots are said as words rather than run together.
    return Result(values={"ip": address,
                          "ip_spoken": address.replace(".", " dot ")},
                  ttl_s=60, source="the routing table")


@provider("device.uptime")
def uptime(**_args):
    try:
        with open("/proc/uptime") as f:
            seconds = int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return Result.failed("unavailable")
    return Result(values={"seconds": seconds, "uptime_spoken": _spoken(seconds)},
                  ttl_s=30, source="/proc/uptime")


@provider("device.hostname")
def hostname(**_args):
    name = socket.gethostname()
    pretty = name
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    return Result(values={"hostname": name, "pretty_name": pretty},
                  ttl_s=3600, source="/etc/os-release")


@provider("device.disk")
def disk(path="/", **_args):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return Result.failed("unavailable")
    free_gb = usage.free / (1000 ** 3)
    return Result(values={
        "free_gb": round(free_gb, 1),
        "free_spoken": ("%.1f gigabytes" % free_gb if free_gb >= 1
                        else "%d megabytes" % int(usage.free / (1000 ** 2))),
        "percent_used": int(100 * usage.used / usage.total) if usage.total else 0,
    }, ttl_s=300, source=path)


@provider("device.memory")
def memory(**_args):
    """How much of the memory is in use.

    From MemAvailable rather than MemFree, which is the number people mean:
    MemFree counts the cache as used and reports a healthy Linux box as nearly
    full, every time, for ever.
    """
    fields = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                fields[key] = int(rest.split()[0])       # kB
    except (OSError, ValueError, IndexError):
        return Result.failed("unavailable")
    total = fields.get("MemTotal", 0)
    avail = fields.get("MemAvailable", fields.get("MemFree", 0))
    if not total:
        return Result.failed("unavailable")
    used = total - avail
    return Result(values={
        "percent_used": int(100 * used / total),
        "used_mb": used // 1024,
        "total_mb": total // 1024,
        "used_spoken": "%d percent" % int(100 * used / total),
    }, ttl_s=10)


@provider("device.load")
def load(**_args):
    """The one minute load average.

    Not a percentage and deliberately not dressed up as one: load is a queue
    length, and a device with four cores at a load of 4 is busy rather than
    broken.
    """
    try:
        with open("/proc/loadavg") as f:
            one, five, fifteen = f.read().split()[:3]
    except (OSError, ValueError):
        return Result.failed("unavailable")
    return Result(values={
        "load1": float(one), "load5": float(five), "load15": float(fifteen),
        "load_spoken": "%.2f" % float(one),
    }, ttl_s=10)


@provider("device.hearing")
def hearing(**_args):
    """Whether it can hear you.

    It can, and it knows: this provider only runs because something was heard,
    transcribed and resolved. The honest answer is yes, and saying anything
    else would be a device doubting evidence it is standing on.

    The capture device is reported alongside because "yes, and it is the
    internal microphone" is more use than "yes" when somebody is asking
    because they doubt it.
    """
    device = "the microphone"
    try:
        out = subprocess.run(["arecord", "-l"], capture_output=True,
                             timeout=3).stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            if line.startswith("card "):
                device = line.split("[", 1)[1].split("]", 1)[0] if "[" in line \
                    else device
                break
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    return Result(values={"device": device}, ttl_s=3600, source="alsa")


def _spoken(seconds):
    """Spoken aloud, so it rounds the way a person would rather than reciting
    seconds at somebody who asked a casual question."""
    if seconds < 90:
        return "%d seconds" % seconds
    minutes = seconds // 60
    if minutes < 90:
        return "%d minute%s" % (minutes, "" if minutes == 1 else "s")
    hours = minutes // 60
    if hours < 48:
        rest = minutes % 60
        out = "%d hour%s" % (hours, "" if hours == 1 else "s")
        return out + (" and %d minutes" % rest if rest else "")
    days = hours // 24
    return "%d day%s" % (days, "" if days == 1 else "s")
