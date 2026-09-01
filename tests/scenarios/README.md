# Scenarios

JSON scripts for `tests/fakes/agent.py`. One file per thing being tested; the
name says what it is, and the steps read as the scenario rather than as setup.

| | |
|---|---|
| `two-tools.json` | two tool calls outstanding at once, answered out of order |
| `stubborn.json` | ignores `SIGTERM` and leaves a grandchild — the group kill |
| `fetch-two.json` | two real fetches, brokered, in parallel |
| `forbidden-host.json` | a host off the allowlist — refused before it becomes a job |
| `ungranted-tool.json` | a tool the job was never granted — no channel to ask through |

`{PORT}` is substituted at run time. The test server binds port 0 and the OS
picks; a fixed port collides with whatever else is running on a development
machine, and with the next test class in the same file.

The fake takes JSON rather than the YAML used for scripted sessions, because it
must not import anything cogiti has and YAML is not in the standard library.
