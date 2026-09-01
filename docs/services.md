# Services, and how one is born

A service is a process with a standing duty. It pins the weather, watches a
build, holds a price on screen, or wakes up every morning and does something.
It outlives the conversation that created it and it survives a reboot.

Most of them will not be written by a person. The user says *keep the ETH
price on screen* and there is no such service, so one is written, reviewed and
installed while they wait. That is the most ambitious thing cogiti
does, and everything in this document exists to make it something other than
reckless.

The presentation port's pinning contract (`ports.md`) is what makes this
possible; read it first.

## 1. What a service is

| | |
|---|---|
| lifetime | until removed; started at boot, restarted on failure |
| identity | a name, a namespace, a uid |
| screen | its own presentation connection, its own namespace, **pinned region only** |
| storage | one directory, `/var/lib/cogiti/services/<name>/` |
| network | only the hosts it declared, enforced by the egress broker |
| secrets | only the ones it declared and the user granted |
| supervision | cogiti: backoff on crash, stop after a crash loop |
| removal | one intent, and it leaves nothing behind |

A service never touches the conversational region. That is the whole of the
screen-sharing policy, and it works because the presentation port requires
per-connection namespace ownership: the conversation and the pinned world
cannot collide because they are in different regions declared by different
connections.

## 2. The directory and the manifest

```
/var/lib/cogiti/services/eth-price/
  service.toml        the manifest — the truth about this service
  main.py             the code
  state.json          whatever it wants to keep (its own business)
  approved            the signed record of what the user said yes to
```

```toml
# service.toml
name        = "eth-price"
title       = "ETH price"                 # what the device calls it out loud
namespace   = "eth-price"                 # presentation namespace; must equal name
region      = "periphery"                 # the only value permitted today
exec        = ["python3", "main.py"]
interval_s  = 60                          # advisory: how often it expects to work
created_by  = "job:01J8ZQ...";            # the authoring job
created_at  = "2026-08-30T19:41:07Z"
source_sha  = "sha256:9f2c..."            # over main.py, at approval

[limits]
cpu_seconds  = 30                          # per minute, enforced by the supervisor
memory_mb    = 128
open_files   = 64
processes    = 4

[network]
allow = ["api.coingecko.com"]              # the egress broker permits nothing else

[secrets]
require = []                               # names in the secret store, granted per service

[phrases]                                  # how the user reaches it by voice
patterns = ["eth price", "what is eth at", "price of ethereum"]
                                           # read aloud at the review gate: these
                                           # are sentences the device stops
                                           # sending to the model
```

**The manifest is the truth.** cogiti holds nothing about a service that is not
derivable from this directory. That is the same discipline the presentation
port requires of an adapter — a display, never a database — one layer up, and
it buys the same things: a service can be inspected, diffed, backed up and deleted with `rm`,
and a cogiti that has just restarted knows exactly what should be running
without having remembered anything.

## 3. The SDK, and why it is the real safety mechanism

An agent writing a service from nothing writes three hundred lines: a socket,
a reconnect loop, a backoff, a JSON encoder, a poller, error handling. Nobody
reviews three hundred lines, so the review gate becomes theatre.

With an SDK that already owns the connection discipline, the same service is
this:

```python
from cogiti.service import Service, every

svc = Service("eth-price")

@every(60)
async def tick():
    price = await svc.get_json("https://api.coingecko.com/api/v3/simple/price",
                               params={"ids": "ethereum", "vs_currencies": "usd"})
    svc.show(kind="text", style="headline",
             text=f"Ξ  ${price['ethereum']['usd']:,.0f}")

svc.run()
```

Twelve lines, and every one of them is readable by someone who does not
program. That is the point. **The SDK is not a convenience, it is what makes
the review gate real**, and every capability it does not expose is a capability
a generated service cannot casually acquire.

What the SDK owns, so the service never writes it: connecting to the
presentation adapter and reconnecting with backoff, re-declaring on every
connect (the mistake every feed makes first: it stays alive after the adapter
restarts, notices nothing, and pins nothing until the value next happens to
change),
the namespace and the region, last-write-wins updates, HTTP through the egress
broker, secrets by name, structured logging, and a clean exit on `SIGTERM`.

## 4. The birth of a service

```
  "keep the ETH price on screen"
            │
            ▼
   resolver ──▶ escalate, or a `pin_thing` intent
            │
            ▼
   ┌─────────────────────────────────────────────────────┐
   │ 1. recognise a standing want                        │
   │    Is this a question or a duty? A question is a    │
   │    job. A duty is a service. If unsure, ask — this  │
   │    is one of the few places a clarifying question   │
   │    is cheaper than being wrong.                     │
   ├─────────────────────────────────────────────────────┤
   │ 2. authoring job (an agent, in a staging dir)       │
   │    writes main.py against the SDK and a template,   │
   │    declares hosts and secrets, writes the manifest  │
   ├─────────────────────────────────────────────────────┤
   │ 3. dry run, in the sandbox, against a fake renderer │
   │    Must produce at least one valid update within    │
   │    its own interval, twice, and exit cleanly on     │
   │    SIGTERM. A service that cannot do that never     │
   │    reaches the user.                                │
   ├─────────────────────────────────────────────────────┤
   │ 4. static checks                                    │
   │    no subprocess, no eval/exec, no imports outside  │
   │    an allowlist, no filesystem writes outside its   │
   │    own directory, no socket except through the SDK, │
   │    every host in the manifest, size under a limit   │
   ├─────────────────────────────────────────────────────┤
   │ 5. the review gate — a person, out loud             │
   │    "I've written a service that reads the ETH price │
   │     from coingecko.com once a minute and pins it in │
   │     the corner. It needs no passwords, and it will  │
   │     answer when you say 'eth price' or 'what is eth │
   │     at'. Shall I keep it?"                          │
   │    ...and the code on screen, readable.             │
   ├─────────────────────────────────────────────────────┤
   │ 6. install: move into place, record the approval    │
   │    with the source hash, start it, and only now     │
   │    does it exist                                    │
   └─────────────────────────────────────────────────────┘
```

Steps 3 and 4 exist so that step 5 is a decision about *purpose* rather than a
decision about *code*. The user is being asked whether they want a thing that
reads coingecko every minute. They are not being asked to audit Python, and a
gate that requires them to is a gate that will be answered yes every time.

**The phrases are part of the decision, not a detail of it.** Since cogiti's
own pattern layer is how a born service is reached (§5), a manifest's
`[phrases].patterns` decides which sentences stop going to the model. An agent
writes them, for a service it wrote. A price ticker that quietly claims "what's
the time" would take that utterance whenever no built-in matches it, and the
user approved a price ticker.

The existing guards only half cover this: built-ins win a tie, and matching is
exact so nothing is taken on a fuzzy score. Neither helps when a service claims
a sentence no built-in has. So the gate says them out loud. They are short,
there are few of them, and reading three phrases costs a second — unlike the
code, which is why step 5 is about purpose and not about Python.

**The approval binds to a hash of the manifest as well as the code.** A service
whose `main.py` no longer matches `source_sha` does not start; it is quarantined
and reported. The manifest is covered for the same reason: a service that
widens its phrase list has changed what the device hears, which is a new
approval even though no code moved. That covers the case that actually worries
me: an agent editing a service it wrote last week, which is a new approval and
not a continuation of the old one.

**Modification is birth again.** "Make it check every ten seconds instead" runs
the whole pipeline and asks again — briefly, because the diff is one line, but
it asks.

## 5. Reaching a born service by voice

The resolver's knowledge was compiled before this service existed, so nothing
in it can resolve "what is eth at". Three ways out, in the order they should be tried:

1. **cogiti's own pattern layer** — the stopgap, and always available. Before escalating,
   cogiti matches the normalised utterance against `[phrases].patterns` from
   every installed manifest. Exact and pattern matching only — the same tier
   discipline the resolver's pre-matcher has, and for the same reason: a born
   service must not be able to steal traffic from a built-in intent on a
   fuzzy score. Built-ins always win a tie.

2. **Recompiling the blob on-device.** Correct, general, and requires the
   embedding model and the Python toolchain in the image. Rejected for now on
   size; revisit if generalisation past listed phrasings turns out to matter.

### Why cogiti keeps this and does not ask the resolver for it

An earlier version of this section proposed a third way and called it the right
answer: the resolver gains a small runtime-loadable set of deterministic
patterns, written by cogiti on install and picked up by the resolver. It was
dropped, and the reason is worth keeping.

**A resolver's overlay is a different feature that shares a word.** The
resolver design this was an ask against describes an overlay as *learned
exemplars*: a request that keeps escalating and keeps resolving to the same
**existing** intent should stop escalating. Its safety rules are explicit —

> An overlay may only add exemplars to intents that already exist in the base.
> It may not define an intent, a slot, a pattern, or a threshold. Everything
> that decides *what the brain does* stays in the shipped artifact.

A service born at three in the afternoon is a **new intent**, reached by
**pattern**. Both are things that rule forbids, and a third collision follows
from the mechanism: exemplars are matched by cosine, which is the `similar`
tier — the very tier this section rules out for born services, for the reason
given above.

**The rule is right and cogiti should not ask for an exception to it.** A
resolver that stays immutable is one whose accuracy can be evaluated before it
ships and whose behaviour cannot be changed by anything the device installs
later. Asking it to accept intent definitions from a service an agent wrote
that afternoon trades that away for a latency win on phrasings the manifest
already lists.

So the pattern layer stays here, in the component that already owns the command
table, consent and the policy about what the device may do. That is where a new
thing the device can be asked to do belongs.

**What this costs, stated plainly.** cogiti's layer matches listed phrasings
only. "what is eth at" reaches the service; "how is ethereum looking" does not,
and escalates to the model like any other unrecognised sentence. The fast path
covers what the manifest names and nothing more, and the model remains the
general answer. Generalising past listed phrasings is what option 2 above is
for, and it is still rejected on size.

Whatever the mechanism, the false-accept rule stands: a born service must not
answer a sentence that was meant for something else. The `open_domain` reject
class a resolver carries exists precisely to protect this, and adding a service is a
reason to add negative eval cases, not just positive ones.

## 6. Supervision

- Start at boot, in manifest order, after the adapters and after the network.
- Restart on exit with exponential backoff: 1 s, 2 s, 4 s, to a 60 s ceiling.
- **Three failures inside a minute stops it.** It is marked `needs-attention`,
  the panel is gone, and the device mentions it next time it is spoken to
  (the quiet queue). A crash loop that continues forever is worse than
  a service that is off, because nobody finds out about it.
- A service exceeding its CPU or memory limit is killed and treated as a
  crash. Repeatedly exceeding it stops it for good and says why.
- `SIGTERM`, then `SIGKILL` after 5 s, to the process group. A service that
  leaves a grandchild running is a bug in the SDK, not in the service.

## 7. Removal

"Unpin the ETH price" or "get rid of the ETH price":

1. Stop it. The panel vanishes on its own, because objects belong to the
   connection that made them — which is the intended way to unpin, and needs no
   extra protocol support at all.
2. Move its directory to `/var/lib/cogiti/removed/<name>-<timestamp>/`, kept
   for thirty days. Removal is a voice command and voice commands are
   misheard; thirty days of undo costs a few kilobytes.
3. Remove its patterns, its egress entries and its secret grants. **This is
   the step to get right.** A revoked secret grant that survives removal is a
   credential belonging to nothing, and a pattern that survives is an utterance
   that resolves to a service that is gone.
4. Say what was removed, by title, so a misheard removal is caught
   immediately.

`list_services`, `service_status` and `pause_service` are intents in the
resolver's registry, exactly as the job intents are.

## 8. Limits, on purpose

| limit | value | why |
|---|---|---|
| services installed | 32 | past this, the periphery is a mess and so is the boot |
| pinned objects per service | 4 | a service is a duty, not an application |
| source size | 32 KB | anything larger is not reviewable in the gate |
| authoring attempts per request | 3 | then it gives up and says so |
| dry-run duration | 90 s | a service that needs longer to prove itself is one nobody wants |

These are not tuning parameters, they are the shape of the product. A device
that will grow forty services is a device nobody understands the behaviour of.

## 9. What a service may never be

- In the conversational region. It is pinned, always.
- A way to run arbitrary code that has nothing to do with a standing duty. If
  the answer is "do this once", that is a job.
- A holder of a credential the user did not grant to it by name.
- Self-modifying. A service that rewrites its own `main.py` fails its hash
  check on next start, which is the intended outcome and not a bug to fix.
- Able to start another service, or a job, or an agent. The only thing that
  spawns is cogiti.
- Silent about failing. A service that has been broken for a day and said
  nothing is the failure mode this whole document is trying to avoid.
