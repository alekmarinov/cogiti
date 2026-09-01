# cogiti

An orchestrator for a spoken, agentic system.

It takes what someone asked for, decides whether it is a reflex, a question or
a standing duty, does it or supervises whoever does, presents the result, and
remembers what it learned.

```
audio ──▶ speech ──┐
                   ├──▶ cogiti ──▶ resolver ──▶ decision
text channel ──────┘       │
                           ├── handle   ──▶ command ──▶ presentation
                           ├── confirm  ──▶ ask, wait, then command
                           └── escalate ──▶ agent ──▶ answer + card + speech
                                              │
                                              └──▶ long job / born service
                                                   ──▶ supervisor
```

**cogiti is not tied to any device.** Everything it needs from the outside —
a resolver, a screen, a voice, a camera, a model, a machine to run on — arrives
through one of six ports. Two are required; the rest are capabilities it
discovers, announces, and does without when absent.

**Nothing is implemented yet.** This repository holds the design, and
`CLAUDE.md` is the part that is not negotiable.

## The documents

| | |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | the design, the hard boundaries, the settled decisions. **Read this first** |
| [`docs/ports.md`](docs/ports.md) | the six ports. What keeps cogiti general, and where a deployment plugs in |
| [`docs/architecture.md`](docs/architecture.md) | process model, the turn state machine, restart semantics, latency budget |
| [`docs/command-table.md`](docs/command-table.md) | intent → effect → presentation, as data |
| [`docs/jobs.md`](docs/jobs.md) | job lifecycle, the registry, and cancellation that actually kills children |
| [`docs/services.md`](docs/services.md) | how a service the user asked for gets written, reviewed and installed while they wait |
| [`docs/agent-protocol.md`](docs/agent-protocol.md) | the wire between cogiti and an agent adapter — the only port whose adapter is the job |
| [`docs/skills.md`](docs/skills.md) | how the agent is taught to use an outside service, and why a skill has no authority |
| [`docs/memory.md`](docs/memory.md) | entities, provenance, contradiction, and forgetting that really forgets |
| [`docs/security.md`](docs/security.md) | secrets, consent, prompt injection, the sandbox, egress, the audit log |

## Using it

A deployment supplies adapters for the ports it wants and configures them. It
does not fork cogiti, and cogiti contains nothing about it.

A deployment lives in its own repository and is not named here. Its roadmap,
its component map and its release process are its own; cogiti has no list of
who uses it, because a list like that is the first thing to go stale and the
second thing to start dictating.

## The idea in one line

Three units of work — a **command** that finishes inside a turn, a **job** that
outlives it, and a **service** that outlives the conversation. A service is the
only one that can be born at runtime because the user asked for it, and that is
what everything else here exists to make safe.
