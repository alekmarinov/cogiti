"""Starting services, keeping them up, and stopping when that is the kinder act.

`docs/services.md` §6 and §7. A service is a standing duty: started at boot,
restarted when it crashes, removed by asking.

Two rules here are the ones that matter, and both are about *not* being
persistent enough:

**Three failures inside a minute stops it.** A crash loop that continues
forever is worse than a service that is off, because nobody finds out about
it — the panel flickers, the log fills, and the device says nothing. Stopped
and marked `needs-attention` is a state a person can be told about.

**A service that exceeds its budget is killed and treated as a crash.** Which
means the backoff and the crash-loop rule apply to it too, so a service that
is over budget every time it runs stops on the third attempt rather than
being killed every minute forever.
"""

import asyncio
import os
import pwd
import resource
import shutil
import signal
import time

from . import approval as _approval
from . import manifest as _manifest

#: Where the SDK is, so a service can import it.
#:
#: A service is started as a bare `python3 main.py`, and the first line of
#: every one of them is `from cogiti.service import Service`. cogiti's own
#: launcher puts itself on sys.path rather than in PYTHONPATH, so a child
#: inherits nothing — both shipped services died on the import, three times
#: each, and were correctly stopped by the crash-loop rule with no hint of
#: why in the log.
SDK_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: §6, exactly: 1 s, 2 s, 4 s, to a 60 s ceiling.
BACKOFF_START_S = 1.0
BACKOFF_CEILING_S = 60.0

#: Three failures inside this window and it stops for good.
CRASH_WINDOW_S = 60.0
CRASH_LIMIT = 3

#: SIGTERM, then this long, then SIGKILL — to the group, never the pid.
TERM_GRACE_S = 5.0

#: How often the supervisor reads a service's CPU usage. The manifest's
#: cpu_seconds is a rate — seconds of CPU per minute — which RLIMIT_CPU cannot
#: express at all, being a lifetime total. So it is sampled instead.
CPU_SAMPLE_S = 10.0

RUNNING = "running"
STOPPED = "stopped"
NEEDS_ATTENTION = "needs-attention"
PAUSED = "paused"


#: The account a service runs as. services.md §1: a service has a uid. One
#: shared account rather than one each — real isolation between services is
#: more, and this is the part that matters most, because without it a service
#: can simply open /var/lib/cogiti/secrets.
SERVICE_USER = "cogiti-service"


def service_uid():
    """The uid and gid to drop to, or None if the account is not there.

    None is a real answer and not an error: a development checkout has no such
    user, and refusing to run services there would make them untestable on the
    machine they are written on. What must not happen is *silently* running as
    root while believing otherwise — so the caller says which it got.
    """
    try:
        e = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        return None
    return e.pw_uid, e.pw_gid


def _rlimits(m, ids=None):
    """Applied in the child, between fork and exec.

    setrlimit rather than a cgroup because this must work on an appliance with
    no systemd and no cgroup writer, and because the three that map cleanly
    are the three that matter for a script that has gone wrong: memory, file
    descriptors, and the number of processes it may become.
    """
    def apply():
        # Order matters and is the usual trap: the group first, then the
        # supplementary groups, then the user. Dropping the uid first loses
        # the privilege needed to do the rest, and the result is a process
        # that kept the group it should not have.
        if ids is not None:
            uid, gid = ids
            os.setgid(gid)
            os.setgroups([gid])
            os.setuid(uid)
        mb = m.limits["memory_mb"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mb, mb))
        n = m.limits["open_files"]
        resource.setrlimit(resource.RLIMIT_NOFILE, (n, n))
        p = m.limits["processes"]
        resource.setrlimit(resource.RLIMIT_NPROC, (p, p))
        # Its own process group, so stopping it stops everything it started.
        # §9: a service that leaves a grandchild running is a bug in the SDK,
        # and this is what makes that recoverable rather than permanent.
        os.setsid()
    return apply


class Supervised:
    """One service, and everything cogiti knows about how it has behaved."""

    def __init__(self, m):
        self.m = m
        self.proc = None
        self.state = STOPPED
        self.backoff = BACKOFF_START_S
        self.failures = []           # monotonic times of recent exits
        self.detail = ""             # why it is in the state it is in
        self.recent = []             # its last lines, for "is it working?"
        self._logs = []
        self.started_ns = 0
        self._cpu_at_start = 0.0
        self._task = None

    @property
    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    def note_failure(self, why):
        now = time.monotonic()
        self.failures = [t for t in self.failures if now - t < CRASH_WINDOW_S]
        self.failures.append(now)
        self.detail = why
        return len(self.failures) >= CRASH_LIMIT


class Services:
    """The supervisor. One per cogiti."""

    def __init__(self, root, on_warn=None, broker=None, drop_privileges=True):
        self.ids = service_uid() if drop_privileges else None
        self.root = root
        self.on_warn = on_warn or (lambda m: None)
        self.broker = broker
        self.services = {}          # name -> Supervised
        self.broken = []            # manifests that would not load

    # ------------------------------------------------------------- load --

    def load(self):
        if self.ids is None and os.geteuid() == 0:
            # Said once, loudly. A device running services as root while its
            # manifest format promises a uid is a device whose security
            # description is wrong, and nobody would find out by looking.
            self.on_warn("no %s account: services will run as root"
                         % SERVICE_USER)
        found, broken = _manifest.load_all(self.root)
        self.broken = broken
        for msg in broken:
            # Loudly. A service that will not start because its manifest is
            # wrong is exactly the thing that must not fail quietly.
            self.on_warn("service manifest rejected: %s" % msg)
        if len(found) > _manifest.MAX_SERVICES:
            self.on_warn("%d services installed; the limit is %d"
                         % (len(found), _manifest.MAX_SERVICES))
        for name, m in found.items():
            if name not in self.services:
                self.services[name] = Supervised(m)
            else:
                self.services[name].m = m
        return list(found)

    # ------------------------------------------------------------ start --

    async def start_all(self):
        """§6: at boot, in manifest order, after the adapters and the network.

        Order is not dependency order and must not be: the SDK reconnects, so a
        service starting before the renderer is a thing to survive rather than
        to sequence. Sequencing it would make the boot as slow as its slowest
        member.
        """
        for s in self.services.values():
            if s.state != PAUSED:
                await self.start(s.m.name)

    async def start(self, name):
        s = self.services.get(name)
        if s is None or s.alive:
            return s

        # §4: a service whose files no longer match its approval does not
        # start. The case this guards is an agent editing a service it wrote
        # last week — which is a new approval, not a continuation of the old
        # one — and the manifest is covered as well as the code, because a
        # service that widens its phrase list has changed what the device
        # hears without moving a line.
        #
        # Services that were installed by hand carry no approval and are
        # allowed: 5a shipped two of them, and a rule that stopped the device
        # from running what its owner put there by hand would be a rule about
        # the wrong thing.
        if _approval.load(s.m.dir) is not None:
            ok, why = _approval.verify(s.m.dir)
            if not ok:
                s.state = NEEDS_ATTENTION
                s.detail = why
                self.on_warn("%s will not start: %s" % (name, why))
                return s
        env = dict(os.environ)
        env["COGITI_SERVICE"] = s.m.name
        env["COGITI_SERVICE_DIR"] = s.m.dir
        env["PYTHONPATH"] = os.pathsep.join(
            [SDK_PATH] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        if self.broker:
            # How it reaches the network: by asking, never by connecting. The
            # allow-list in the manifest is enforced on the far end of this
            # socket, which is the only place it can be.
            env["COGITI_BROKER"] = self.broker

        try:
            s.proc = await asyncio.create_subprocess_exec(
                *s.m.argv, cwd=s.m.dir, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=_rlimits(s.m, self.ids))
        except OSError as e:
            s.state = NEEDS_ATTENTION
            s.detail = "will not start: %s" % e
            self.on_warn("%s %s" % (name, s.detail))
            return s

        s.state = RUNNING
        s.detail = ""
        s.started_ns = time.monotonic_ns()
        s._cpu_at_start = _cpu_of(s.proc.pid)
        s._task = asyncio.ensure_future(self._watch(s))
        # Read what it says, or the pipe fills and the service stops on a
        # write. §9: a service silent about failing is the failure mode this
        # whole design is trying to avoid — and a supervisor that captures the
        # reason and shows nobody is the same failure wearing a hat.
        s._logs = [asyncio.ensure_future(self._drain(s, s.proc.stderr, "err")),
                   asyncio.ensure_future(self._drain(s, s.proc.stdout, "out"))]
        return s

    async def _drain(self, s, stream, which):
        """One line at a time, kept and warned about.

        Only stderr is warned about: a service printing to stdout is being
        chatty, and a service printing to stderr is telling somebody
        something.
        """
        try:
            async for raw in stream:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                s.recent.append(line)
                del s.recent[:-20]
                if which == "err":
                    self.on_warn("%s: %s" % (s.m.name, line))
        except (asyncio.CancelledError, ValueError):
            pass

    async def _watch(self, s):
        """Wait for it to die, then decide whether to bring it back."""
        try:
            await s.proc.wait()
        except asyncio.CancelledError:
            return
        if s.state in (STOPPED, PAUSED):
            return                      # we stopped it; that is not a crash

        code = s.proc.returncode
        too_many = s.note_failure("exited with %s" % code)
        if too_many:
            s.state = NEEDS_ATTENTION
            # §6: the panel is gone, and the device mentions it next time it
            # is spoken to. Gone because objects belong to the connection that
            # made them, so the renderer drops them when the process dies —
            # which is also how unpinning works and needs no protocol at all.
            self.on_warn("%s stopped after %d failures in %ds: %s"
                         % (s.m.name, CRASH_LIMIT, int(CRASH_WINDOW_S),
                            s.detail))
            return

        delay = s.backoff
        s.backoff = min(s.backoff * 2, BACKOFF_CEILING_S)
        self.on_warn("%s %s; restarting in %.0fs" % (s.m.name, s.detail, delay))
        await asyncio.sleep(delay)
        if s.state not in (STOPPED, PAUSED):
            await self.start(s.m.name)

    # -------------------------------------------------------------- stop --

    async def stop(self, name, state=STOPPED):
        s = self.services.get(name)
        if s is None:
            return None
        s.state = state
        if s._task:
            s._task.cancel()
        if s.alive:
            await _end_group(s.proc)
        return s

    async def stop_all(self):
        for name in list(self.services):
            await self.stop(name)

    # ------------------------------------------------------------- cpu --

    async def sample_cpu(self):
        """The manifest's cpu_seconds, which is a rate and not a total.

        RLIMIT_CPU is a lifetime ceiling: a service allowed 30 CPU-seconds
        would be killed after some hours of ordinary work, for a reason nobody
        would connect to this setting. So usage is read per interval and
        compared against what the budget allows over that interval.

        Over budget is treated as a crash — deliberately — so the backoff and
        the three-strikes rule apply, and something that is over every time it
        runs stops rather than being killed forever.
        """
        for s in list(self.services.values()):
            if not s.alive:
                continue
            used = _cpu_of(s.proc.pid) - s._cpu_at_start
            elapsed = (time.monotonic_ns() - s.started_ns) / 1e9
            if elapsed < CPU_SAMPLE_S:
                continue
            allowed = s.m.limits["cpu_seconds"] * (elapsed / 60.0)
            if used > allowed:
                self.on_warn("%s used %.1fs of cpu in %.0fs, budget %.1fs"
                             % (s.m.name, used, elapsed, allowed))
                s.note_failure("over its cpu budget")
                await _end_group(s.proc)

    async def run_sampler(self):
        while True:
            await asyncio.sleep(CPU_SAMPLE_S)
            try:
                await self.sample_cpu()
            except Exception as e:                            # noqa: BLE001
                self.on_warn("cpu sampling failed: %s" % e)

    # ----------------------------------------------------------- remove --

    async def remove(self, name, removed_root):
        """§7, and step 3 is the one to get right.

        The directory is moved rather than deleted: removal is a voice command
        and voice commands are misheard, so thirty days of undo costs a few
        kilobytes. What must not survive is anything cogiti holds *about* it —
        a secret grant belonging to nothing, or a pattern that resolves to a
        service that is gone.
        """
        s = self.services.get(name)
        if s is None:
            return None
        await self.stop(name)
        title = s.m.title
        stamp = time.strftime("%Y%m%dT%H%M%S")
        os.makedirs(removed_root, exist_ok=True)
        shutil.move(s.m.dir, os.path.join(removed_root, "%s-%s" % (name, stamp)))
        del self.services[name]
        # Nothing else to revoke today: grants and patterns are derived from
        # the manifest, which has just gone. That is the point of §2 — there is
        # no second place holding them, so there is no second place to forget.
        return title


def _cpu_of(pid):
    """User + system jiffies for a process and its dead children, in seconds."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            parts = f.read().rsplit(") ", 1)[-1].split()
        ticks = sum(int(parts[i]) for i in (11, 12, 13, 14))   # utime..cstime
        return ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return 0.0


async def _end_group(proc):
    """SIGTERM to the group, five seconds, then SIGKILL. §6.

    To the group because the service leads one — os.setsid in the child — so
    anything it started goes with it. Killing the pid alone is how a
    grandchild survives its parent.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), TERM_GRACE_S)
            return
        except asyncio.TimeoutError:
            continue
