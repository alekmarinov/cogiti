"""Text out, through the speech port.

`docs/ports.md`: `say(text) -> audio, and timing marks for whatever draws a
mouth`. This is the *out* half only. The port is one adapter in both directions
because barge-in needs them to share a clock, and until something exists that
listens, an out-only speech path is a partial adapter and is described as one.

**cogiti does not synthesise and does not send the audio anywhere.** It asks an
adapter for marks and hands them to whoever draws a mouth — which keeps three
things separable that are easy to weld together: the voice, the face, and the
decision to speak at all.

The adapter contract, deliberately the same shape as the agent port's:

    <speech_adapter> [args...] TEXT      one JSON object on stdout

    {"visemes": [[0.012, "TH"], [0.068, "EH"], ...],   seconds from t=0
     "audio":   "/tmp/utterance.wav",                  optional
     "seconds": 1.00}                                  optional

Which engine, which voice and which phoneme set are the adapter's business.
cogiti names none of them — the same rule that keeps a model name out of this
repository keeps a TTS engine out of it.
"""

import asyncio
import json
import time

TIMEOUT_S = 20


class Speech:
    """Synthesis, and the clock the mouth is scheduled against."""

    def __init__(self, argv, lead_ms=120, on_warn=None, env=None):
        self.argv = list(argv)
        # Explicit, never inherited — the same rule the agent adapter is held
        # to. A cloud voice is a credential and a network call, so "whatever
        # the shell that started cogiti happened to export" is not an
        # acceptable answer to what this process may reach.
        self.env = env
        # The audio is scheduled a little in the future so the trip over the
        # socket and the renderer's next frame both land before t=0. Without a
        # lead the first viseme is always already late.
        self.lead_ms = lead_ms
        self._warn = on_warn or (lambda m: None)

    async def marks(self, text):
        """Returns {visemes, audio, audio_start_ns} or None. Never raises:
        an appliance that cannot speak should still show its answer.

        asyncio, and not `subprocess.run`, for a reason that cost an afternoon:
        the event loop installs a child watcher that reaps children, so a
        blocking `Popen.wait()` never sees the exit status and hangs until its
        timeout — a synthesis that takes 60 ms became a 20 second stall, and
        only under the loop, which is why it looked like the adapter was slow.

        It is also simply the right shape. `architecture.md` §1: the loop is a
        router that does not compute and does not block. Speaking takes real
        time, and barge-in needs the loop answering while it happens.
        """
        if not self.argv or not text:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv, text, env=self.env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            self._warn("speech adapter would not start: %s" % e)
            return None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self._warn("speech adapter timed out after %ss" % TIMEOUT_S)
            return None

        # stdout only. An engine that chatters on stderr is normal and is not
        # a failure; a non-zero exit with usable json is not one either.
        text_out = out.decode("utf-8", "replace").strip()
        if not text_out:
            self._warn("speech adapter wrote no marks (exit %s): %s"
                       % (proc.returncode, err.decode("utf-8", "replace")[:200]))
            return None
        try:
            msg = json.loads(text_out.splitlines()[-1])
        except ValueError:
            self._warn("speech adapter did not return json: %r" % text_out[:200])
            return None

        visemes = msg.get("visemes") or []
        if not visemes:
            return None

        # CLOCK_MONOTONIC, because that is the clock the renderer schedules
        # against. Anything else and the mouth drifts from the voice by
        # whatever the two clocks disagree about.
        start = (time.clock_gettime_ns(time.CLOCK_MONOTONIC)
                 + self.lead_ms * 1_000_000)
        return {"visemes": visemes, "audio": msg.get("audio"),
                "seconds": msg.get("seconds"), "audio_start_ns": start}
