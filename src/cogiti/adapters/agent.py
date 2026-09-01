"""cogiti's half of the agent protocol.

`docs/agent-protocol.md` is the wire. This spawns an adapter as a job, feeds it
one `run`, reads its events, and brokers the tools it asks for.

asyncio, because `architecture.md` §1 says the event loop is a router that
does not compute and does not block — and brokering means several tool jobs
running at once while the adapter keeps streaming. A thread per tool would work
and would put the concurrency somewhere the rest of cogiti cannot see.
"""

import asyncio
import json
import os
import sys

from .. import db as _db
from .. import jobs, trust

V = 1
TOOL_RUNNERS = {
    "http": [sys.executable, "-m", "cogiti.tools.http"],
}


class ProtocolError(Exception):
    pass


class AgentRun:
    """One escalation: one adapter process, its tool jobs, and their results."""

    def __init__(self, db, argv, session_id, on_event=None):
        self.db = db
        self.argv = argv
        self.session_id = session_id
        self.on_event = on_event or (lambda e: None)
        self.job_id = None
        self.proc = None
        self._write_lock = asyncio.Lock()
        self._tools = set()          # outstanding asyncio tasks

    # --------------------------------------------------------------- run --

    async def run(self, prompt, tools, budget, allow_private=False):
        """Returns the terminal event: a `result` or a `failed`."""
        self.grants = {t["name"]: t for t in tools}
        self.allow_private = allow_private

        self.job_id = _db._ulid() if hasattr(_db, "_ulid") else jobs._ulid()
        _db.insert_job(self.db, self.job_id, "agent",
                       prompt.get("text", "")[:60] or "escalation",
                       self.session_id)

        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,        # its own group; cancel signals it
        )
        _db.set_pgid(self.db, self.job_id, os.getpgid(self.proc.pid))

        await self._send({"type": "run", "job": self.job_id, "prompt": prompt,
                          "tools": tools, "budget": budget})

        stderr = asyncio.create_task(self._drain_stderr())
        try:
            terminal = await self._read_events()
        except asyncio.CancelledError:
            # Cancelling the task does not cancel the process. Without this the
            # adapter keeps running after an interruption — still brokering
            # tools, still spending a budget, eventually writing a result into
            # a pipe nobody reads. jobs.cancel signals the group, which reaches
            # its tool jobs too, because they are groups under this one.
            jobs.cancel(self.db, self.job_id, "interrupted")
            raise
        finally:
            stderr.cancel()
            for t in list(self._tools):
                t.cancel()

        await self.proc.wait()
        return terminal

    # ------------------------------------------------------------ events --

    async def _read_events(self):
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                return self._fail("protocol", "unparseable event: %r" % line[:200])

            if msg.get("v") != V:
                return self._fail("protocol", "unknown protocol version %r" % msg.get("v"))

            kind = msg.get("type")
            if kind in ("thought", "progress"):
                # Informational, and droppable under load.
                self.on_event(msg)
                _db.append_log(self.db, self.job_id, "event",
                               "%s %s" % (kind, msg.get("text") or msg.get("note", "")))
            elif kind == "tool":
                # Traced before brokering. Without this the trace records no
                # tools at all, and "why did it do that" — the only question
                # the trace exists to answer — cannot be answered for the
                # events that actually reach the network.
                self.on_event(msg)
                # Not awaited: several may be outstanding, and answers go back
                # in whatever order the work finishes.
                self._tools.add(asyncio.create_task(self._broker(msg)))
            elif kind == "question":
                # If a caller wired a person in, ask them; otherwise say so
                # rather than leaving the adapter waiting on an answer that is
                # never coming — a pending question with no deadline is
                # jobs.md failure mode 4.
                self.on_event(msg)
                self._tools.add(asyncio.create_task(self._ask(msg)))
            elif kind == "result":
                _db.set_state(self.db, self.job_id, "done",
                              result_json=json.dumps(msg))
                self.on_event(msg)
                return msg
            elif kind == "failed":
                _db.set_state(self.db, self.job_id, "failed",
                              error_kind=msg.get("kind", "agent"),
                              error_detail=msg.get("message"))
                self.on_event(msg)
                return msg
            else:
                # An adapter that learns to say something new must not break an
                # older cogiti. Recorded, ignored.
                _db.append_log(self.db, self.job_id, "event",
                               "unknown event type %r" % kind)

        # stdout closed with no terminal event.
        return self._fail("protocol", "adapter exited without a result")

    def _fail(self, kind, message):
        _db.set_state(self.db, self.job_id, "failed",
                      error_kind=kind, error_detail=message)
        msg = {"v": V, "type": "failed", "kind": kind, "message": message}
        self.on_event(msg)
        return msg

    # ------------------------------------------------------------ broker --

    async def _broker(self, req):
        """Run one tool the adapter asked for, and answer it.

        Every refusal here is a `tool_result` with ok=false, never silence:
        an adapter waiting forever for an answer that will not come is the
        worst outcome available.
        """
        tid, name = req.get("id"), req.get("name")
        try:
            if name not in self.grants:
                # Not refused so much as absent: there is no channel to ask
                # through, and asking anyway is a security event.
                trust.audit(self.db, self.job_id, "ungranted-tool", str(name))
                raise PermissionError("tool %r was not granted to this job" % name)

            if name not in TOOL_RUNNERS:
                raise PermissionError("no runner for tool %r" % name)

            argv = list(TOOL_RUNNERS[name])
            if name == "http":
                url = (req.get("args") or {}).get("url", "")
                host = trust.check(url, self.grants[name].get("hosts", []),
                                   allow_private=self.allow_private)
                _db.append_log(self.db, self.job_id, "event", "egress %s" % host)
                argv.append(url)

            value = await self._run_tool_job(name, argv)
            await self._send({"type": "tool_result", "id": tid, "ok": True,
                              "value": value})

        except trust.EgressDenied as e:
            trust.audit(self.db, self.job_id, "egress-denied", str(e))
            await self._send({"type": "tool_result", "id": tid, "ok": False,
                              "error": {"kind": "egress", "message": str(e)}})
        except jobs.Backpressure as e:
            await self._send({"type": "tool_result", "id": tid, "ok": False,
                              "error": {"kind": "backpressure", "message": str(e)}})
        except Exception as e:                                # noqa: BLE001
            await self._send({"type": "tool_result", "id": tid, "ok": False,
                              "error": {"kind": "tool", "message": str(e)}})

    async def _ask(self, msg):
        """Put the agent's question to whoever is listening."""
        asker = getattr(self, "ask_user", None)
        if asker is None:
            await self._send({"type": "answer", "id": msg["id"], "value": None,
                              "error": "no one is available to answer"})
            return
        try:
            _db.set_state(self.db, self.job_id, "needs-input")
            value = await asker(msg)
            _db.set_state(self.db, self.job_id, "running")
            await self._send({"type": "answer", "id": msg["id"], "value": value})
        except asyncio.CancelledError:
            raise
        except Exception as e:                                # noqa: BLE001
            await self._send({"type": "answer", "id": msg["id"], "value": None,
                              "error": str(e)})

    async def _run_tool_job(self, name, argv):
        """A tool is a job: its own row, its own process group, cancellable
        with its parent. architecture.md §1 — the loop holds no long work."""
        jobs._check_caps(self.db, "tool", self.job_id)
        tool_id = jobs._ulid()
        _db.insert_job(self.db, tool_id, "tool", name, self.session_id,
                       parent_job=self.job_id)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        _db.set_pgid(self.db, tool_id, os.getpgid(proc.pid))
        out, err = await proc.communicate()
        if err:
            _db.append_log(self.db, tool_id, "err",
                           err.decode("utf-8", "replace")[:2000])
        _db.set_state(self.db, tool_id, "done" if proc.returncode == 0 else "failed",
                      error_kind=None if proc.returncode == 0 else "tool")
        try:
            return json.loads(out.decode("utf-8", "replace") or "{}")
        except ValueError:
            raise ProtocolError("tool %s did not return json" % name)

    # --------------------------------------------------------------- io --

    async def _send(self, msg):
        msg.setdefault("v", V)
        line = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        async with self._write_lock:       # several brokers answer at once
            self.proc.stdin.write(line)
            await self.proc.stdin.drain()

    async def _drain_stderr(self):
        async for raw in self.proc.stderr:
            _db.append_log(self.db, self.job_id, "err",
                           raw.decode("utf-8", "replace").rstrip())


async def capabilities(argv):
    """Probed once at startup, never per run. Returns the declared set, or
    raises — a configuration error belongs at startup, not at first use."""
    proc = await asyncio.create_subprocess_exec(
        *argv, "--capabilities",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    try:
        msg = json.loads(out.decode().strip())
    except ValueError:
        raise ProtocolError("adapter did not declare its capabilities")
    if msg.get("v") != V or msg.get("type") != "capabilities":
        raise ProtocolError("adapter declared %r, not a v%d capabilities line"
                            % (msg.get("type"), V))
    return msg


def require(declared, needed):
    """Fail loudly at startup naming the missing capability."""
    missing = [c for c in needed if not declared.get(c)]
    if missing:
        raise ProtocolError(
            "the agent adapter does not support: %s" % ", ".join(missing))
