#!/usr/bin/env python3
"""The one tool slice 2 ships: fetch a url, print a result, exit.

Spawned by cogiti as a job of kind `tool`, in its own process group, after
cogiti has already decided the url is allowed. It does not consult the
allowlist itself and must not: the decision belongs to the broker, and a tool
that could decide for itself would be a second policy to keep in step.

**It does not follow redirects, deliberately.** An allowlisted url answering
`302 Location: http://192.168.1.1/` would otherwise defeat the egress check
entirely — the check ran against the first url and the fetch lands on the
second. That is the same server-side request forgery the private-range rule
exists to stop, arriving one hop later.

So a redirect is a result, not a detour: the status and the Location come back
to the agent, which may ask for the new url through the broker like any other
request, where it is checked. Slower by one turn, and the check is never
skipped.
"""

import json
import sys
import urllib.error
import urllib.request

TIMEOUT_S = 20
MAX_BYTES = 1 << 20     # a tool that can return a gigabyte is a tool that can
                        # fill the job log and the disk behind it


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None     # urllib then raises HTTPError, which is what we want


def fetch(url):
    opener = urllib.request.build_opener(NoRedirects)
    try:
        with opener.open(url, timeout=TIMEOUT_S) as r:
            body = r.read(MAX_BYTES + 1)
            truncated = len(body) > MAX_BYTES
            return {
                "ok": True,
                "status": r.status,
                "headers": {k.lower(): v for k, v in r.headers.items()},
                "body": body[:MAX_BYTES].decode("utf-8", "replace"),
                "truncated": truncated,
            }
    except urllib.error.HTTPError as e:
        # A redirect arrives here because NoRedirects refused it. It is a
        # result the agent can act on, not a failure of the fetch.
        return {
            "ok": e.code < 400,
            "status": e.code,
            "headers": {k.lower(): v for k, v in (e.headers or {}).items()},
            "body": (e.read(MAX_BYTES) or b"").decode("utf-8", "replace"),
            "redirected_to": (e.headers or {}).get("Location"),
        }
    except urllib.error.URLError as e:
        return {"ok": False, "error_kind": "unreachable", "error": str(e.reason)}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error_kind": "tool", "error": "%s" % e}


def main(argv):
    if len(argv) != 1:
        print(json.dumps({"ok": False, "error_kind": "usage",
                          "error": "one url expected"}))
        return 2
    result = fetch(argv[0])
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
