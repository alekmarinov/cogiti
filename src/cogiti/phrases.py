"""Reaching a service that did not exist when the resolver was compiled.

`docs/services.md` §5. reflexi's blob was built before this service was born,
so nothing in it can resolve "what is bitcoin at". This is the layer that can,
and §5 chose it over the two alternatives — including one an earlier draft of
that document proposed as the right answer.

**Exact matching only, and built-ins always win.** The same tier discipline
reflexi's pre-matcher has, for the same reason: a service written by an agent
this afternoon must not be able to take traffic from a built-in intent on a
fuzzy score. Nothing here computes a similarity.

**The patterns are read from installed manifests, never cached.** §2 says the
manifest is the truth and cogiti holds nothing about a service that is not
derivable from its directory — so removing a service removes its phrases by
construction, and a pattern cannot outlive the thing it points at.

What this costs, in §5's own words: listed phrasings only. "what is eth at"
reaches the service and "how is ethereum looking" does not — it escalates like
any other unrecognised sentence, and the model remains the general answer.
"""

import re

#: Trailing politeness and punctuation. Deliberately small: this is the only
#: liberty taken with the utterance, because every additional one is a way for
#: a service to match something that was not said to it.
_TRIM = re.compile(r"^(please\s+|hey\s+|ok\s+|okay\s+)+|[.?!,\s]+$")
_SPACE = re.compile(r"\s+")


def normalise(text):
    """Lowercase, collapse whitespace, drop trailing punctuation.

    Not reflexi's normaliser, and that is a compromise worth naming: reflexi's
    is C behind the resolver port and normalises for *tokenisation*, expanding
    contractions and folding numbers. Matching a spoken phrase against a phrase
    somebody typed into a manifest wants far less than that. The risk is two
    ideas of "normalised" drifting apart, and it is mitigated by this being
    four lines that do nothing clever.
    """
    t = _SPACE.sub(" ", (text or "").strip().lower())
    prev = None
    while prev != t:
        prev = t
        t = _TRIM.sub("", t).strip()
    return t


def match(text, manifests):
    """The service whose manifest claimed this sentence, or None.

    `manifests` is whatever is installed *now*. An ambiguous claim — two
    services listing the same phrase — resolves to neither: it is a question,
    and answering with one of them at random is how a device becomes
    unpredictable in the way people remember.
    """
    want = normalise(text)
    if not want:
        return None
    hits = [m for m in manifests
            if any(normalise(p) == want for p in m.phrases)]
    if len(hits) != 1:
        return None
    return hits[0]


def claimed_by_others(phrases, manifests, me=None):
    """Which of these phrases another installed service already claims.

    For the review gate: a service asking for a sentence somebody else's
    service already answers is a collision the user should hear about before
    approving it, not after.
    """
    taken = {}
    for m in manifests:
        if me is not None and m.name == me:
            continue
        for p in m.phrases:
            taken[normalise(p)] = m.title
    return {p: taken[normalise(p)] for p in phrases if normalise(p) in taken}


def unreachable(patterns, resolve):
    """Which of these phrases a built-in already answers.

    Built-ins always win (§5), so a phrase the resolver already recognises
    will never reach the service that claimed it. That is the rule working —
    and the consequence is a service listing a sentence it will never receive,
    with nobody any the wiser.

    Found on a device: a weather service claimed "show the weather", which is
    an exemplar of the built-in `pin_thing`. The built-in took it, correctly,
    and the manifest went on saying the service would answer it.

    So the gate says so. `resolve` is cogiti's resolver call, passed in rather
    than reached for, because this module knows about phrases and not about
    how the device happens to resolve anything.
    """
    out = {}
    for p in patterns:
        try:
            d = resolve(normalise(p))
        except Exception:                                     # noqa: BLE001
            continue
        intent = getattr(d, "intent_id", None) if d is not None else None
        if intent:
            out[p] = intent
    return out
