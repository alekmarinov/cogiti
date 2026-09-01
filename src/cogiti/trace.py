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
        row = self._row(session, turn)
        row["states"].append(state.value)
        if state.value == "idle" and len(row["states"]) > 1:
            self._write(turn, row, "done")

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

    def _write(self, turn, row, outcome):
        row["outcome"] = outcome
        row["ms"] = (time.monotonic_ns() - row.pop("started_ns")) // 1_000_000
        self._out.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._out.flush()
        self._rows.pop(id(turn), None)
