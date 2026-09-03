"""An escalation that took too long, and what happens to its answer.

`docs/jobs.md`, and the Stage 4 brief's first decision. An escalation runs
inside the turn like it always did; if it has not finished within
`DETACH_AFTER_S` the turn stops waiting, says so, and the work carries on as
the job it already was — `adapters/agent.py` has been inserting a `job` row of
kind `agent` since the day it was written, so nothing here creates a job. It
only stops awaiting one.

**Why a deadline and not a flag.** The command table could mark an intent as
slow, and for a resolved intent that would be the better answer — data over
code, as this project prefers. But an escalation is by definition the case
where nothing resolved, so there is no row to carry the flag. A deadline needs
no foreknowledge and covers the case that actually occurs.

**Where the answer goes.** Never into a sentence the user did not ask for.
Measured on the appliance, an escalation takes 7 to 25 seconds; the person is
usually still standing there, so the common case is that it speaks. If they
are not — the session has moved on, or another turn is in flight — it becomes
a pending item and is mentioned when the next turn ends. Announcing into an
empty room is Stage 12, which is budgeted and has an off switch, and this is
not that stage.
"""

import asyncio

#: How long a turn waits before it stops waiting. Five seconds because the
#: measured median escalation on this hardware is 7 to 14 — so the slow ones
#: detach and the genuinely quick ones stay a conversation. It is one number
#: and it is meant to be argued with once there are more.
DETACH_AFTER_S = 5.0

#: What the device says when it gives up waiting. A sentence, not a spinner:
#: `docs/jobs.md` §5 is emphatic that the request is always accepted and never
#: silently deferred, and this is the audible form of that.
STILL_WORKING = "I'm still working on that. I'll tell you when I have it."


class Detached:
    """A job whose turn has ended, and the answer it will one day produce."""

    __slots__ = ("job_id", "title", "task", "session")

    def __init__(self, job_id, title, task, session):
        self.job_id = job_id
        self.title = title
        self.task = task
        self.session = session


class Pending:
    """Answers that arrived with nobody listening.

    A list rather than a callback, which is `docs/jobs.md` §7.4 stated as a
    data structure: a callback fires into whatever turn happens to be running
    and the answer lands in the wrong conversation. A list is read at a moment
    of cogiti's choosing — the end of a turn — and until then the answer simply
    waits.
    """

    def __init__(self):
        self.running = {}            # job_id -> Detached
        self.finished = []           # (Detached, result), oldest first

    def add(self, d):
        self.running[d.job_id] = d

    def done(self, job_id, result):
        d = self.running.pop(job_id, None)
        if d is not None:
            self.finished.append((d, result))

    def drop(self, job_id):
        """Cancelled, or delivered. Either way it is no longer waiting."""
        self.running.pop(job_id, None)
        self.finished[:] = [(d, r) for d, r in self.finished
                            if d.job_id != job_id]

    def take(self):
        """Everything waiting, and it stops waiting. Called once, when a turn
        ends — never while one is running, which is the whole point."""
        out, self.finished = self.finished, []
        return out

    def __len__(self):
        return len(self.running)


async def with_deadline(coro, seconds=DETACH_AFTER_S):
    """Run it, but stop waiting after `seconds`.

    Returns `(result, task)`. Exactly one is None: a result means it finished
    in time and there is nothing to detach; a task means it did not, and the
    caller now owns a job that is still running.

    `asyncio.shield` is the reason this is not two lines. `wait_for` cancels
    what it is waiting on when it times out, which is precisely the opposite of
    what is wanted here — the work must survive the wait ending. Shielding lets
    the wait expire while the task underneath runs on.
    """
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.wait_for(asyncio.shield(task), seconds), None
    except asyncio.TimeoutError:
        return None, task
    except asyncio.CancelledError:
        # The turn was interrupted, not the work. Barge-in cancels a turn, and
        # a turn that is cancelled while waiting on an escalation should take
        # the escalation with it: nobody is waiting for that answer any more.
        task.cancel()
        raise
