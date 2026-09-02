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
import time

from . import config as _config
from . import db as _db
from . import jobs
from .adapters import agent, presentation, resolver
from . import present
from . import presentation_templates as templates
from . import providers
from . import secrets
from . import table
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
            await self._until_spoken(marks)
        if self.echo or not (marks or self.p.a.connected):
            # Never silently succeed at nothing: if neither port took it, it
            # still has to go somewhere a person can see.
            print(text, flush=True)
        return text

    async def _until_spoken(self, marks):
        """Stay in `speaking` for as long as speaking takes.

        Without this the turn reached `idle` in the same millisecond the
        `speak` went out — and avatari's `idle` calls audio_stop() and
        viseme_stop(), so cogiti was cancelling its own utterance 120 ms
        before it was due to start. The mouth never moved and nothing was
        heard, on a path where every individual message was correct.

        Being a real wait is also what makes `speaking` a state rather than a
        label: an interruption arriving now cancels this, which is barge-in.
        """
        duration = marks.get("seconds")
        if not duration:
            v = marks.get("visemes") or [[0.0]]
            duration = v[-1][0]
        start = marks.get("audio_start_ns") or 0
        lead = max(0.0, (start - time.clock_gettime_ns(time.CLOCK_MONOTONIC)) / 1e9)
        # The mouth releases back to the expression layer about 150 ms after
        # the last viseme, so going idle exactly on the last mark would clip it.
        try:
            await asyncio.sleep(lead + duration + 0.15)
        except asyncio.CancelledError:
            # Barge-in, or a new question typed over the answer. Stop the mouth
            # and the audio now rather than letting them run under whatever
            # comes next; ports.md fixes this order.
            self.p.stop()
            raise

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

        # The fast path. Both optional: cogiti with neither escalates
        # everything, which ports.md says is a valid deployment and is how it
        # ran before this existed.
        self.templates = {}
        self.resolver = self._build_resolver(cfg)
        self.table = self._build_table(cfg)

        self.sessions = {}

    def _build_resolver(self, cfg):
        lib, blob = cfg["resolver_library"], cfg["resolver_blob"]
        if not lib and not blob:
            return None
        if not (lib and blob):
            raise _config.ConfigError(
                "resolver_library and resolver_blob go together; only one is set")
        return resolver.Resolver(
            lib, blob, config=cfg["resolver_config"] or None,
            device_location=cfg["device_location"] or None)

    def _build_table(self, cfg):
        if not cfg["command_table"]:
            return None
        providers.load_all()
        t = table.load(cfg["command_table"])
        self.templates = templates.load_dir(cfg["presentation_dir"])
        missing = sorted({c.present for c in t.commands.values()
                          if c.present and c.present != "none"
                          and c.present not in self.templates
                          and "{" not in c.present})
        if missing:
            # At load, not at first use. A card that is wrong is wrong the day
            # it is written, and finding out when someone finally asks about
            # the weather is finding out in the worst place.
            raise table.TableError(
                "commands name presentation templates that do not exist: %s"
                % ", ".join(missing))
        return t

    def resolve(self, text):
        """The fast path, or None when there is no resolver."""
        if self.resolver is None:
            return None
        return self.resolver.resolve(text)

    async def run_command(self, cmd, decision):
        """A resolved command, run as a local effect.

        In a thread, not on the loop: a provider is synchronous by contract and
        may block for up to its timeout. architecture.md §1 says the loop is a
        router that does not compute — and a provider that stalls the loop
        would stall the barge-in that is supposed to interrupt it.
        """
        fn = providers.get(cmd.provider)
        try:
            args, provenance = cmd.bind(decision)
        except table.TableError as e:
            return {"type": "failed", "kind": "table", "message": str(e)}

        call = dict(args)
        if cmd.command:
            call["_command"] = cmd.command
        if cmd.source:
            call["_source"] = cmd.source

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(fn, **call), cmd.timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            result = providers.Result.failed("timeout")
        except Exception as e:                                # noqa: BLE001
            return {"type": "failed", "kind": "provider", "message": str(e)}

        if not result.ok:
            return {"type": "result", "say": table.apology(result.reason),
                    "did": ["tried %s" % cmd.provider]}

        values = dict(args, **result.values)
        values.update({"_provenance": provenance})
        out = {"type": "result", "say": table.render(cmd.speak, values)}
        if cmd.present and cmd.present != "none":
            tpl = self.templates.get(cmd.present)
            # A named template renders to ops; anything else is a plain line,
            # which is what a device with a one-line display wants and is not
            # worth a file of its own.
            out["show"] = (tpl.ops(values) if tpl
                           else table.render(cmd.present, values))
        return out

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
        speech = None
        if speech_argv:
            speech = speech_mod.Speech(
                speech_argv, on_warn=warn,
                env=secrets.env_for(cfg["state_dir"],
                                    cfg.secret_grants("speech_secrets")))
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
    pending = None
    interactive = sys.stdin.isatty()

    async def settle():
        """Scripted input only: let the turn get somewhere before reading on.

        It ends on either of two things, and the second is the one that
        matters: the turn finished, *or* it started waiting for an answer.
        Waiting only for the first means nothing reads stdin while a confirm
        is pending, so the answer sitting in the pipe is never delivered and
        the question dies of its timeout — after which the "no" resolves as a
        fresh utterance and turns out to mean `mute`.

        A poll rather than an event because this is a development entry point
        and 10 ms of latency in a REPL buys nothing worth the plumbing.
        """
        while pending and not pending.done() and not s.awaiting_answer():
            await asyncio.sleep(0.01)

    def _report(task):
        """A dispatched turn's failure has nowhere to surface on its own."""
        if task.cancelled():
            return
        e = task.exception()
        if e is not None:
            print("(turn failed: %r)" % e, file=sys.stderr, flush=True)

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
        if s.awaiting_answer():
            await s.answer(line)
            await settle()
            continue

        pending = asyncio.ensure_future(s.utterance(line))
        pending.add_done_callback(_report)

        # A person and a script want opposite things from the same code.
        #
        # At a terminal, typing over an answer is the typed form of barge-in:
        # the turn is dispatched, reading continues, and the next line
        # interrupts. Awaiting here instead would mean nothing reads stdin
        # while a turn runs, so a confirm could never be answered and would
        # always die of its timeout.
        #
        # Piped in, every line arrives at once. Interrupting on each would
        # cancel every turn before it spoke — which it did: a six-line session
        # produced no output at all. A script means "these, in order".
        if not interactive:
            await settle()

    # Input ended. A turn still waiting on an answer will never get one, so it
    # is cancelled rather than waited for — and a cancelled confirm is a
    # cancelled action, which is the same answer silence gives.
    if pending and not pending.done():
        if s.awaiting_answer():
            pending.cancel()
        try:
            await asyncio.wait_for(pending, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="cogiti", add_help=False)
    ap.add_argument("--print-config", action="store_true")
    ap.add_argument("--help", "-h", action="store_true")
    # Where the config file is, which cannot itself come from the config file.
    # /etc/cogiti.conf is the appliance's; a checkout needs to name its own,
    # and the alternative is a pile of flags that drift from what ships.
    ap.add_argument("--conf", default="/etc/cogiti.conf")
    known, rest = ap.parse_known_args(argv)

    if known.help:
        print(__doc__.strip())
        print("\n  --conf PATH      the config file, default /etc/cogiti.conf")
        print("  --print-config   every setting, and who decided it")
        return 0

    cfg = _config.load(rest, conf_path=known.conf)
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
