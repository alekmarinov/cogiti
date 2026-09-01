# cogiti

An orchestrator for a spoken, agentic system. It takes what someone asked for,
decides who does the work, supervises whoever does it, presents the result, and
remembers what it learned.

It is **not tied to any particular device.** Everything it needs from the
outside — a resolver, a screen, a voice, a camera, a model, a machine to run
on — arrives through one of the six ports in `docs/ports.md`. cogiti names no
implementation of any of them.

This document is the source of truth for architecture and scope. Read it before
proposing changes. If a decision here looks wrong, say so explicitly rather
than quietly working around it.

---

## 1. What cogiti is, in one paragraph

cogiti turns a request into an effect and an effect into a presentation. It
owns the conversation, the decision of who does the work, the supervision of
anything that takes longer than a sentence, everything the system remembers,
and every credential it holds. It is the only thing allowed to say yes to an
agent.

## 2. The three units of work

Almost every design question here is really the question *which of these three
is this?* Getting it wrong is what produces a model in a polling loop, or a
shell command with no cancellation.

| | **command** | **job** | **service** |
|---|---|---|---|
| what it is | an instant, deterministic effect | a bounded unit of work, possibly long | a persistent process with a duty |
| decided by | the resolver's `handle`/`confirm` plus the command table | escalation, or a command that declares itself long | the user asking for something standing |
| lifetime | one turn | minutes to hours; survives the turn | until removed; survives a restart |
| runs where | in cogiti's loop, via a provider | its own process group | its own process group, own identity |
| may be shown | cogiti draws for it, in the conversational region | never; cogiti draws its status | itself, in its own pinned region |
| born at runtime | no — it is data, shipped or generated | yes | **yes, and this is the point** |
| example | "what time is it" | "summarise this repository" | "keep that price on screen" |

The last row is the interesting boundary. A service is the only one of the
three that outlives the conversation that created it, which is why it is the
only one needing a review gate, a manifest, a sandbox and an uninstall.
`docs/services.md` is about that boundary and very little else.

## 3. Hard boundaries

Invariants. Violating one is a design bug, not a style issue. Each exists
because it is otherwise eroded by a plausible-looking convenience.

- **cogiti never blocks.** Anything that can take longer than a frame is a
  child process or an awaited I/O. A synchronous call into the network, into a
  model, or into a subprocess in the event loop is the bug that makes a system
  feel dead, and it is never worth the simplicity it buys.

- **cogiti never renders, and never computes a coordinate.** It sends the
  presentation adapter intent — objects, ids, regions, relationships. Where
  things go belongs to the adapter, which is the only party that knows the
  screen.

- **The conversational region is cogiti's; the pinned region is the
  services'.** An agent never gets the presentation adapter at all. A service
  gets its own connection and its own namespace. This is what stops two
  processes fighting over one screen.

- **Nothing an agent writes runs without a review gate.** Not a service, not a
  script, not a scheduled task. The user approves it once, explicitly, and the
  approval is recorded with the source hash it approved.

- **The system works with the network down.** Degraded, and it says so. The
  resolver's intents, local providers, the clock, and what it already remembers
  all keep working. Escalation does not, and the honest failure is "I can't
  reach anything right now", not a silent hang.

- **Destructive actions never resolve on a score.** A `confirm` is never
  auto-answered by cogiti, by a timeout, or by an agent.

- **cogiti is a supervisor, not a store of its own invention.** What a service
  is lives in its manifest on disk. What the system knows lives in the
  database. cogiti holds neither across a restart and re-derives its world from
  both on start. State that exists only in a running process is state nobody
  can inspect, back up or repair.

- **Memory is never silently written from inference.** Every fact carries how
  it was learned. Stated, inferred and observed are three different things, and
  conflating them is how a system confidently repeats a wrong guess for a year.

- **The resolver is never asked to do more than decide**, and never asked
  anything that needs the world. If a feature seems to need the resolver to
  know something, it needs cogiti to know it.

- **An agent gets the smallest set of tools that can answer the question**, and
  no credential it was not granted for that job. Scope is per job, never
  global.

- **No port is assumed present.** Two are required — agent and platform. Every
  other capability is discovered, announced, and done without when absent.

## 4. Decisions

**Proposed, and treated as settled unless changed before implementation
begins.** Each records what it rules out, because that is the part that gets
forgotten.

| decision | choice | why, and what it costs |
|---|---|---|
| Language | **Python 3.11+, asyncio, no framework** | cogiti is I/O, subprocess supervision, JSON, TLS and tool protocols — a fight in C and a library call in Python. Costs startup time and memory, neither on any critical path. Rejected: Go (a second toolchain, and cgo is not simpler than ctypes), Rust (build time and a larger async commitment than this needs), C (wrong tool for this half of the system) |
| Process model | **One asyncio process that is also a supervisor** | The loop is a router: it never computes, never blocks, holds no work of its own. Everything with a duration is a child in its own process group, so cancellation is a signal to a group and not a hope. Rejected: threads (cancellation does not work), a separate supervisor (a second thing to keep alive, for no gain) |
| Resolver binding | **In-process, via the platform's FFI** | A resolve is microseconds; a process boundary would cost more than the work. The only port where that trade comes out this way |
| Agent runtime | **A subprocess per job, behind a driver interface** | Keeps a model SDK's dependency tree out of cogiti, makes cancellation real, contains a crash. Rejected: calling an SDK in-process (a hung agent hangs everything) |
| Inference location | **Local for perception, remote for reasoning, routed by data** | Perception runs per frame and carries biometrics, so it stays local. `config/models.toml` maps task type to model, so a local reasoning model is a config change and not a port |
| Persistence | **SQLite (WAL), plus files** | Jobs, memory, transcripts, consent grants and timers in the database. Service manifests and their source as plain files, because they are meant to be read, diffed and deleted by a person. Secrets in a separate store, never in the database |
| Session model | **Per-speaker sessions from day one** | A conversation is keyed `(speaker_id, thread)`. With no perception adapter every speaker is `owner`. The cost now is one column; the cost later is every row in memory |
| Time | **Monotonic for scheduling, wall clock only for display** | Durable timers store both, so a restart or a clock step does not lose or double-fire a reminder |

## 5. Architecture

```
   speech adapter        perception adapter
   transcripts, barge-in    identity, presence
            │                       │
            ▼                       ▼
  ┌──────────────────────────────────────────────────┐
  │ cogiti — one asyncio process                     │
  │                                                  │
  │  turn.py      the conversation state machine     │
  │  resolve.py   the resolver port; decisions in    │
  │  table.py     intent → provider → presentation   │
  │  providers/   small local effects                │
  │  escalate.py  prompt assembly, then a job        │
  │  jobs.py      spawn, track, stream, cancel       │
  │  services.py  manifests, supervision, birth      │
  │  memory.py    entities, provenance, forgetting   │
  │  present.py   a result → presentation ops        │
  │  speech.py    text → the speech port             │
  │  trust.py     secrets, consent, egress, audit    │
  │  load.py      budgets, backpressure, shedding    │
  └──────────────────────────────────────────────────┘
        │ process groups              │ process groups
        ▼                             ▼
   jobs (agents, tools)          services (feeds, daemons)
   no presentation access        own connection, own identity
```

Every arrow in and out is newline-delimited JSON over a socket or a pipe. One
framing and one serialisation, because a second is a second parser, a second
set of limits and a second way to hang.

Two rules about the module list: nothing above `present.py` may use a
presentation adapter's vocabulary, and nothing except `trust.py` may read a
secret.

`docs/architecture.md` has the turn state machine, restart semantics and the
latency budget.

## 6. Everything that can be data, is data

When something needs new code, ask first whether it could be a row.

| registry | decides | written by |
|---|---|---|
| the resolver's intents | what an utterance is | a person, at build time |
| `config/commands.toml` | what a resolved intent does, and how it is shown | a person |
| `config/presentation/*.toml` | the shape of a result kind | a person |
| `config/adapters.toml` | which implementation serves which port | a person, per deployment |
| `config/models.toml` | which model serves which task | a person |
| `config/policy.toml` | what needs consent, what is never delegated | a person |
| `config/load.toml` | concurrency budgets and shedding order | a person |
| `services/*/service.toml` | what a service is and may do | **an agent, then a person approving it** |

The last row is the ambition of the project in one line. Everything above it is
the machinery that makes it safe.

## 7. Layout

```
CLAUDE.md                  this file — the design
docs/
  ports.md                 the six ports. The document that keeps cogiti general
  architecture.md          process model, turn machine, restart, latency
  command-table.md         intent → effect → presentation, as data
  jobs.md                  job lifecycle, registry, cancellation
  services.md              the service contract, and how one is born
  memory.md                entities, provenance, forgetting
  security.md              trust boundaries, secrets, consent, egress
config/                    the registries above
src/cogiti/                the modules in §5
providers/                 local effects, one file each
adapters/                  port clients — one per protocol, not per product
eval/                      scripted sessions, expected effects
tools/                     the simulator, the CLI, service templates
```

## 8. Conventions

- Python 3.11+, standard library first. A dependency is a cost on every
  deployment — say what it costs when you propose one.
- Type hints everywhere, `mypy --strict` on `src/`. The value is that the
  contracts between modules stay written down.
- No global mutable state. One object, passed explicitly.
- All timing in nanoseconds from `time.monotonic_ns()`.
- Every subprocess starts in a new process group and is killed as a group. A
  kill that leaves a grandchild running is a bug with a name.
- Fail loudly at startup, degrade honestly at runtime. A missing file named in
  the config stops the process; a dead API says so and keeps the last value.
- One structured trace line per turn. It is the only way to answer "why did it
  do that", and the source of future resolver exemplars. Do not let it rot.
- Every feature ships with an eval that runs before and after a change, and
  both numbers go in the commit message.

## 9. Non-goals

- **Being tied to one device.** A deployment's specifics — its renderer, its
  hardware, its image, its persona — belong to that deployment, not here.
- A general plugin API for third parties. Services are authored by an agent for
  one user on one system, reviewed by that user. Different problem, different
  threat model.
- A web UI, a settings app, or any screen of its own.
- Multi-device sync, accounts, or a cloud backend.
- Running the reasoning model in-process. It is a config change if it ever
  becomes true; designing for it now costs latency everywhere.
- Being a general agent host. cogiti runs agents for its own user in its own
  context. It is not somewhere to run a CI job.
- Sandboxing by container runtime, assumed. The platform port says what
  confinement is available; cogiti requires a uid and limits, not a runtime.

## 10. Open questions

- ~~How does a service born at runtime get an utterance routed to it?~~
  **Settled.** cogiti keeps its own deterministic pattern layer and does not
  ask the resolver for a runtime overlay: a resolver's overlay is scored
  exemplars for intents that already exist, and a born service is a new intent
  reached by pattern. Keeping the resolver immutable is worth more than the
  latency on phrasings a manifest already lists. `docs/services.md` §5 carries
  both positions and what it costs.
- **What protects a secret at rest** on a deployment with no secure element?
  File permissions and physical control, and nothing else. Say that plainly
  rather than implying more.
- **One conversation or several at once?** The session model allows several;
  whether that is a real case is unknown until a perception adapter exists.

## 11. How to help

- Give **one terminal command at a time**.
- Say which unit of work (§2) a proposal is about. Most confusion available
  here is a job discussed as if it were a service.
- Before writing code, ask whether it could be a row in one of §6's registries.
- **If a proposal names a specific product, device or renderer, it is in the
  wrong repository** — unless it is adding a port in `docs/ports.md`.
- Do not add a message framing, a serialisation format, or a seventh port
  without discussing it first.
