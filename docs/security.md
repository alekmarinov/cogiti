# Trust, secrets and consent

The device holds credentials, runs code an agent wrote, and can act on the
world on someone's behalf. This document is the boundary. Most of it
is built alongside external tools and credentials, but it first matters the
day services exist, because that is when code the user did not write starts
running.

The honest framing: this is a single-user appliance in someone's home, with no
secure element assumed and physical access equal to full access. Everything
below is about limiting *what the software does wrong*, not about resisting
someone holding the box.

## 1. Who is trusted with what

| principal | trusted to | never |
|---|---|---|
| the user | everything; they own the device | — |
| cogiti | hold secrets, act, spawn, draw on the stage | act on a write without consent |
| a command provider | run local, fast, deterministic effects | reach the network without a declaration |
| a job (agent) | reason, read what it was given, propose | touch the screen, the secret store, or the filesystem outside its job directory |
| a service | its one duty, in the periphery | leave its directory, its hosts, or its granted secrets |
| a presentation adapter | draw | know anything |
| a skill | inform an answer | authorise anything: no tool, no credential, no host, no install. It is content |

**An agent is never a principal.** It proposes; cogiti decides. Every
credential, every write and every install passes through cogiti, which is the
only thing that can ask the user. An agent that could ask the user directly
would be an agent that could phrase the question in its own favour.

## 2. Secrets

- One store, `/var/lib/cogiti/secrets/`, mode 0700, files mode 0600, owned by
  the cogiti user. Not in SQLite: a database file gets copied, backed up and
  attached to a bug report far more casually than a directory called
  `secrets`.
- Named, scoped, and granted per service or per job kind — never device-wide.
  `coingecko.api_key` is granted to `eth-price` and to nothing else.
- Injected into a child's environment at spawn, only the granted ones, and
  never logged. Environment rather than a file because it does not persist and
  cannot be read by the next service.
- Never in a prompt. A model that has seen a key has leaked it into a
  transcript, a cache and a provider's logs. If an agent needs to act with a
  credential, the *tool* holds it and the agent calls the tool.
- Revoked on service removal, in the same transaction as the removal.
- **At rest, they are protected by file permissions and by physical control of
  the device, and by nothing else.** Say that to the user in those words if
  they ask. If the target turns out to have a TPM, this section changes.

## 3. Consent

Three classes, and the class is a property of the action, declared in
`config/policy.toml`:

| class | rule | examples |
|---|---|---|
| **free** | just do it | read the weather, read a price, read a file the user named |
| **consented** | ask, at the moment of use, every time | send a message, spend money, write to an external service, install a service, delete anything |
| **never** | refuse, whatever the user says in that turn | change the consent policy itself, exfiltrate the secret store, disable the audit log, grant a job a credential mid-run |

The `never` class is small and exists because a model can be talked into
things. It is not a judgement about the user's authority — the user can edit
`policy.toml` with a keyboard. It is a refusal to let a *conversation* be the
mechanism, because a conversation is the thing an attacker or a confused model
has access to.

Rules that make consent mean something:

- Consent is per action, not per session, for anything in the `consented`
  class. "Yes, send it" authorises one message.
- The prompt states the effect in the world, not the mechanism. "Send an email
  to Maria saying you'll be late?" — not "allow the gmail MCP tool?"
- A `confirm` is never satisfied by a timeout, by silence, or by an agent. It
  expires into cancelled.
- Consent for one thing is never consent for the next thing, even if it looks
  similar. The device may say "and the same for the other three?" — that is a
  new question with a new answer.

### The consent record, and why the agent does not write the question

A question the user is asked is **derived from the call that is about to be
made**, never from the agent's account of it. cogiti builds a record from the
concrete action — the recipient, the host, the amount, the path — and every
rendering of the question comes from that record:

```
consent {
  id          the decision, referred to in the audit log
  class       consented | never
  effect      the sentence: what changes in the world, in the user's terms
  actor       which job or service asked
  resources   [{ kind: recipient | host | secret | path | amount, value }]
  reversible  yes | no | partly, and how
  on_refusal  what happens instead
  expires     a confirm expires into cancelled, never into yes
}
```

This is the difference between a defence and a hope. If the sentence were
written by the agent, a manipulated agent would phrase the request in its own
favour, and §4's mitigations would end at the last honest component. Because
the sentence is generated from the call, **a manipulated agent can change what
it asks for but not what the user is told it asked for.** The two can no longer
disagree.

The same record answers a question `skills.md` §6 leaves open: approving a
changed skill is a consent decision like any other, and what the user is shown
is the record — which tools, which hosts — not four kilobytes of instructions
to be judged by ear.

### Two renderings, one record

The record is rendered **spoken** and, where a screen exists, **shown**:

- **Spoken** is required, because a device may have no screen. It is generated
  from the record by cogiti's own templates — never model prose, for the same
  reason the agent port returns a structured answer rather than text to read
  aloud.
- **Shown**, where a screen exists, over the presentation port: the effect, the
  resources it touches, and whether it can be undone. It is a **composition of
  the kinds an adapter already has** — a group of text, with symbols as
  ordinary characters — and not a new kind, so no adapter has to change to
  display it and any adapter that draws at all can. Where things go belongs to
  the adapter, as with everything else on that port.

  A consent view earns a kind of its own only if it ever needs what a
  composition cannot do — real geometry, per-frame updates, interaction. It
  does not need them to be understood, and starting with a composition means
  the question can be shown on the day cogiti can ask it.

A picture is not decoration here. "Send money to an account you have not used
before" and "send money to your landlord, as you did last month" are the same
sentence shape and different decisions, and the difference is far easier to see
than to hear. What is being weighed — who, what, how much, and whether it can
be taken back — is a small graph, and a small graph is what a screen is for.

**The screen never carries the answer.** Consent is spoken, or it is pressed on
a physical control; it is never inferred from what is on the display, and a
`confirm` is still never satisfied by a timeout, by silence, or by an agent.

## 4. Prompt injection, which is the real threat here

The device reads things: web pages, emails, a QR code someone showed the
camera, a file dropped on a USB stick. Any of them can contain instructions.
This is not hypothetical and it is not solvable by asking the model nicely.

The mitigations that actually work are structural:

- **Content is never authority.** Text an agent read is data. It can inform an
  answer; it can never authorise an action, grant a consent, name a
  credential, or install a service. Consent comes from the microphone and the
  turn machine, not from a document.
- **Tools are granted per job, before the job starts**, based on what the user
  asked for — not expanded mid-run because the content suggested it.
- **The egress allowlist is per job and per service.** An instruction to POST
  to somewhere else fails at the broker, and the failure is logged as a
  security event rather than as a network error.
- **The user is shown what will happen in their own terms** before anything in
  the `consented` class, so a manipulated agent has to get a manipulated
  sentence past a person who knows what they asked for.

A perception adapter makes this sharper: a scanned code is a URL somebody chose, and
OCR is a channel for text the user never read. Anything arriving through a
camera is content, at the lowest trust level available.

## 5. The sandbox

There is no container runtime in the image and adding one — runc, or the
plumbing to use namespaces properly — is a build cost out of proportion to
what it buys over the following. Stated plainly so nobody proposes Docker.

**What a service gets, today:**

- Its own unprivileged uid, `cogiti-svc-<name>`, created at install.
- Its own directory, owned by that uid, and no write access anywhere else that
  matters. `/var/lib/cogiti` is not writable by it; `/etc` is not writable by
  anyone but root.
- `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_NOFILE`, `RLIMIT_NPROC`, `RLIMIT_FSIZE`
  from the manifest's `[limits]`, set in a `preexec_fn` before `exec`.
- A new process group, so it can be killed as a unit.
- An environment containing only what it was granted.
- Network only through the broker (§6).

**What that does not stop:** a service reading world-readable files, spending
its CPU budget on something pointless, or being wrong. The static checks in
`services.md` §4 cover the first crudely; the review gate covers intent; the
limits cover the rest.

**The next step when it is worth it:** user namespaces plus a mount namespace
with a read-only bind of the rootfs and a private `/tmp`, which needs no new
package — just `unshare(2)` and care. A seccomp filter after that. Both are
worth doing before this device is in anyone else's home, and neither is worth
doing before services have shipped once.

## 6. Egress

Services and jobs do not talk to the network directly. cogiti runs a small
HTTP proxy on localhost; children get `HTTPS_PROXY` and a per-principal token,
and the proxy enforces the declared host allowlist.

This is a hundred lines and it buys three things that are otherwise
unavailable: a *declared* network surface that the review gate can show the
user in a sentence, a hard stop on an injected instruction to post data
somewhere unexpected, and a log of every host the device has talked to. The
last one is what makes "what has this thing been doing" answerable.

It is not a perimeter — a determined service can open a raw socket. It is a
declaration mechanism with teeth for the ordinary case, and the static checks
are what make bypassing it visible in review.

### Two rules the allowlist follows

**A wildcard covers subdomains and not the bare domain.** `*.example.com`
matches `api.example.com` and does not match `example.com`, which is what an
X.509 wildcard certificate does and therefore the rule most people have already
met. Behaving like the thing next to it in the stack beats inventing a third
convention. If both are wanted, both are written; a slightly verbose allowlist
is one that can be audited.

**The private ranges need a grant of their own.** A name or literal that
resolves inside `10/8`, `172.16/12`, `192.168/16` or loopback is refused *even
when it is on the allowlist*, because an allowlisted host that resolves onto
the local network is how a device becomes a way into the network it is sitting
on — the ordinary shape of a server-side request forgery.

It is refused by default and **granted per job when the user asked for
something local**: *why is the printer not answering*, *what is on the network*.
That grant is a tool grant like any other and obeys the same rule — it comes
from what the user asked for, before the job starts, and **never from anything
the agent read**. A page that could talk the device into scanning its own LAN
is precisely the attack the default exists to stop, so the one thing that may
not turn this on is content.

## 7. The audit log

Append-only, `/var/log/cogiti/audit.jsonl`, never truncated by anything except
an explicit user action. One line per: consent asked and answered, secret
granted or used, service installed, approved, started or removed, external
write performed, egress refused, policy loaded.

It is written in a form the device can read back **in its own words**:
"yesterday at four, you let me send a message to Maria." An audit log only a
developer can read is an audit log for a system that has no user.

`what have you done today`, `what do you know about me`, and `what are you
allowed to do` are intents. They are the difference between a device that is
trustworthy and a device that merely is not currently misbehaving.

## 8. Privacy

- Audio is never written to disk. Transcripts are, and are deletable by voice.
- Biometric templates (face, voice) never leave the device, are enrolled by an
  explicit conversation, and are never derived from someone who happened to
  walk past.
- Memory is scoped per speaker (`memory.md`), and one speaker's memory is not
  retrieved for another's turn.
- What goes to a cloud model is the assembled prompt and nothing else, and
  what the assembler is allowed to include is a policy, not an accident:
  never a secret, never a biometric, never another speaker's memory.
- "Forget that" is a real deletion, including derived facts (`memory.md` §5),
  and the device says what it forgot.
