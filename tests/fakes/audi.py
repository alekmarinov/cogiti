#!/usr/bin/env python3
"""A fake speech adapter, and the reference implementation of the speech port.

Speaks `docs/speech-protocol.md` on real pipes, as a real long-lived process.
Anyone writing a real one should read this before reading the prose.

Three things about it are deliberate:

**It is long-lived.** Unlike the agent fake, it is not spawned per unit of work
— it is a device. It starts, holds its imaginary hardware, and only stops when
cogiti stops. The failure that matters is therefore *dying and being missed*,
so `{"exit": 1}` is a step and cogiti is expected to notice.

**It does not import cogiti.** The encoding here is written by hand from the
document, so agreement between the two sides is evidence rather than tautology.

**It can misbehave in the ways the document forbids**, because those are the
interesting tests: partials that rewrite themselves, a `speech_start` that
never becomes words, its own voice reported as a transcript.

A scenario is JSON:

    {
      "capabilities": {"partials": true, "barge_in": true},
      "steps": [
        {"wait_ms": 50},
        {"emit": {"type": "speech_start"}},
        {"partials": ["turn the", "turn the volume", "turn the volume up"]},
        {"emit": {"type": "speech_end"}},
        {"emit": {"type": "final", "text": "turn the volume up", "ms": 900}}
      ]
    }

Steps:

    {"wait_ms": 50}              pause
    {"emit": {...}}              one message; "v" defaults to 1
    {"emit_raw": "{ not json"}   unparseable, on purpose
    {"partials": ["a","a b"]}    a growing partial per entry
    {"say_back": true}           echo any `say` as `speaking` with fake marks
    {"await_say": true}          block until cogiti asks us to speak
    {"dump_commands": "<path>"}  write every command cogiti sent, for the test
    {"expect_stop": true}        block until cogiti sends `stop`, then report
    {"barge_in": {...}}          speech_start while speaking; stops own audio
                                 first, exactly as the protocol requires
    {"exit": 1}                  die, so cogiti has to notice
"""

import json
import sys
import threading
import time

V = 1


def emit(obj):
    obj.setdefault("v", V)
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def now_ns():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


class Inbox:
    """Everything cogiti sends, read on a thread so emitting never blocks."""

    def __init__(self):
        self.commands = []
        self.speaking = None            # the `say` we are pretending to play
        self.stopped = threading.Event()
        self.said = threading.Event()
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
                continue
            with self._cv:
                self.commands.append(msg)
                kind = msg.get("type")
                if kind == "say":
                    self.speaking = msg
                    self.said.set()
                elif kind == "stop":
                    self.speaking = None
                    self.stopped.set()
                self._cv.notify_all()

    def wait_say(self, timeout=10.0):
        return self.said.wait(timeout)

    def wait_stop(self, timeout=10.0):
        return self.stopped.wait(timeout)


def speak_back(inbox):
    """Answer a `say` with `speaking`, as a real adapter does after it has
    started playing. The marks are invented; their shape is not."""
    msg = inbox.speaking
    if not msg:
        return
    text = msg.get("text", "")
    seconds = max(0.2, len(text) * 0.06)
    emit({"type": "speaking", "id": msg.get("id"),
          "visemes": [[0.0, "AA"], [round(seconds, 3), "sil"]],
          "audio_start_ns": now_ns() + 120_000_000,
          "seconds": round(seconds, 3)})


def run(script, inbox):
    for step in script.get("steps", []):
        if "wait_ms" in step:
            time.sleep(step["wait_ms"] / 1000.0)

        elif "emit" in step:
            msg = dict(step["emit"])
            msg.setdefault("at_ns", now_ns())
            emit(msg)

        elif "emit_raw" in step:
            sys.stdout.write(step["emit_raw"] + "\n")
            sys.stdout.flush()

        elif "partials" in step:
            for text in step["partials"]:
                emit({"type": "partial", "text": text,
                      "stable": step.get("stable", True)})
                time.sleep(step.get("gap_ms", 20) / 1000.0)

        elif step.get("await_say"):
            if not inbox.wait_say(step.get("timeout_s", 10)):
                emit({"type": "error", "kind": "model",
                      "message": "cogiti never asked us to speak"})

        elif step.get("say_back"):
            speak_back(inbox)

        elif step.get("expect_stop"):
            ok = inbox.wait_stop(step.get("timeout_s", 5))
            emit({"type": "error", "kind": "model",
                  "message": "stop %s" % ("received" if ok else "never came")})

        elif "barge_in" in step:
            # The protocol's §5: our own playback stops *before* cogiti is
            # told, because a round trip is time spent talking over someone.
            inbox.speaking = None
            emit({"type": "speech_start"})
            for text in step["barge_in"].get("partials", []):
                emit({"type": "partial", "text": text, "stable": True})
            emit({"type": "speech_end"})
            if "final" in step["barge_in"]:
                emit({"type": "final", "text": step["barge_in"]["final"],
                      "ms": 500})

        elif "dump_commands" in step:
            # Everything cogiti has sent us, written where the test can read
            # it. A test that wants to prove the microphone was muted needs
            # the adapter's own account of what it was told, not cogiti's.
            with open(step["dump_commands"], "w") as f:
                json.dump(inbox.commands, f)

        elif "exit" in step:
            sys.exit(step["exit"])

        else:
            print("fake-audi: unknown step %r" % step, file=sys.stderr,
                  flush=True)


def main(argv):
    script = {}
    if "--script" in argv:
        script = json.load(open(argv[argv.index("--script") + 1]))

    if "--capabilities" in argv:
        caps = {"v": V, "type": "capabilities", "partials": True,
                "barge_in": True, "wake_word": False,
                "languages": ["en"], "sample_rate": 16000}
        caps.update(script.get("capabilities", {}))
        emit(caps)
        return 0

    inbox = Inbox()
    run(script, inbox)
    # A device does not exit when its script ends; it waits, like the real one.
    if not script.get("steps") or "exit" not in (script["steps"][-1] or {}):
        while True:
            time.sleep(3600)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
