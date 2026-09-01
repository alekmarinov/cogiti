# Scenarios

JSON scripts for `tests/fakes/agent.py`. One file per thing being tested; the
name says what it is, and the steps read as the scenario rather than as setup.

| | |
|---|---|
| `two-tools.json` | two tool calls outstanding at once, answered out of order |
| `stubborn.json` | ignores `SIGTERM` and leaves a grandchild — the group kill |

The fake takes JSON rather than the YAML used for scripted sessions, because it
must not import anything cogiti has and YAML is not in the standard library.
