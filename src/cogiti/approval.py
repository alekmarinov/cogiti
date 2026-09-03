"""What the user said yes to, and whether it is still that thing.

`docs/services.md` §4. The approval binds to a hash of **the code and the
manifest**, and the manifest is not an afterthought: a service that widens its
phrase list has changed what the device hears even though no code moved, and
that is a new approval rather than a continuation of the old one.

The case this actually guards is an agent editing a service it wrote last
week.

A service whose files no longer match does not start. It is quarantined and
reported — not deleted, because a mismatch may be a person editing their own
device, and §7 keeps thirty days of undo precisely because these decisions get
made wrongly.
"""

import hashlib
import json
import os
import time

FILENAME = "approved"


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def record(service_dir, spoken, phrases, hosts, secrets, entry="main.py"):
    """Write what was approved, in the words it was approved in.

    `spoken` is kept because the gate is spoken: what the person actually
    agreed to is the sentence they heard, not a summary reconstructed later
    from fields. If those two ever disagree, the sentence is the truth about
    the consent and the fields are the truth about the software.
    """
    data = {
        "approved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "spoken": spoken,
        "phrases": list(phrases),
        "hosts": list(hosts),
        "secrets": list(secrets),
        "source_sha": digest(os.path.join(service_dir, entry)),
        "manifest_sha": digest(os.path.join(service_dir, "service.toml")),
    }
    path = os.path.join(service_dir, FILENAME)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return data


def load(service_dir):
    try:
        with open(os.path.join(service_dir, FILENAME)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def verify(service_dir, entry="main.py"):
    """Returns (ok, reason). Never raises: a broken approval is a service that
    does not start, which is a thing to report rather than an exception to
    handle three frames up."""
    a = load(service_dir)
    if a is None:
        return False, "it has no approval record"
    try:
        code = digest(os.path.join(service_dir, entry))
        man = digest(os.path.join(service_dir, "service.toml"))
    except OSError as e:
        return False, "cannot read it: %s" % e
    if code != a.get("source_sha"):
        return False, "its code has changed since you approved it"
    if man != a.get("manifest_sha"):
        # Named separately because it is the one people will not expect, and
        # because "what it says it does" changing is exactly as much a new
        # decision as "what it does" changing.
        return False, "its manifest has changed since you approved it"
    return True, ""
