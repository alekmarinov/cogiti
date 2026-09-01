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

What goes in a prompt is a policy, not a convenience: current facts only,
this speaker only, never a secret, never a biometric, and an explicit marker
on anything inferred so the model does not launder a guess into a statement.

## 4. The write path

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

## 5. Forgetting

"Forget that" must actually forget:

1. Resolve what "that" is. Ambiguity is a question — deleting the wrong memory
   is not recoverable by talking.
2. Delete the fact.
3. **Cascade**: delete everything with it in `derived_from`, transitively.
4. Delete the transcript spans that were its `source`, if the user asked to
   forget rather than to correct.
5. Say what was forgotten, by name, so a misheard request is caught.

A correction is not a deletion: "no, it's Mariya" closes the old row and opens
a new one, and the history stays. Only an explicit forget deletes.

`what do you know about me` lists it, out loud, grouped by entity, with
provenance spoken. That intent is not a nicety — it is the only way a user can
find the wrong fact before it embarrasses the device.

## 6. Scope

Memory is keyed by `speaker_id` from day one, even while every speaker is
`owner` (`CLAUDE.md` §4). With no perception adapter it is one column doing
nothing. With one, it is the difference between an assistant and an incident.

Device-level facts — where the device is, what it is called, what services are
installed — are not memory. They are configuration and state, and they live
elsewhere. If a fact would be true after a factory reset, it is not memory.

## 7. The eval

A labelled corpus of conversations with expected extractions, expected
retrievals, and expected *refusals* to extract. The negative cases carry the
weight: an utterance that sounds like a statement of fact but is a hypothetical
("if I worked at X..."), a joke, or somebody else's fact mentioned in passing.

Report precision and recall separately on the write path and hold precision
above recall in every trade. Both numbers in the commit message.
