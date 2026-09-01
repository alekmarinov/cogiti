# Architecture

How cogiti is put together, what happens in a turn, and what happens when any
part of it dies. `CLAUDE.md` is the design and the boundaries; this is the
mechanism.

## 1. One process, and what is not in it

cogiti is a single Python asyncio process. The event loop is a **router**: it
reads events, consults data, writes messages, and starts and stops children.
It does not compute, does not block, and holds no long work of its own.

Everything expensive is somewhere else, and each of those places was chosen
because it is expensive in a different way:

| out of process | why it is out |
|---|---|
| the presentation adapter | it owns a screen and must never stall on us |
| the speech adapter | CPU-heavy per frame, owns the sound card, tight timing for barge-in |
| the perception adapter | CPU-heavy per frame, owns the camera, carries biometrics |
| jobs (agent runners, tools) | unbounded duration, must be killable, may crash |
| services (feeds, born daemons) | unbounded lifetime, untrusted code, own uid |

The one exception is the resolver, which is linked and called inline, because a
resolve is microseconds and a process boundary would cost more than the work.

**The rule that follows:** if you are about to write something in cogiti that
loops, sleeps, parses a large file, or calls a model, it is in the wrong
process. Ask which of the five rows it belongs to.

## 2. Module map

```
main.py       config, open everything, install signal handlers, run forever
session.py    (speaker_id, thread) → a Turn machine and a context
turn.py       the state machine in §3; also multi-turn flows (confirm, setup)
resolve.py    the resolver port; partial and final transcripts in
table.py      config/commands.toml: intent → provider + presentation
providers/    local effects. One file each. No provider may take >250 ms.
escalate.py   prompt assembly → a job with an agent driver
jobs.py       the registry: spawn, track, stream, cancel, backpressure
services.py   manifests, supervision, birth, review, install, removal
memory.py     entities, provenance, retrieval, forgetting
present.py    a result object → presentation ops
speech.py     text → the speech port → audio and timing marks
adapters/     one client per port: reconnect, re-declare, capabilities
trust.py      secrets, consent, egress policy, the audit log
load.py       budgets, backpressure, shedding order
trace.py      one structured line per turn
db.py         SQLite, WAL, migrations
```

Two rules about this list. Nothing above `present.py` may use a presentation
adapter's protocol vocabulary — a region name appears in exactly one module.
Nothing except `trust.py` may read a secret.

## 3. The turn

```
                     ┌──────────────────────────────────────┐
                     ▼                                      │
                  ┌──────┐  speech/text                     │
                  │ idle │─────────────▶┌───────────┐       │
                  └──────┘              │ listening │       │
                     ▲                  └───────────┘       │
                     │                        │ partial     │
                     │                        ▼             │
                     │                  ┌───────────┐       │
                     │                  │ resolving │       │
                     │                  └───────────┘       │
                     │        handle ┌────────┼────────┐ escalate
                     │               ▼        ▼        ▼    │
                     │          ┌────────┐ ┌──────┐ ┌──────────┐
                     │          │ acting │ │confirm│ │ thinking │
                     │          └────────┘ └──────┘ └──────────┘
                     │               │        │ yes     │
                     │               │◀───────┘         │
                     │               ▼                  ▼
                     │          ┌──────────────────────────┐
                     └──────────│        speaking          │
                       done     └──────────────────────────┘
                                        ▲  barge-in ──▶ listening
```

What each state owes the user, and how long it has:

| state | on entry | budget |
|---|---|---|
| listening | nothing on screen changes | — |
| resolving | signal *thinking* if this looks like it will take a while | < 1 ms |
| acting | nothing; a command should finish before it needs a spinner | < 250 ms |
| confirm | speak the question, show the thing being confirmed | user's time |
| choosing | speak or show an enumerated list, and wait | user's time |
| thinking | `busy`, a thought `stream` with `attention:"never"` | seconds |
| speaking | `speak` with visemes and the wav | — |

**Partial transcripts.** `resolving` is entered on a partial, not only on a
final. A decision from the `pattern` tier is safe to act on early: the phrase
is one we listed and another word cannot change it into something else. A
decision from the `similar` tier is not, because the next word can move the
cosine. So: pattern-tier on a partial may pre-warm (open the socket, start the
provider's fetch) but may not produce an effect; only a final commits.
Never on a partial for a destructive intent, whatever the tier.

`choosing` is a fourth branch alongside `confirm` and is left out of the
diagram above to keep it readable; it returns the same way.

**Confirm never times out into yes.** A `confirm` that goes unanswered expires
into cancelled, silently. This is worth stating because the opposite is a
one-line change someone will eventually make to smooth over an awkward pause.

### Choosing from a list

Some questions cannot be answered contextually. *"Which of these can you talk
to?"* over a catalogue the user has never seen has no *that one* to point at,
because they do not yet know what is there. Enumeration is the way into a set
you do not know.

It is **not** the way to disambiguate a set you do know, and `jobs.md` §6 is
right about that: nobody says "cancel job 01J8ZQ", they say "stop that" or
"cancel the repository thing". Turning that into a numbered menu would be a
regression in naturalness dressed as rigour. So:

- **contextual reference** for a small known set — the most recent, the one just
  mentioned, the only one running;
- **an enumerated list** for browsing a set the user has not seen.

**Three ways to pick, and cogiti resolves all of them itself.** The item names
are cogiti's data and were never in the resolver's blob, so this is the same
arrangement as routing to a born service — a deterministic layer here, and the
resolver left immutable.

| said | means |
|---|---|
| "three", "the third one" | by position |
| "the tracker one" | by name, matched exactly or by pattern, never on a score |
| "down", "next", "up", "that one" | by cursor, if there is a screen |

Never on a similarity score. Two items that both partly match a name is a
question — *"the tracker or the ticket one?"* — not a guess, for the reason
`jobs.md` gives about cancelling the wrong thing.

**Navigation needs a screen and cogiti knows whether it has one.** "Down" moves
a cursor, and a cursor the user cannot see is not navigation. With no
presentation adapter configured, the list is spoken — the first few with their
numbers, then *"or say more"* — and up and down are not offered. Reading twelve
items aloud is not a menu.

**A list does not trap the user.** Numbers, ordinals, navigation words and the
item names are captured while choosing; everything else resolves as it normally
would, and if it resolves to a real intent the choice is abandoned and the
device says so. "Cancel" and "never mind" always leave. A `choosing` that goes
unanswered expires into cancelled, exactly as a `confirm` does and for the same
reason.

**Selecting is not consenting.** Picking item two from *"which service shall I
remove?"* identifies a target and authorises nothing. Removal is `consented`
and still asks, with the record from `security.md` §3. Keeping the two apart
matters: a list is a cheap, low-attention interaction, and consent must not
become something a person can give by saying "two".

**The list itself is a composition, not a new kind** — a group of text, each
child numbered, exactly like the consent view. No adapter needs to change to
show one.

**Barge-in** is a transition out of `speaking` on a `speech_start` event, and it
does three things in order: tell the presentation adapter to stop, stop the
audio, then start listening. Doing them in the other order gives you a face still
talking over the user for a second, which is the worst thing this device can
do.

**One turn at a time per session, always accepting.** A new utterance while a
turn is in `thinking` does not queue behind it — it interrupts, exactly as
the agent port's driver contract requires (drain the interrupted turn's remaining
messages before dispatching the new one, or the answers mix). A job started by
the old turn keeps running; the turn machine and the job registry are
deliberately not the same thing.

## 4. What talks to what

Every seam is newline-delimited JSON. One framing, one serialisation, one set
of limits.

| seam | transport | direction | who reconnects |
|---|---|---|---|
| cogiti → presentation | its adapter's socket | ops out, events in | cogiti, with backoff |
| service → presentation | same socket, own connection | ops out | the service, with backoff |
| speech ↔ cogiti | `/run/cogiti/speech.sock` | transcripts in, say requests out | cogiti |
| perception → cogiti | `/run/cogiti/perception.sock` | identity and vision events | cogiti |
| cogiti ↔ job | pipes to the child | prompt in, events out | neither; a job that dies is a job that failed |
| cogiti ↔ service | pipes for logs, signals for control | logs out | neither; cogiti restarts it |

Note what is missing: nothing connects *to* cogiti from outside the device,
and cogiti listens on no TCP port. The device's attack surface is the network
calls it makes, not the ones it accepts.

## 5. Restart, and who owns what

The appliance runs sysvinit with no systemd, so there is exactly one
supervisor per thing and it is worth being explicit about which.

```
init ──supervises──▶ adapters     (however the platform port supervises)
init ──supervises──▶ cogiti       (the same pattern, to be added)
cogiti ──supervises──▶ services   (restart with backoff, from manifests)
cogiti ──supervises──▶ jobs       (no restart; a dead job is a failed job)
```

**The presentation adapter restarts.** Every pinned object vanishes, because an
adapter keeps no state across connections. cogiti reconnects and re-declares its own stage
content; each service reconnects and re-declares its own. Nobody coordinates
this and nobody has to. Cost: a flicker.

**cogiti restarts.** It must:

1. read `/var/lib/cogiti/services/` and reconcile against what is running —
   kill by recorded pgid anything that is no longer in a manifest, adopt or
   restart the rest;
2. mark every job that was `running` as `failed` with reason `orphaned`, since
   its child died with the process group;
3. reconnect to every configured adapter;
4. say nothing about any of it unless asked. A device that narrates its own
   crash recovery is a device that draws attention to it.

**A service crashes.** Restart with exponential backoff to a ceiling. Three
failures inside a minute stops it and files a *needs attention* item; the
system says so next time it is spoken to, via the quiet queue. A crash
loop that silently continues forever is worse than a service that is off.

**A job crashes.** It failed. No restart, ever, automatically: a job may have
had a side effect before it died, and retrying a side effect is a decision the
user makes.

## 6. Latency budget

Measured from end-of-speech, which is the only clock the user has. These are
targets to hold the design to — put real numbers in the commit message once
there are any.

| from end of speech to | target | what has to be true |
|---|---|---|
| final transcript | 300 ms | local STT, endpointing tuned in the speech adapter |
| resolver decision | +1 ms | inline, no allocation |
| `busy` visible on the face | 50 ms from the *partial*, not the final | the face reacts before the sentence ends |
| a local command spoken | 800 ms total | provider under 250 ms, TTS streaming |
| an escalation acknowledged | 1 s | a spoken filler, not a spinner |
| an escalation answered | whatever it takes | `busy` and a thought stream carry it |

The one number that matters most is the third. Whatever the adapter does to
show *heard you, thinking* only reads that way if it starts while the user is
still speaking their last word.

## 7. Configuration

A flat `key = value` file for paths, devices and defaults; TOML under `config/` for the registries, because they are
structured. Precedence: built-in default, then `/etc/cogiti.conf`, then the
`COGITI_*` environment, then a flag.

A path the config *names* must exist. cogiti stops at startup and says which
line was wrong rather than falling back to something else — A deployment that silently ran with the wrong model is unexplainable later.

`cogiti --print-config` prints every setting and who decided it.

## 8. Testing without a device

The harness provides a fake presentation adapter that speaks the real protocol
and records ops, a fake speech adapter that replays transcripts with
realistic partial timing, a fake agent driver with scripted outputs, and a
controllable clock. A scripted session is a YAML file of inputs and expected
effects.

Everything in this document is testable that way except the process
supervision, which needs real processes — so the job and service tests spawn
real children that sleep, print and ignore `SIGTERM`, and assert on the
process table afterwards. That last case is the one that finds the bug that
matters.

### The fake agent driver

The agent port is the only required port with a wire, so its fake is the first
thing worth building — before any real adapter, because it is also the
**reference implementation** of the port. Anyone writing a real one reads it.

**It is a real process on real pipes, never an in-process object.** The port
requires *a separate process in its own process group*, and an object satisfies
none of what that is for: cancellation is never exercised, the group kill is
never exercised, and the framing is never exercised. Those are the three things
most likely to be wrong.

**It does not share cogiti's codec.** It writes its JSON by hand, from this
document rather than from cogiti's modules. Sharing the serialisation would
make both sides agree by construction, and a test that proves the code equals
itself proves nothing. This is inconvenient exactly once and is the reason the
fake can catch anything at all.

**It must be able to misbehave**, or it only ever proves the happy path:

| it can | which proves |
|---|---|
| ignore `SIGTERM` | the group kill actually kills |
| spawn a grandchild that outlives it | failure mode 1 in `jobs.md` §7 |
| emit `question` and wait forever | failure mode 4 — a pending question is a list with a deadline, not a callback |
| exit non-zero with no `result` | failure mode 3 — the stream is destroyed in a `finally` |
| return prose as a `result` | that *structured, not prose* is enforced rather than merely asked for |
| emit an unknown type, a wrong `v`, malformed JSON | cogiti rejects rather than crashes |
| ask for a tool it was not granted | the grant is real, and an ungranted tool has no channel |
| run past its budget | the budget is cogiti's to enforce, not the adapter's to honour |
| declare one capability short | startup fails loudly, naming it |
| emit an enormous stream | failure mode 6 — ring buffers, in memory and in SQLite |

That table is the acceptance criterion for the port implementation, which is
the other reason the fake comes first.

**Timing: scripted delays for now.** A second mode, where the harness releases
one event at a time over a control channel, is what interleaving tests need —
placing a barge-in exactly between the second and third event rather than
sleeping and hoping. Every hard case here is of that shape: barge-in during
`thinking` where the interrupted turn must be drained or the answers mix,
failure mode 5's *stop* meaning cancel or barge-in depending on turn state,
failure mode 7's two answers arriving at once. Deferred until the first such
bug, but **the fake is designed with room for that channel** — it cannot arrive
on stdin, which already carries `run` and brokered tool results, so it will
want a second descriptor. Leaving space for it now costs nothing; retrofitting
it means changing every scenario file.
