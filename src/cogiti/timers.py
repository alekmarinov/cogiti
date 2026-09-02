"""Timers: the first job that is not an agent.

`docs/command-table.md` draws the line and `set_timer` falls the wrong side of
it — *"if a command needs to keep running after the answer, it is not a
command"*. A timer outlives its turn by design, so it is a job.

**It is a real process, not an asyncio task.** `sleep 300` in its own process
group looks wasteful for something a `call_later` could do, and it is the right
shape anyway: it gets a row, a pgid, a deadline and the whole of `jobs.cancel`
— the TERM, the grace, the KILL, the reap and the verify — without any of that
being written a second time for a special case. The job engine has only ever
run agent jobs and their tools; a timer is the cheapest way to find out whether
it works for anything else.

**Firing is the first thing cogiti says without being asked.** Everything until
now has been a reply inside a turn. That is stage 12's whole subject and this
is the smallest possible instance of it, so it is deliberately dumb: it speaks
once, when the timer is up, and never at any other time.
"""

import asyncio

from . import db as _db
from . import jobs

KIND = "timer"


def human(seconds):
    """'5 minutes'. Spoken aloud, so it rounds the way a person would."""
    seconds = int(seconds)
    if seconds < 60:
        return "%d second%s" % (seconds, "" if seconds == 1 else "s")
    if seconds % 3600 == 0:
        h = seconds // 3600
        return "%d hour%s" % (h, "" if h == 1 else "s")
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        if not s:
            return "%d minute%s" % (m, "" if m == 1 else "s")
        return "%d minute%s %d second%s" % (m, "" if m == 1 else "s",
                                            s, "" if s == 1 else "s")
    h, rest = divmod(seconds, 3600)
    m = rest // 60
    out = "%d hour%s" % (h, "" if h == 1 else "s")
    return out + (" %d minute%s" % (m, "" if m == 1 else "s") if m else "")


def running(db):
    """Live timers, most recent first. The set a person can refer to."""
    return [j for j in _db.live_jobs(db) if j["kind"] == KIND][::-1]


class Timers:
    def __init__(self, cogiti):
        self.cogiti = cogiti
        self._waiters = {}

    def set(self, session_id, seconds, on_fire):
        """Start one. Returns (job_id, title)."""
        seconds = int(seconds)
        # "the timer for 4 seconds", not "the 4 seconds timer". The spoken
        # duration is a noun phrase and reads wrong as an adjective, and this
        # string is what the device calls the job out loud.
        title = "the timer for %s" % human(seconds)
        deadline = _db.now_ns() + seconds * 1_000_000_000
        job_id, proc = jobs.spawn(
            self.cogiti.db, KIND, title, session_id,
            ["sleep", str(seconds)], deadline_ns=deadline)
        self._waiters[job_id] = asyncio.ensure_future(
            self._wait(job_id, title, proc, on_fire))
        return job_id, title

    async def _wait(self, job_id, title, proc, on_fire):
        """Wait for the sleep to end, then say so — unless it was cancelled.

        The state is re-read from the table rather than inferred from the exit
        code. A cancelled timer's `sleep` dies of SIGTERM and exits non-zero,
        but so would one killed by anything else, and 'the timer you cancelled
        went off anyway' is the one outcome worth being careful about.
        """
        try:
            await asyncio.to_thread(proc.wait)
        except asyncio.CancelledError:
            return
        finally:
            self._waiters.pop(job_id, None)

        row = _db.get_job(self.cogiti.db, job_id)
        if row is None or row["state"] != "running":
            return                       # cancelled, or already accounted for
        _db.set_state(self.cogiti.db, job_id, "done")
        await on_fire(job_id, title)

    def cancel(self, job_id, reason="cancelled by the user"):
        task = self._waiters.pop(job_id, None)
        if task:
            task.cancel()
        return jobs.cancel(self.cogiti.db, job_id, reason)

    async def shutdown(self):
        """Cancel every live timer and wait for the cancellations to settle.

        Awaited, not fired and forgotten: a cancelled task is not a finished
        one, and closing the loop between the two is what produces "Task was
        destroyed but it is pending" — a warning that is also a real leak,
        since the `sleep` behind it has not been reaped yet.
        """
        tasks = [t for t in self._waiters.values() if not t.done()]
        for job_id in list(self._waiters):
            self.cancel(job_id, "cogiti stopped")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
