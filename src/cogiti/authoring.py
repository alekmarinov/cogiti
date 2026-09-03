"""The device writes a service, proves it works, and asks. `services.md` §4.

Six steps, and the middle two are the reason the last one is answerable:

    1 recognise a standing want
    2 an agent writes it, in a staging directory
    3 dry run against a fake renderer
    4 static checks
    5 the review gate — a person, out loud
    6 install, record the approval with the hash

**Steps 3 and 4 exist so that step 5 is a decision about purpose rather than
about code.** The user is being asked whether they want a thing that reads
coingecko every minute. They are not being asked to audit Python — and §4 says
plainly that a gate which requires them to is a gate that will be answered yes
every time.

So nothing reaches the person that has not already been proved to run, proved
to exit cleanly, and proved to contain nothing that would make either of those
proofs meaningless.
"""

import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import time

from . import manifest as _manifest
from . import service_template as _template
from . import services as _services
from . import static_checks

#: What the model is asked to fill in. Declared here rather than in the
#: adapter because the shape of a service is cogiti's business — the adapter
#: passes it through and never learns what a service is.
PROPOSE_TOOL = {
    "name": "propose_service",
    "description":
        "Propose a service that fetches a JSON document and keeps one value "
        "from it on screen. Call this exactly once. If it comes back with a "
        "problem, fix that one thing and call it again.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "title", "url", "path", "format", "interval_s"],
        "properties": {
            "name": {"type": "string",
                     "description": "short id, lowercase and hyphens, e.g. eth-price"},
            "title": {"type": "string",
                      "description": "what the device calls it out loud, e.g. "
                                     "'the ETH price'"},
            "url": {"type": "string",
                    "description": "https url returning JSON"},
            "path": {"type": "array", "items": {"type": "string"},
                     "description": "where the value is in that document, as "
                                    "keys, e.g. ['ethereum','usd']"},
            "format": {"type": "string",
                       "description": "how it appears on screen; must contain "
                                      "{value}, e.g. 'ETH ${value}'"},
            "interval_s": {"type": "integer",
                           "description": "how often to refresh, in seconds"},
            "phrases": {"type": "array", "items": {"type": "string"},
                        "description": "up to 6 sentences the user might say "
                                       "to reach it. These are read aloud for "
                                       "approval, so keep them specific."},
        },
    },
}

SYSTEM = (
    "You are configuring a small display service for a voice appliance. You "
    "do not write code: call propose_service with the details and the device "
    "builds it. Choose a public JSON endpoint that needs no API key. If you "
    "do not know one that certainly exists, say so instead of guessing — a "
    "service pointing at a url that does not answer is worse than none."
)

#: §8: three, then it gives up and says so. An agent that can retry without
#: limit against a checker eventually produces something that passes the
#: checker rather than something that works.
MAX_ATTEMPTS = 3

#: §4 step 3: at least one valid update within its own interval, twice, and a
#: clean exit on SIGTERM. §8 caps the whole thing at 90 s — a service that
#: needs longer to prove itself is one nobody wants.
DRY_RUN_MAX_S = 90.0
REQUIRED_UPDATES = 2


class Rejected(Exception):
    """A service that will not be offered, and why in one sentence."""


# ------------------------------------------------------------ the fake --

class FakeRenderer:
    """A unix socket that accepts a connection and remembers what was said.

    Not a mock of the protocol: the SDK talks to this exactly as it talks to
    avatari, so what is proved here is the thing that will run. It only has to
    be a socket that reads lines — which is all the renderer is, from the
    writing side.
    """

    def __init__(self, path):
        self.path = path
        self.ops = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_unix_server(self._client, self.path)

    async def _client(self, reader, writer):
        # The writer is closed in a finally, and that is not tidiness: since
        # 3.12 Server.wait_closed() waits for every connection to close, so a
        # handler that returns without closing its own leaves stop() waiting
        # forever. The whole test suite hung on it.
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue                # the keepalive
                try:
                    self.ops.append(json.loads(text))
                except ValueError:
                    pass
        finally:
            try:
                writer.close()
            except Exception:                                 # noqa: BLE001
                pass

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    @property
    def updates(self):
        return [o for o in self.ops if o.get("op") == "create"]


# ------------------------------------------------------------ the runs --

async def dry_run(service_dir, m, broker_path=None):
    """Step 3. Does it actually do the thing, twice, and stop when asked?

    Twice rather than once because a service that pins one value and then
    dies, or pins one value and then loops silently, has produced a screenshot
    rather than a duty. The second update is what distinguishes them.
    """
    tmp = tempfile.mkdtemp()
    sock = os.path.join(tmp, "avatari.sock")
    fake = FakeRenderer(sock)
    await fake.start()

    env = dict(os.environ)
    env["AVATARI_SOCKET"] = sock
    env["COGITI_SERVICE"] = m.name
    env["COGITI_SERVICE_DIR"] = service_dir
    env["PYTHONPATH"] = _services.SDK_PATH
    if broker_path:
        env["COGITI_BROKER"] = broker_path

    proc = await asyncio.create_subprocess_exec(
        *m.argv, cwd=service_dir, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        preexec_fn=_services._rlimits(m, _services.service_uid()))

    # Its own interval, not a fixed wall: a service that refreshes every
    # fifteen minutes cannot show two updates in ninety seconds, so the
    # deadline is what it asked for plus room, capped by §8.
    interval = m.interval_s or 10
    deadline = min(DRY_RUN_MAX_S, max(10.0, interval * (REQUIRED_UPDATES + 1)))

    started = time.monotonic()
    try:
        while time.monotonic() - started < deadline:
            if len(fake.updates) >= REQUIRED_UPDATES:
                break
            if proc.returncode is not None:
                raise Rejected("it stopped on its own after %.0f seconds"
                               % (time.monotonic() - started))
            await asyncio.sleep(0.25)
        else:
            raise Rejected(
                "it produced %d update(s) in %.0f seconds and needs %d"
                % (len(fake.updates), deadline, REQUIRED_UPDATES))

        # §4: and exits cleanly on SIGTERM. A service that has to be killed is
        # a service the supervisor will have to kill every time.
        await _services._end_group(proc)
        if proc.returncode not in (0, -15, 143):
            raise Rejected("it did not exit cleanly when asked to stop "
                           "(status %s)" % proc.returncode)
        return fake.updates
    finally:
        if proc.returncode is None:
            await _services._end_group(proc)
        await fake.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def review(m, updates):
    """Step 5's sentence. §4 gives the shape and the reason for every clause.

    The phrases are in it because they are **part of the decision, not a detail
    of it**: a manifest's patterns decide which sentences stop going to the
    model, and a price ticker that quietly claimed "what's the time" would take
    that utterance whenever no built-in matched. Reading three phrases costs a
    second — unlike the code, which is why this is about purpose.
    """
    # Built from the parts rather than from the title, because a title is a
    # noun phrase — "the weather" — and pasting it after "a service that"
    # produces a sentence nobody can parse, which is a poor thing to read to
    # somebody you are asking for consent.
    if m.allow:
        what = "reads %s from %s" % (m.title, " and ".join(m.allow))
    else:
        what = "shows %s" % m.title
    if m.interval_s:
        what += ", every %s," % _every(m.interval_s)
    bits = ["I've written a service that %s and pins it in the corner." % what]

    if m.secrets:
        bits.append("It needs: %s." % ", ".join(m.secrets))
    else:
        bits.append("It needs no passwords.")
    if m.phrases:
        bits.append("It will answer when you say %s."
                    % " or ".join("'%s'" % p for p in m.phrases))
    bits.append("Shall I keep it?")
    return " ".join(bits)


def _every(seconds):
    if seconds % 3600 == 0:
        n = seconds // 3600
        return "hour" if n == 1 else "%d hours" % n
    if seconds % 60 == 0:
        n = seconds // 60
        return "minute" if n == 1 else "%d minutes" % n
    return "%d seconds" % seconds


# --------------------------------------------------------------- steps --

async def from_spec(spec, staging_root):
    """Steps 2: the form becomes two files. Raises BadSpec or Rejected.

    The model never writes Python. What it supplies reaches the running
    program only inside a string literal, put there by repr(), and the
    manifest's allow-list is derived from the url it gave rather than
    declared separately — so the code and its description cannot disagree
    about which host is reached.
    """
    spec = _template.validate(spec)
    code, man = _template.render(spec)
    d = os.path.join(staging_root, spec["name"])
    if os.path.exists(d):
        raise Rejected("a service called %s is already being written"
                       % spec["name"])
    os.makedirs(d)
    with open(os.path.join(d, "main.py"), "w") as f:
        f.write(code)
    with open(os.path.join(d, "service.toml"), "w") as f:
        f.write(man)
    return d, spec


async def vet(staging_dir, broker_path=None):
    """Steps 3 and 4, in the order that fails cheapest first.

    Static checks before the dry run, deliberately: reading the source costs
    milliseconds and running unknown code costs up to ninety seconds and
    whatever the code does. A service that imports subprocess should never be
    started to find that out.
    """
    path = os.path.join(staging_dir, "service.toml")
    m = _manifest.load(path)                       # raises ManifestError

    entry = None
    for a in m.argv:
        if a.endswith(".py"):
            entry = a
            break
    if entry is None:
        raise Rejected("its exec line runs no python file")

    try:
        with open(os.path.join(staging_dir, entry)) as f:
            source = f.read()
    except OSError as e:
        raise Rejected("cannot read %s: %s" % (entry, e))

    try:
        static_checks.check(source, m)
    except static_checks.Refusal as e:
        raise Rejected(str(e))

    updates = await dry_run(staging_dir, m, broker_path)
    return m, updates


def install(staging_dir, services_root, m, spoken):
    """Step 6: move it into place, record the approval, and only now does it
    exist. The hash is taken after the move, over the files that will run."""
    dest = os.path.join(services_root, m.name)
    if os.path.exists(dest):
        raise Rejected("a service called %s is already installed" % m.name)
    os.makedirs(services_root, exist_ok=True)
    shutil.move(staging_dir, dest)

    from . import approval
    entry = next(a for a in m.argv if a.endswith(".py"))
    approval.record(dest, spoken, m.phrases, m.allow, m.secrets, entry=entry)
    return dest


# ------------------------------------------------------- the whole thing --

class Author:
    """One authoring request: the model fills the form, cogiti judges it.

    The retry loop is not a loop in this file. The model calls
    `propose_service`; if what it sent will not do, the reason goes back as the
    tool's result and it calls again — which is `services.md` §8's three
    attempts happening through the protocol that already existed, with no
    retry machinery written for it.
    """

    def __init__(self, cogiti, staging_root):
        self.cogiti = cogiti
        self.staging_root = staging_root
        self.attempts = 0
        self.ready = None            # (staging_dir, manifest) once vetted

    async def propose(self, args):
        """The tool. Returns what the model is told; never raises at it."""
        if self.ready is not None:
            return {"ok": False,
                    "problem": "you have already proposed one that works; "
                               "call answer now"}
        self.attempts += 1
        if self.attempts > MAX_ATTEMPTS:
            return {"ok": False,
                    "problem": "that was the last of %d attempts" % MAX_ATTEMPTS}

        staged = None
        name = None
        try:
            staged, spec = await from_spec(args, self.staging_root)
            name = spec["name"]
            # The broker has to answer for it during the dry run. It is not
            # installed and never may be — that is what the dry run decides —
            # so it is registered for the length of the run and forgotten
            # after, whichever way it goes.
            self.cogiti.register_manifest(_manifest.load(
                os.path.join(staged, "service.toml")))
            m, updates = await vet(staged, self.cogiti.broker_path)
        except (_template.BadSpec, Rejected, _manifest.ManifestError) as e:
            if staged:
                shutil.rmtree(staged, ignore_errors=True)
            if name:
                self.cogiti.forget_manifest(name)
            # Logged as well as returned. The model is told what was wrong and
            # the user is not, so without this the only trace of three failed
            # attempts is the sentence saying there were three.
            print("(authoring: %s)" % e, file=sys.stderr, flush=True)
            # One thing, in words it can act on. It has %d tries left, and a
            # wall of complaints is a rewrite that fixes none of them.
            return {"ok": False, "problem": str(e),
                    "attempts_left": MAX_ATTEMPTS - self.attempts}
        self.ready = (staged, m, updates)
        return {"ok": True,
                "note": "it runs and pins a value. Call answer now; the user "
                        "will be asked whether to keep it."}
