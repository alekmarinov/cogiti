# Memory

What the system knows, how it learned it, and how it forgets.

**This is a proposal.** Agree the schema before it is built. It is also the
stage most likely to be worth moving earlier: poor agent output is usually
missing context, not a missing model capability, and the moment you find
yourself re-explaining the same project to every job, this stage has already
started paying for itself.

## 1. The one idea

A fact without provenance is a rumour. Everything here follows from refusing
to store facts without recording how they were learned, because the failure
mode of a memory system is not forgetting — it is a device confidently
repeating, for a year, something it guessed once.

```
stated    the user said it, in those words        "my sister is called Maria"
observed  the device saw or measured it           the device is in Sofia (GPS)
inferred  something concluded it from other facts "Maria is family"
```

These are not equal and are never merged. A stated fact overrides an inferred
one without a question. An inferred fact that contradicts a stated one is
discarded, not reconciled. And the device speaks about them differently:
*your sister Maria* versus *I think Maria is your sister* — which is a small
thing that does more for trust than any amount of accuracy.

## 2. Schema

```sql
CREATE TABLE entity (
  id         TEXT PRIMARY KEY,        -- ULID
  kind       TEXT NOT NULL,           -- person | project | place | thing | preference
  name       TEXT NOT NULL,
  speaker_id TEXT NOT NULL,           -- whose world this belongs to
  created_ns INTEGER NOT NULL,
  UNIQUE (speaker_id, kind, name)
);

CREATE TABLE fact (
  id          TEXT PRIMARY KEY,
  entity_id   TEXT NOT NULL,
  attribute   TEXT NOT NULL,          -- 'works_at', 'prefers', 'lives_in'
  value       TEXT NOT NULL,
  provenance  TEXT NOT NULL,          -- stated | observed | inferred
  source      TEXT NOT NULL,          -- turn id, job id, service name, sensor
  confidence  REAL,                   -- only meaningful for inferred
  valid_from  TEXT NOT NULL,          -- wall clock; facts have a history
  valid_to    TEXT,                   -- NULL = current; set, never deleted, on contradiction
  derived_from TEXT,                  -- fact ids, for the forget cascade
  created_ns  INTEGER NOT NULL
);

CREATE TABLE relation (
  subject_id  TEXT NOT NULL,
  predicate   TEXT NOT NULL,
  object_id   TEXT NOT NULL,
  provenance  TEXT NOT NULL,
  source      TEXT NOT NULL,
  valid_from  TEXT NOT NULL,
  valid_to    TEXT
);
```

Two decisions embedded there worth naming:

**Contradiction closes a row, it does not update one.** Newer wins, the older
value gets a `valid_to`, and the history stays. "You told me in March that you
worked at X" is answerable, and a wrong correction is recoverable.

**`derived_from` is what makes forgetting work.** Without it, "forget that I
work at X" leaves behind the three things that were inferred from it, and the
device keeps behaving as though it remembers something it says it forgot —
which is worse than never having forgotten.

## 3. Retrieval, before asking

The rule is in the roadmap and it is the whole user-visible point of this
stage: **query memory before putting a question to the user.** A device that
asks which project you mean, when it has only ever heard of one, is a device
that is not paying attention.

Retrieval happens in two places:

- **Slot filling.** A missing required slot goes to memory before it goes to
  the user. the resolver's `escalate` with `missing_slot` set is exactly the hook.
- **Prompt assembly.** Every job gets the entities relevant to its request,
  with their provenance, and a hard budget on how much. Prompt assembly is a
  module from the day escalation existed, precisely so this could be added
without rewriting it.
- **Template binding, which is the one that runs most.** A command's `speak`
  already substitutes `{hostname}`; it can substitute a remembered fact the
  same way, in microseconds and with no model call.

  This matters more than it sounds. The fast path is where personality goes
  to die: *hello*, *what is your name*, *can you hear me* are the most human
  things anybody says to the device and exactly the ones the resolver answers
  from a constant string, so a persona written into an agent's system prompt
  reaches none of them. Measured on a device: "hello, what's your name?"
  resolved to `get_hostname` and was answered *"I am inteliboy."* — correct,
  instant, and the reply of a machine.

  `speak = "Hello, {speaker_name}."` costs nothing and is the difference. It
  also means the first fact worth having is the one that makes every
  subsequent greeting personal, which is a good argument for asking for it
  early rather than waiting to overhear it.

What goes in a prompt is a policy, not a convenience: current facts only,
this speaker only, never a secret, never a biometric, and an explicit marker
on anything inferred so the model does not launder a guess into a statement.

## 4. Volunteering

§3 is memory used when somebody asked. This is memory used when nobody did,
and it is a different thing with a different failure mode. Retrieval is wrong
when it misses. Volunteering is wrong when it *lands* — at the wrong moment,
into the wrong silence, for the third time.

The decision is that it should, when relevant, the way a person does. What
follows is the restraint that makes that bearable, because a companion who
volunteers everything relevant is exhausting and a device that talks into a
room is the fastest way to get itself unplugged.

**cogiti is reactive today and this is the seam where that changes.** A turn
exists because something was heard. The only code that speaks unbidden is
`Session._deliver_pending`, and even that is an answer to a question that was
asked. Everything below is built at that seam and not beside it.

### Three tiers, which feel alike and are not

**A — inside a turn.** The answer to what was asked carries what is
remembered. *"Rainy all afternoon, and you said Peter has football at ten, so
that will be a wet one."* No new machinery: the facts arrive in prompt
assembly with everything else and the model writes one sentence instead of
two. This is most of what being remembered actually feels like, and it
carries no risk of interrupting anybody. **Build this first and it may be
enough.**

**B — at the seam.** After a turn ends and nothing else is happening, the
device may add one thing. `_deliver_pending` already owns that moment and
already has the right instinct — "a device that speaks an old answer over a
new question is worse than one that waits another minute". A volunteered
remark is the same shape: it goes where a detached answer goes, under the
same rule, and never into a silence that was not just filled by a
conversation. People volunteer at the end of an exchange, not into a quiet
room.

**C — into a quiet room.** Requires knowing somebody is there, which is the
`perception` port, which is not built. Until it is, this tier does not exist
— a device speaking to furniture is only funny once, and a device speaking
over a conversation it cannot hear is worse than that.

### The periphery is the first channel, and speech is the exception

The device has something a speaker does not: a face, and a periphery that
`services.md` §1 hands to services and nothing else, under avatari's rule
that the periphery never takes the gaze.

So a volunteered fact **appears before it is spoken**. `Peter · football
10:00` in the corner is the device's equivalent of a glance: it offers
without interrupting, it can be ignored at no cost to anybody, and it is
free when the room is empty. Speech is reserved for the few things that earn
breaking a silence, and "earns it" means time-critical or costly to miss —
not merely true and not merely interesting.

This is also what lets tier C exist before `vidi` does. Showing something to
an empty room is nothing. Saying it is a fault.

### The ledger

    volunteered(fact_id, when_ns, channel, acknowledged)

Somebody who tells you the same thing twice is worse than somebody who never
mentioned it. Nothing is offered twice on the same channel without a reason
that is written down, and "the user asked again" is such a reason.

`acknowledged` is what makes back-off possible rather than aspirational: it
records whether the remark was answered, referred to, or acted on.

### Back-off, as a rule and not a judgement

Three offerings of a kind that go unremarked, and that kind stops being
offered. Not because it was wrong — it may have been right every time — but
because being ignored three times is the only signal available in a system
that cannot see a face, and a proactive device that cannot take a hint is the
one people switch off.

Turning it off entirely is a setting, and asking for silence out loud
(*"stop telling me things"*) sets it. That request is a fact like any other,
with provenance `stated`, and it outranks every relevance score in the
system.

### What is never volunteered

Anything `inferred`. A guess offered unprompted is how the device
confidently tells somebody something about their own life that is not true,
and it will be believed precisely because nobody asked. Volunteering is for
`stated` and `observed` facts. An inference may be *used* to decide that a
stated fact is relevant — never to become the remark itself.

## 5. The write path

The dangerous half. Precision matters far more than recall here — a wrong fact
is worse than a missing one, because a missing one gets asked about and a wrong
one does not.

- **Stated facts** are extracted from the turn, after it completes, by a small
  local pass over the transcript. Explicit statements only: "my sister is
  Maria", "I prefer metric", "the project is called cogiti". Not implications.
- **Observed facts** come from providers and services with a `source`, and are
  written directly. Location, device state, a calendar event that was read.
- **Inferred facts** are written only by an explicit reflection job, never as a
  side effect of answering, and always with `derived_from` populated. If you
  cannot say which facts an inference came from, it does not get written.
- **Nothing is written from an agent's prose.** The agent proposes structured
  candidates; cogiti decides. This is the same rule as everywhere else in the
  system, and it is what stops a hallucinated detail becoming a permanent one.
- **Uncertain and important is a question, not a write.** "Did you say Maria or
  Mariya?" costs one turn and saves a year of being wrong.

## 6. Forgetting

"Forget that" must actually forget:

1. Resolve what "that" is. Ambiguity is a question — deleting the wrong memory
   is not recoverable by talking.
2. Delete the fact.
3. **Cascade**: delete everything with it in `derived_from`, transitively.
   This includes its rows in `volunteered` (§4): a forgotten fact that is
   still on the ledger is one the device will never mention again and can no
   longer explain why, and if it is ever learned afresh it will be treated as
   already said.
4. Delete the transcript spans that were its `source`, if the user asked to
   forget rather than to correct.
5. Say what was forgotten, by name, so a misheard request is caught.

A correction is not a deletion: "no, it's Mariya" closes the old row and opens
a new one, and the history stays. Only an explicit forget deletes.

`what do you know about me` lists it, out loud, grouped by entity, with
provenance spoken. That intent is not a nicety — it is the only way a user can
find the wrong fact before it embarrasses the device.

## 7. Scope

Memory is keyed by `speaker_id` from day one, even while every speaker is
`owner` (`CLAUDE.md` §4). With no perception adapter it is one column doing
nothing. With one, it is the difference between an assistant and an incident.

Device-level facts — where the device is, what it is called, what services are
installed — are not memory. They are configuration and state, and they live
elsewhere. If a fact would be true after a factory reset, it is not memory.

## 8. The eval

A labelled corpus of conversations with expected extractions, expected
retrievals, and expected *refusals* to extract. The negative cases carry the
weight: an utterance that sounds like a statement of fact but is a hypothetical
("if I worked at X..."), a joke, or somebody else's fact mentioned in passing.

Report precision and recall separately on the write path and hold precision
above recall in every trade. Both numbers in the commit message.

Volunteering (§4) needs its own set and it is graded the other way round.
Retrieval is measured on what it finds; volunteering is measured on **what it
declines to say**, because every false positive is an interruption of a real
person in a real room and no amount of relevant remarks pays one back.

The corpus is moments rather than utterances — a fact, a channel, and a point
in a conversation — and the expected answer is usually *nothing*. Cases worth
having: the third unremarked offering of a kind, which must stop; an inferred
fact that is relevant and must still be withheld; a stated fact that was
already volunteered on the same channel; something time-critical enough to
earn speech rather than the periphery; and anything at all after the user has
asked for silence.
