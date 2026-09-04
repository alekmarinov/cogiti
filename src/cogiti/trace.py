"""One structured line per turn.

`CLAUDE.md`: "It is the only way to answer 'why did it do that', and the source
of future resolver exemplars. Do not let it rot."

One line per turn, not per event: a trace with a line per event is a log, and
the thing that makes this useful is that a turn is one row you can read across.
Events accumulate into the row and it is written when the turn ends.
"""

import json
import sys
import time


class Trace:
    def __init__(self, path=""):
        self._out = open(path, "a") if path else sys.stderr
        self._rows = {}
        #: Turns whose row has moved to a job. Their remaining states belong
        #: to a turn that is over, and writing them produced a second, empty
        #: row of zero milliseconds beside every real one.
        self._moved = set()

    def _row(self, session, turn):
        return self._rows.setdefault(id(turn), {
            "session": "%s/%s" % session.key,
            "said": turn.text,
            "started_ns": time.monotonic_ns(),
            "states": [],
            "tools": [],
            "thoughts": 0,
        })

    def state(self, session, turn, state):
        if id(turn) in self._moved:
            return
        row = self._row(session, turn)
        row["states"].append(state.value)
        if state.value == "idle" and len(row["states"]) > 1:
            self._write(turn, row, "done")

    def spoke(self, session, turn, text):
        """What the device said back.

        The row held what was heard, how it resolved, which tools ran and how
        long it took — everything about a turn except its other half. So the
        one file that has a line per turn could not answer "what did it say?",
        and reading a conversation back meant the LLM dumps, which exist only
        for the turns that escalated: every fast-path answer, which is most of
        them, was spoken and written down nowhere.
        """
        if turn is None or id(turn) in self._moved:
            return
        if text:
            self._row(session, turn)["answered"] = text

    def exchange(self, session, turn, question=None, answer=None):
        """A question the device put, and what came back.

        Neither was written down anywhere. `remove the bitcoin service` was
        asked four times in a row and never completed, and the row said only
        `confirming, speaking, idle` — which is the same shape whether nobody
        answered, somebody said no, or somebody said yes and it was misheard.
        Three different faults, one indistinguishable trace.

        A confirm is the one exchange where the device speaks first, so it is
        also the one the transcript misses by construction: the answer never
        becomes a turn.
        """
        # Recording must never be able to end a turn. There is no turn at all
        # when a question is put outside one, and a trace that raises there
        # would take the question with it.
        if turn is None or id(turn) in self._moved:
            return
        row = self._row(session, turn)
        if question is not None:
            row.setdefault("questions", []).append(question)
        if answer is not None:
            row.setdefault("answers", []).append(answer)

    def decided(self, session, turn, decision):
        """What the fast path made of the utterance.

        Recorded for every turn including the escalations, because the
        interesting question later is not "what did the model answer" but
        "why did this reach the model at all" — and the answer is a verdict, a
        tier and a runner-up.
        """
        row = self._row(session, turn)
        if decision is None:
            row["resolved"] = None
            return
        row["resolved"] = {
            "intent": decision.intent_id,
            "verdict": decision.verdict,
            "tier": decision.tier,
            "confidence": decision.confidence,
        }
        if decision.missing_slot:
            row["resolved"]["missing_slot"] = decision.missing_slot
        if decision.runner_up_id:
            row["resolved"]["runner_up"] = decision.runner_up_id

    def event(self, session, turn, event):
        row = self._row(session, turn)
        kind = event.get("type")
        if kind == "tool":
            row["tools"].append(event.get("name"))
        elif kind == "question":
            row["asked"] = event.get("ask")
        elif kind == "thought":
            row["thoughts"] += 1
        elif kind == "failed":
            row["error"] = event.get("kind")
            self._write(turn, row, "failed")

    def interrupted(self, session, turn):
        self._write(turn, self._row(session, turn), "interrupted")

    def detached(self, session, turn, job_id):
        """A turn ended and its work carried on. Keep the row open under the
        job's name, so what the job goes on to do is still recorded.

        Without this a detached escalation was a hole in the trace: the row is
        written when the turn ends at five seconds, and everything the model
        then does — every tool it calls, every thought — happens afterwards
        and landed nowhere. The device did things it could not account for.
        """
        row = self._rows.pop(id(turn), None)
        if row is None:
            return
        row["job"] = job_id
        self._rows[job_id] = row
        # Bounded: a turn object is alive only while its job is, and the id
        # is dropped when the job's row is written.
        self._moved.add(id(turn))
        row["_turn"] = id(turn)

    def job_event(self, job_id, event):
        """A tool or a thought from work whose turn is over."""
        row = self._rows.get(job_id)
        if row is None:
            return
        kind = event.get("type")
        if kind == "tool":
            row["tools"].append(event.get("name"))
        elif kind == "thought":
            row["thoughts"] += 1

    def job_done(self, job_id, outcome="done", answered=None):
        """Close a detached row, with what was eventually said.

        Without the answer the transcript read as a monologue: nine of twelve
        turns in one evening's conversation were escalations, and every one
        of them recorded the question, the tools, the milliseconds — and
        "(nothing said)" — because the answer arrives a minute later, outside
        the turn, and was written down nowhere.
        """
        row = self._rows.get(job_id)
        if row is None:
            return
        if answered:
            row["answered"] = answered
        row["outcome"] = outcome
        row["ms"] = (time.monotonic_ns() - row.pop("started_ns")) // 1_000_000
        self._moved.discard(row.pop("_turn", None))
        self._out.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._out.flush()
        self._rows.pop(job_id, None)

    def _write(self, turn, row, outcome):
        row["outcome"] = outcome
        row["ms"] = (time.monotonic_ns() - row.pop("started_ns")) // 1_000_000
        self._out.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._out.flush()
        self._rows.pop(id(turn), None)
