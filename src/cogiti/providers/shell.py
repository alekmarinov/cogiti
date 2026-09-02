"""Effects that belong to the device, run as a configured command.

Volume, mute and power are hardware, and `ports.md` puts hardware behind the
platform port rather than inside cogiti. Until that port has a real adapter,
this is the seam: the command comes from the table, which is a deployment's
file, so cogiti still names no device.

Two things it will not do. **The command is a list, never a string**, so
nothing is passed to a shell and an argument built from a slot cannot become
a second command. And **a slot is never interpolated into the command** — the
table's `args` may only supply values through `{}` placeholders that are
substituted as single argv entries, so `set_volume` with a slot of
`70; rm -rf /` passes one argument containing a semicolon to `amixer`, which
rejects it.
"""

import shutil
import subprocess

from . import Result, provider

TIMEOUT_S = 5


@provider("shell.run")
def run(_command=None, _source=None, **args):
    if not _command:
        return Result.failed("unavailable")
    argv = [_fill(part, args) for part in _command]
    if not shutil.which(argv[0]):
        return Result.failed("unavailable")
    try:
        p = subprocess.run(argv, capture_output=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return Result.failed("timeout")
    except OSError:
        return Result.failed("unavailable")
    if p.returncode != 0:
        return Result.failed("refused")
    return Result(values=dict(args,
                              output=p.stdout.decode("utf-8", "replace").strip()),
                  source=_source or argv[0])


def _fill(part, args):
    """One argv entry, with {slot} substituted. Substitution happens *inside*
    an entry and can never split it, which is what keeps a slot from becoming
    a second argument."""
    for k, v in args.items():
        part = part.replace("{%s}" % k, str(v))
    return part
