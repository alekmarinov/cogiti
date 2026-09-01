"""The secret store.

`docs/security.md` §2. One directory, one file per secret, the bare value.

The name is the unit of granting — `coingecko.api_key` is granted to
`eth-price` and to nothing else — so a single env-file cannot express it, and
per-file permissions make "revoked on service removal" an unlink.

The value is stored bare rather than as `NAME=value` on purpose: nothing here
should look sourceable. A `source secrets` puts a credential into a shell's
environment, and everything spawned from that shell inherits it — which is the
leak this module and the explicit child environments exist to prevent.
"""

import os
import stat

MODE_FILE = 0o600
MODE_DIR = 0o700


class SecretError(Exception):
    pass


def store_dir(state_dir):
    return os.path.join(state_dir, "secrets")


def get(state_dir, name):
    """Read one secret, refusing a file anyone else can read.

    At rest these are protected by file permissions and physical control of the
    device and by nothing else — security.md §2 says so and says to tell the
    user in those words. Permissions being the whole defence is exactly why a
    wrong mode is a refusal rather than a warning.
    """
    if "/" in name or name.startswith("."):
        raise SecretError("bad secret name %r" % name)

    path = os.path.join(store_dir(state_dir), name)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise SecretError("no such secret: %s" % name)

    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SecretError(
            "%s is mode %o; it must be %o. Permissions are the only thing "
            "protecting it." % (path, stat.S_IMODE(st.st_mode), MODE_FILE))

    with open(path) as f:
        return f.read().strip()


def env_for(state_dir, grants):
    """Build the environment a child is spawned with.

    Not the parent's environment plus extras — a fresh one. Inheriting means
    every tool job and every service gets whatever happens to be exported in
    the shell cogiti was started from, including credentials meant for
    something else entirely.

    `grants` maps an environment variable name to a secret name:
        {"ANTHROPIC_API_KEY": "anthropic.api_key"}
    Which variable a program wants is that program's business, not the store's.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        # Python needs this to find cogiti.tools.http when spawned as -m.
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }
    for var, name in (grants or {}).items():
        env[var] = get(state_dir, name)
    return env
