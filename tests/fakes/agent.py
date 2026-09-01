#!/usr/bin/env python3
"""A fake agent adapter, and the reference implementation of the agent port.

Speaks `docs/agent-protocol.md` on real pipes, as a real process. Anyone
writing a real adapter should read this before reading the prose.

Two things about it are deliberate and neither is an accident of being a test
fixture:

**It is a process, not an object.** The port requires a separate process in its
own process group. An in-process double exercises none of what that is for —
cancellation, the group kill, the framing — which are the three things most
likely to be wrong.

**It does not import cogiti.** The encoding here is written by hand from the
document. If both sides shared a codec they would agree by construction, and a
test that proves the code equals itself proves nothing.

Usage:

    agent.py --capabilities [--script S]     print one line, exit
    agent.py --script S                      run S against the protocol

A script is JSON, because this file may not import anything cogiti has and
YAML is not in the standard library:

    {
      "capabilities": {"tools": true, "questions": true, "streaming": true},
      "steps": [
        {"emit": {"type": "thought", "text": "checking both sources"}},
        {"emit": {"type": "tool", "id": "t1", "name": "http",
                  "args": {"url": "https://api.coingecko.com/x"}}},
        {"emit": {"type": "tool", "id": "t2", "name": "http",
                  "args": {"url": "https://api.kraken.com/x"}}},
        {"await": ["t1", "t2"]},
        {"emit": {"type": "result", "say": "about 2,400 dollars"}}
      ]
    }

Every misbehaviour in the table in `architecture.md` §8 is a step, not a mode,
so a scenario reads as the thing it is testing:

    {"wait_ms": 50}              pause
    {"await": ["t1"]}            block until those ids are answered
    {"emit": {...}}              one message; "v" defaults to 1, give it to override
    {"emit_raw": "{ not json"}   unparseable, on purpose
    {"ignore_sigterm": true}     from here on, SIGTERM does nothing
    {"spawn_grandchild": true}   a process that outlives us and ignores signals
    {"flood": 100000}            a great many progress events
    {"sleep_forever": true}      never terminate
    {"exit": 3}                  leave now, with this code, no terminal event
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

V = 1


# --------------------------------------------------------------------- io --
#
# One JSON object per line, flushed. The flush is not optional: stdout to a
# pipe is block-buffered, and an unflushed `tool` request is a deadlock — we
# wait for an answer to something cogiti has not been told about yet.

def emit(obj):
    line = json.dumps(obj, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_raw(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


class Inbox:
    """Everything cogiti sends us, read on a thread so emitting never blocks.

    Answers are keyed by id because they arrive in whatever order the work
    finished, which is the rule an adapter is most likely to get wrong.
    """

    def __init__(self):
        self.answers = {}          # id -> the tool_result or answer message
        self.run = None
        self.cancelled = False
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                # cogiti sending us nonsense is cogiti's bug; say so on stderr,
                # which is the job log, and keep going.
                print("fake-agent: unparseable input: %r" % line[:200],
                      file=sys.stderr, flush=True)
                continue
            with self._cv:
                kind = msg.get("type")
                if kind == "run":
                    self.run = msg
                elif kind == "cancel":
                    self.cancelled = True
                elif "id" in msg:
                    self.answers[msg["id"]] = msg
                self._cv.notify_all()

    def wait_for_run(self, timeout=10.0):
        with self._cv:
            if not self._cv.wait_for(lambda: self.run is not None, timeout):
                fail("protocol", "no run message arrived")
            return self.run

    def wait_for(self, ids, timeout=60.0):
        with self._cv:
            ok = self._cv.wait_for(
                lambda: all(i in self.answers for i in ids) or self.cancelled,
                timeout)
        if not ok:
            fail("protocol", "timed out waiting for %s" % ",".join(ids))
        return [self.answers.get(i) for i in ids]


def fail(kind, message):
    """Terminal. `kind` is a category so failures can be counted, not a message."""
    emit({"v": V, "type": "failed", "kind": kind, "message": message})
    sys.exit(1)


# ----------------------------------------------------------------- steps --

def step_spawn_grandchild():
    """A process that outlives us and ignores SIGTERM.

    This is `jobs.md` failure mode 1. Killing our pid leaves it running;
    killing our process group does not, which is the whole point of recording
    a pgid before anything else happens.
    """
    subprocess.Popen(
        [sys.executable, "-c",
         "import signal,time\n"
         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "time.sleep(3600)\n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_steps(steps, inbox):
    for step in steps:
        if inbox.cancelled and "exit" not in step:
            # A cancelled job produces no further effects. We stop emitting;
            # the signal that follows is what actually ends us.
            print("fake-agent: cancelled, stopping", file=sys.stderr, flush=True)
            return

        if "wait_ms" in step:
            time.sleep(step["wait_ms"] / 1000.0)

        elif "await" in step:
            inbox.wait_for(step["await"])

        elif "emit" in step:
            msg = dict(step["emit"])
            msg.setdefault("v", V)      # so a script can send v:99 on purpose
            emit(msg)

        elif "emit_raw" in step:
            emit_raw(step["emit_raw"])

        elif step.get("ignore_sigterm"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

        elif step.get("spawn_grandchild"):
            step_spawn_grandchild()

        elif "flood" in step:
            for i in range(step["flood"]):
                emit({"v": V, "type": "progress", "note": "flood", "pct": 0})

        elif step.get("sleep_forever"):
            while True:
                time.sleep(3600)

        elif "exit" in step:
            sys.exit(step["exit"])

        else:
            print("fake-agent: unknown step %r" % step, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ main --

DEFAULT_CAPABILITIES = {"tools": True, "questions": True, "streaming": True}


def main(argv):
    script = {}
    if "--script" in argv:
        with open(argv[argv.index("--script") + 1]) as f:
            script = json.load(f)

    # Probed once at cogiti's startup, never per run. A script may declare one
    # capability short so that "fails loudly at startup naming it" is testable.
    if "--capabilities" in argv:
        caps = script.get("capabilities", DEFAULT_CAPABILITIES)
        emit(dict({"v": V, "type": "capabilities"}, **caps))
        return 0

    inbox = Inbox()
    inbox.wait_for_run()
    run_steps(script.get("steps", []), inbox)

    # Falling off the end without a terminal event is itself a case worth
    # testing — cogiti should record the job as failed with kind=protocol when
    # the process exits having said nothing conclusive. A script that wants a
    # clean run ends with an explicit result.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
