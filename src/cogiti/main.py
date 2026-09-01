"""config, open everything, install signal handlers, run forever.

`docs/architecture.md` §2. This slice runs the text-driven orchestrator that
`ports.md` describes: "a cogiti with an agent and a platform is a usable
text-driven orchestrator". You type; it escalates; it answers.
"""

import argparse
import asyncio
import os
import signal
import sys

from . import config as _config
from . import db as _db
from . import jobs
from .adapters import agent, presentation
from . import present
from . import speech as speech_mod
from .session import Session
from .trace import Trace


class TextOutput:
    """The output of last resort, and a legitimate one.

    ports.md gives presentation and speech as optional ports, which leaves a
    deployment with neither unable to answer at all. Rather than a silent
    fallback, printing is a configured choice — `output = text` — so that
    'no way to reach the user' stays a startup failure rather than a surprise.
    """

    async def say(self, result):
        if result is None:
            return ""
        if result.get("type") == "failed":
            text = "I couldn't do that: %s" % result.get("message", result.get("kind"))
        else:
            text = result.get("say", "")
        print(text, flush=True)
        show = result.get("show")
        if show:
            print("  [would show: %s]" % (show if isinstance(show, str)
                                          else show.get("kind", "an object")),
                  flush=True)
        return text


class FaceOutput:
    """A result reaches the person through the presentation and speech ports.

    The two are used together but stay separable, which is the point of them
    being two ports: `say` is what is heard, `show` is what is drawn, and an
    adapter may be missing without the other stopping. With no speech adapter
    the face shows the answer silently; with no presentation adapter it is
    spoken and nothing is drawn.
    """

    def __init__(self, presenter, speech=None, echo=False):
        self.p = presenter
        self.speech = speech
        self.echo = echo            # also print, for a workstation with no ears

    async def say(self, result):
        if result is None:
            return ""
        if result.get("type") == "failed":
            text = "I couldn't do that: %s" % (result.get("message")
                                               or result.get("kind"))
        else:
            text = result.get("say", "")

        self.p.result(result)
        marks = await self.speech.marks(text) if self.speech else None
        if marks:
            self.p.speak(marks)
        if self.echo or not (marks or self.p.a.connected):
            # Never silently succeed at nothing: if neither port took it, it
            # still has to go somewhere a person can see.
            print(text, flush=True)
        return text

    def on_state(self, state):
        """The face is a status display for the turn, not a printer that runs
        once at the end — architecture.md §3."""
        if state == "resolving":
            self.p.busy(True)
            self.p.expression("listening")
        elif state == "thinking":
            self.p.expression("thinking")
        elif state == "idle":
            self.p.busy(False)
            self.p.idle()

    def on_thought(self, text):
        self.p.thought(text)


class Cogiti:
    def __init__(self, cfg):
        self.config = cfg
        self.output_kind = _config.require_output(cfg)
        self.output = self._build_output(cfg)

        state = cfg["state_dir"]
        os.makedirs(state, exist_ok=True)
        self.db = _db.open_db(os.path.join(state, "jobs.db"))
        self.trace = Trace(cfg["trace_file"])

        self.agent_argv = cfg["agent_adapter"].split()
        if not self.agent_argv:
            raise _config.ConfigError(
                "agent_adapter is not set. It is a required port: cogiti has no "
                "default, because naming one would mean having an opinion about "
                "which model runs.")

        self.sessions = {}

    def _build_output(self, cfg):
        """Which ports actually answer. `output = text` stays a legitimate
        choice rather than a fallback, so 'no way to reach the user' remains a
        startup failure and never a surprise."""
        socket_path = cfg["presentation_adapter"]
        speech_argv = cfg["speech_adapter"].split()
        if not socket_path and not speech_argv:
            return TextOutput()

        warn = lambda m: print("(%s)" % m, file=sys.stderr, flush=True)
        adapter = presentation.Presentation(socket_path, on_warn=warn)
        speech = speech_mod.Speech(speech_argv, on_warn=warn) if speech_argv else None
        return FaceOutput(present.Presenter(adapter), speech,
                          echo=(cfg["output"] == "text"))

    async def start(self):
        # Orphan recovery before anything else runs, so the table never claims
        # a process that is not there.
        orphaned = jobs.recover(self.db)
        if orphaned:
            print("(%d job(s) from a previous run marked orphaned)" % len(orphaned),
                  file=sys.stderr)

        # Capabilities are probed once, here, and a missing one stops startup
        # rather than surfacing at the first escalation.
        caps = await agent.capabilities(self.agent_argv)
        agent.require(caps, ["tools"])
        return caps

    def session(self, speaker="unknown", thread="main"):
        key = (speaker, thread)
        if key not in self.sessions:
            self.sessions[key] = Session(self, speaker, thread)
        return self.sessions[key]


async def repl(c):
    loop = asyncio.get_event_loop()
    s = c.session()
    print("cogiti — type to ask, ctrl-d to leave", flush=True)
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        # A pending question takes the next thing typed. Nothing else can be
        # meant by it, and treating it as a new utterance would abandon the
        # turn that is waiting.
        if await s.answer(line):
            continue
        await s.utterance(line)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="cogiti", add_help=False)
    ap.add_argument("--print-config", action="store_true")
    ap.add_argument("--help", "-h", action="store_true")
    known, rest = ap.parse_known_args(argv)

    if known.help:
        print(__doc__.strip())
        print("\n  --print-config   every setting, and who decided it")
        return 0

    cfg = _config.load(rest)
    if known.print_config:
        cfg.print_config()
        return 0

    c = Cogiti(cfg)
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, loop.stop)
    try:
        loop.run_until_complete(c.start())
        loop.run_until_complete(repl(c))
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
