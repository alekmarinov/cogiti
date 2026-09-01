"""Secrets, consent, egress policy, the audit log.

Slice 1 implements only the egress broker, because the first brokered tool
makes a real request and a security property that arrives later arrives as a
retrofit. The rest of this module is deliberately absent rather than stubbed:
an empty `check_consent` that returns True is worse than no function at all,
because it reads like a decision was made.

`docs/security.md` §6.
"""

import ipaddress
import socket
from urllib.parse import urlsplit


class EgressDenied(Exception):
    """Not an error the caller handles — a security event the caller logs.

    security.md: 'An instruction to POST to somewhere else fails at the broker,
    and the failure is logged as a security event rather than as a network
    error.' The distinction is the point: a network error invites a retry.
    """

    def __init__(self, host, reason, allowed):
        self.host, self.reason, self.allowed = host, reason, allowed
        super().__init__("egress denied: %s (%s)" % (host, reason))


def _host_of(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EgressDenied(parts.scheme or "?", "scheme is not http or https", [])
    if not parts.hostname:
        raise EgressDenied(url[:60], "no host in the url", [])
    return parts.hostname.lower().rstrip(".")


def _matches(host, pattern):
    """Exact, or one leading '*.' for a subdomain wildcard.

    '*.example.com' does not match 'example.com', deliberately, and the
    precedent is the one most people have already met: an X.509 wildcard
    certificate for *.example.com does not cover example.com either. Matching
    TLS means this allowlist behaves like the thing next to it in the stack
    rather than inventing a third convention. If you want both, write both.

    No general globbing. 'api*.example.com' would match
    'apisomethingelse.example.com', and an allowlist whose entries are hard to
    read is an allowlist nobody audits.
    """
    pattern = pattern.lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[1:]              # '.example.com'
        return host.endswith(suffix) and host != suffix[1:]
    return host == pattern


def check(url, allowed_hosts, allow_private=False):
    """Decide whether a job may reach this url. Raises, or returns the host.

    The allowlist is per job and per service — it is passed in, never read from
    global state, so a job cannot be widened by anything that happens elsewhere
    while it runs. security.md §4: tools are granted before the job starts and
    never expanded mid-run because the content suggested it.

    `allow_private` is a second grant of the same kind, and it is off unless the
    user's request was about the local network — "why is the printer not
    answering", "what is on the LAN". It is granted from what the *user* asked
    for and never from anything the agent read: a page that could talk the
    device into scanning the network it sits on is the attack the default
    exists to stop.
    """
    host = _host_of(url)

    if not allowed_hosts:
        raise EgressDenied(host, "this job declared no hosts", [])

    if not any(_matches(host, p) for p in allowed_hosts):
        raise EgressDenied(host, "not in this job's allowlist", list(allowed_hosts))

    # A literal address bypasses the name check entirely, and a name that
    # resolves to a private range is how an allowlisted host becomes a way into
    # the network the device is sitting on — the standard shape of a
    # server-side request forgery. Allowed only when this job was granted it.
    if not allow_private:
        _refuse_private(host)
    return host


def _refuse_private(host):
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None

    if addr is not None:
        if not addr.is_global:
            raise EgressDenied(host, "literal address in a non-global range", [])
        return

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Cannot resolve. Let the request fail as a network error rather than
        # inventing a verdict; nothing has been reached.
        return
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise EgressDenied(
                host, "resolves to %s, which is not a global address" % ip, [])


def audit(db, job_id, event, detail):
    """Security events go in the job log for now, tagged, and move to their own
    table when the audit log proper exists (security.md §7). Tagged rather than
    free text so that 'has anything been denied today' is a query."""
    from . import db as _db
    _db.append_log(db, job_id, "event", "%s %s" % (event, detail))
