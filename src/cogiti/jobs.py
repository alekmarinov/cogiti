"""The registry: spawn, track, cancel, backpressure.

A job is a row before it is a process, and a process group before it is
anything else. `docs/jobs.md` §3 and §4.

Nothing here computes. A job that takes four seconds takes them in its own
process, and this module's only long operation is waiting for a signal to
land — which is bounded and explicit.
"""

import asyncio
import os
import signal
import subprocess
import time

from . import db as _db

# docs/jobs.md §5. Proposed defaults, to be argued with once there are real
# numbers — including the fact that these two cannot both bind.
LIMITS = {
    "concurrent_agent_jobs": 2,
    "concurrent_tool_jobs_per_agent": 4,
    "concurrent_jobs_total": 4,
}

TERM_GRACE_S = 5.0      # SIGTERM, then this long, then SIGKILL


class Backpressure(Exception):
    """Over a cap. The caller queues; it does not refuse and does not
    silently defer. docs/jobs.md §5."""


def _ulid():
    """Sortable and legible in a log. Not the real ULID alphabet — a
    timestamp prefix is what makes it sortable, and that is the property
    being relied on here."""
    return "%013X%s" % (int(time.time() * 1000), os.urandom(5).hex().upper())


# ----------------------------------------------------------------- spawn --

def spawn(db, kind, title, session_id, argv, parent_job=None, env=None,
          deadline_ns=None, stdin_text=None):
    """Start a job in its own process group.

    Ordering is the whole of the correctness here:

      1. the row, in `spawn`, before the fork — so a crash between fork and
         the pgid write leaves evidence rather than an untracked group;
      2. the fork, with start_new_session so the child leads its own group;
      3. the pgid, immediately, because it is what cancel signals.
    """
    _check_caps(db, kind, parent_job)

    job_id = _ulid()
    _db.insert_job(db, job_id, kind, title, session_id, parent_job, deadline_ns)

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,     # its own process group; see cancel()
    )
    _db.set_pgid(db, job_id, os.getpgid(proc.pid))

    if stdin_text is not None:
        proc.stdin.write(stdin_text)
        proc.stdin.flush()

    return job_id, proc


def _check_caps(db, kind, parent_job):
    if kind == "agent":
        n = _db.count_live(db, kind="agent")
        if n >= LIMITS["concurrent_agent_jobs"]:
            raise Backpressure("%d agent jobs already running" % n)

    if kind == "tool" and parent_job:
        n = _db.count_live(db, kind="tool", parent_job=parent_job)
        if n >= LIMITS["concurrent_tool_jobs_per_agent"]:
            # Queued silently: a tool call inside an escalation is not the
            # user's request, it is an internal step of an errand they have
            # already been told about. docs/jobs.md §5.
            raise Backpressure("fan-out cap reached for %s" % parent_job)


# ---------------------------------------------------------------- cancel --

def cancel(db, job_id, reason="cancelled"):
    """SIGTERM to -pgid, grace, SIGKILL to -pgid, reap, then verify.

    Not to the pid: a kill(pid) that leaves an agent's own subprocess running
    is the bug this exists to prevent. Cancelling a parent cancels its
    children, because each is a group under it and all of them are ended here.
    """
    row = _db.get_job(db, job_id)
    if row is None or row["state"] in _db.TERMINAL:
        return

    for child in _db.children(db, job_id):
        cancel(db, child["id"], reason)

    pgid = row["pgid"]
    if pgid:
        _signal_group(pgid, signal.SIGTERM)
        if not _group_gone(pgid, TERM_GRACE_S):
            _signal_group(pgid, signal.SIGKILL)
        _reap()
        survivors = _group_members(pgid)
        if survivors:
            # A zombie is not an escape — it has been killed and is waiting to
            # be reaped by whoever owns it. Anything else is a double-fork that
            # got away, and it will happen again.
            _db.append_log(db, job_id, "event",
                           "escaped process group: %s" % survivors)

    _db.set_state(db, job_id, "cancelled", error_kind=reason)


async def cancel_async(db, job_id, reason="cancelled"):
    """`cancel`, for a caller on the event loop.

    Identical in what it does to the process group and in what order; the only
    difference is that the grace period is awaited rather than slept through.
    That matters because the wait is up to five seconds and
    `architecture.md` §1 says the loop is a router that does not block — a
    cancel that stalls it would stall the barge-in that is meant to be able to
    interrupt anything.

    The database work stays on this thread deliberately. The sqlite connection
    is single threaded by construction, so moving the whole of this into a
    worker would need a second connection and a second set of rules about who
    may write.
    """
    row = _db.get_job(db, job_id)
    if row is None or row["state"] in _db.TERMINAL:
        return

    for child in _db.children(db, job_id):
        await cancel_async(db, child["id"], reason)

    pgid = row["pgid"]
    if pgid:
        _signal_group(pgid, signal.SIGTERM)
        if not await _group_gone_async(pgid, TERM_GRACE_S):
            _signal_group(pgid, signal.SIGKILL)
        _reap()
        survivors = _group_members(pgid)
        if survivors:
            _db.append_log(db, job_id, "event",
                           "escaped process group: %s" % survivors)

    _db.set_state(db, job_id, "cancelled", error_kind=reason)


async def _group_gone_async(pgid, timeout):
    """The same poll as `_group_gone`, yielding between attempts.

    Reaping inside the loop, as the sync one does: a killed child stays a
    zombie with the pgid it always had until its parent calls waitpid, so a
    check that does not reap first reports an escape on every cancellation.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _reap()
        if not _group_members(pgid):
            return True
        await asyncio.sleep(0.05)
    _reap()
    return not _group_members(pgid)


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass                      # already gone, which is the goal


def _group_gone(pgid, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _reap()
        if not _group_members(pgid):
            return True
        time.sleep(0.05)
    return not _group_members(pgid)


def _reap():
    """Reap anything that has exited, so the process table stops showing
    zombies. Measured: a group killed with SIGKILL still shows a member in
    state Zs until its parent reaps it, and verifying before reaping reports
    an escape on every cancellation."""
    try:
        while os.waitpid(-1, os.WNOHANG)[0]:
            pass
    except ChildProcessError:
        pass


def _group_members(pgid):
    """Live members of a process group, zombies excluded.

    Read from /proc rather than by running ps: this is on the cancellation
    path, and spawning a process to find out whether processes are gone is
    both slow and circular.
    """
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % pid) as f:
                fields = f.read().rsplit(") ", 1)[1].split()
            state, pgrp = fields[0], int(fields[2])
        except (OSError, IndexError, ValueError):
            continue
        if pgrp == pgid and state != "Z":
            out.append(int(pid))
    return out


# --------------------------------------------------------------- startup --

def recover(db):
    """Every job that was running when cogiti died is failed with `orphaned`,
    at startup, before anything else runs. Its child died with the process
    group; the table must never claim a process that is not there.

    docs/architecture.md §5.
    """
    return _db.sweep_orphans(db)
