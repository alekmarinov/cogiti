"""What a generated service may contain. `docs/services.md` §4, step 4.

This exists so that step 5 — the person, out loud — is a decision about
**purpose** rather than about code. The user is being asked whether they want a
thing that reads coingecko every minute. They are not being asked to audit
Python, and §4 is blunt about why that matters: *a gate that requires them to
is a gate that will be answered yes every time.*

So everything a reviewer would have to look for, this looks for instead.

**It is a whitelist, not a blacklist.** Not because blacklists are unfashionable
but because this one has an adversary with a rewrite loop: an agent that fails
a named check and is told why will try the next thing, and a list of forbidden
names is a list of the things somebody already thought of. An allowed set of
imports and a refusal of the dangerous *forms* leaves nothing to enumerate.

It reads the source and never runs it. Anything that decides what to do at
runtime — `__import__(name)`, `getattr(os, "sys" + "tem")` — cannot be seen
here, which is why the forms that make that possible are refused outright
rather than inspected.
"""

import ast

#: What a service may import. Everything a duty on a screen needs, and nothing
#: that reaches the machine. `cogiti.service` is the SDK, which is the only
#: door to the network and the screen.
ALLOWED_IMPORTS = {
    "cogiti.service", "cogiti",
    "json", "time", "datetime", "math", "re", "random", "os",
    "collections", "itertools", "statistics", "textwrap", "decimal",
    "zoneinfo", "urllib.parse",
}

#: Refused by name, with the reason a person would give. These are the forms
#: that make the rest of the analysis meaningless, not merely dangerous ones.
FORBIDDEN_CALLS = {
    "eval":     "eval runs code decided at runtime, which nothing here can read",
    "exec":     "exec runs code decided at runtime, which nothing here can read",
    "compile":  "compile builds code at runtime",
    "__import__": "__import__ imports a name decided at runtime",
    "open":     "a service keeps its state through the SDK, not by opening files",
    "input":    "a service has nobody to type at it",
}

#: `os` is allowed because a service reads its own environment — the supervisor
#: puts its name there. Nothing else about it is.
OS_ALLOWED = {"environ", "getenv", "path", "sep"}

MAX_SOURCE_BYTES = 32 * 1024


class Refusal(Exception):
    """One reason, in the words the device will use. Never a list: the agent
    gets told the first thing that was wrong and rewrites, and a wall of
    complaints is a rewrite that fixes none of them."""

    def __init__(self, reason, line=None):
        self.reason = reason
        self.line = line
        super().__init__("line %s: %s" % (line, reason) if line else reason)


def check(source, manifest=None):
    """Raises Refusal, or returns the set of hosts the code mentions.

    The hosts are returned so the caller can compare them against the
    manifest: a service that reads a URL the manifest does not declare is a
    service whose description is wrong, and the description is the thing the
    user approved.
    """
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise Refusal("longer than %d KB, which is more than the review gate "
                      "can honestly show" % (MAX_SOURCE_BYTES // 1024))
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise Refusal("it does not parse: %s" % e.msg, e.lineno)

    hosts = set()
    for node in ast.walk(tree):
        _check_node(node, hosts)

    if manifest is not None:
        declared = {h.lower() for h in manifest.allow}
        undeclared = hosts - declared
        if undeclared:
            raise Refusal(
                "it reaches %s, which its manifest does not declare"
                % ", ".join(sorted(undeclared)))
    return hosts


def _check_node(node, hosts):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for name in _imported_names(node):
            if name not in ALLOWED_IMPORTS:
                raise Refusal(
                    "it imports %s, which a service may not use" % name,
                    node.lineno)

    elif isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in FORBIDDEN_CALLS:
            raise Refusal(FORBIDDEN_CALLS[fn.id], node.lineno)
        # os.system, os.popen, os.remove — `os` is allowed for environ and
        # nothing else, so the attribute is what decides.
        if (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "os" and fn.attr not in OS_ALLOWED):
            raise Refusal("os.%s is not something a service does" % fn.attr,
                          node.lineno)

    elif isinstance(node, ast.Attribute):
        # getattr-free dynamic access is still reachable through dunders;
        # refusing the names outright is cheaper than modelling them.
        if node.attr in ("__globals__", "__builtins__", "__subclasses__",
                         "__class__", "__bases__", "__code__"):
            raise Refusal("%s is a way around every other check here"
                          % node.attr, node.lineno)

    elif isinstance(node, ast.Name) and node.id in ("getattr", "setattr",
                                                    "globals", "locals",
                                                    "vars"):
        raise Refusal("%s resolves names at runtime, which nothing here can "
                      "read" % node.id, node.lineno)

    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        host = _host_in(node.value)
        if host:
            hosts.add(host)


def _imported_names(node):
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    # `from cogiti.service import Service` -> "cogiti.service"
    return [node.module or ""]


def _host_in(text):
    if "://" not in text:
        return None
    from urllib.parse import urlparse
    try:
        return (urlparse(text).hostname or "").lower() or None
    except ValueError:
        return None
