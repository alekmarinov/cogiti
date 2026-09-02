"""Configuration, and where every setting came from.

`docs/architecture.md` §7. A flat `key = value` file for paths, devices and
defaults. Precedence, lowest first:

    built-in default  ->  /etc/cogiti.conf  ->  COGITI_* environment  ->  a flag

Two rules that are the whole point:

**A path the config names must exist.** cogiti stops at startup and says which
line was wrong rather than falling back to something else. A deployment that
silently ran with the wrong model is unexplainable later.

**Every setting remembers who decided it**, so `--print-config` can answer "why
is this value what it is" without anyone reading four files in precedence
order.
"""

import os

DEFAULTS = {
    # Where the job table and everything the device remembers live. The
    # platform port promises this path survives an update; how a deployment
    # delivers that is its own business.
    "state_dir":     "/var/lib/cogiti",

    # The agent adapter, as an argv string. There is no default that works:
    # naming one would mean cogiti had an opinion about which model runs, and
    # the whole of ports.md exists to stop that.
    "agent_adapter": "",

    # How results reach the person. At least one output is required — see
    # require_output(). 'text' prints; a deployment with a screen or a voice
    # names its adapter instead.
    "output":        "text",

    "presentation_adapter": "",
    "speech_adapter":       "",

    # Hosts a job may reach, comma separated. Empty means a job may reach
    # nothing, which is the right default.
    "egress_hosts":  "",

    "trace_file":    "",          # empty: stderr

    # What the agent adapter is given, as `ENV_VAR=secret.name` pairs. Which
    # variable a program wants is that program's business; the store only knows
    # names. Empty means the adapter is spawned with no credential at all,
    # which is correct for a local model and for the fake.
    "agent_secrets": "",

    # The same, for the speech adapter. A cloud voice needs a credential and a
    # local one needs none, and which is in use is a deployment's business.
    "speech_secrets": "",
}

# Settings naming a path that must exist if set. state_dir is created rather
# than required — it is ours to make — so it is not here.
# A path that must exist if set. `presentation_adapter` is deliberately NOT
# here: it names a socket, and the renderer is a separate process with its own
# lifetime. Requiring the socket at startup would mean cogiti could not be
# started before the face — and the port requires it to survive the face going
# away and coming back, which makes "absent right now" a normal state rather
# than a configuration error.
MUST_EXIST = ("agent_adapter_binary", "speech_adapter")


class ConfigError(Exception):
    pass


class Config:
    def __init__(self, values, origins):
        self._v, self._origin = values, origins

    def __getitem__(self, key):
        return self._v[key]

    def get(self, key, default=None):
        return self._v.get(key, default)

    def origin(self, key):
        return self._origin.get(key, "unset")

    def secret_grants(self, key="agent_secrets"):
        """{"ANTHROPIC_API_KEY": "anthropic.api_key"} from `agent_secrets`,
        or from any other `*_secrets` setting."""
        out = {}
        for pair in self.list(key):
            if "=" not in pair:
                raise ConfigError(
                    "%s entry %r is not ENV_VAR=secret.name" % (key, pair))
            var, name = (p.strip() for p in pair.split("=", 1))
            out[var] = name
        return out

    def list(self, key):
        raw = self._v.get(key, "")
        return [p.strip() for p in raw.split(",") if p.strip()]

    def print_config(self):
        width = max(len(k) for k in self._v)
        for k in sorted(self._v):
            print("%-*s = %-28s (%s)" % (width, k, self._v[k] or "''",
                                         self.origin(k)))


def load(argv=None, conf_path="/etc/cogiti.conf", environ=None):
    environ = os.environ if environ is None else environ
    values = dict(DEFAULTS)
    origins = {k: "built-in default" for k in DEFAULTS}

    # 1. the file
    if os.path.exists(conf_path):
        for n, line in enumerate(open(conf_path), 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" not in line:
                raise ConfigError("%s:%d: not a key = value line: %r"
                                  % (conf_path, n, line))
            k, v = (p.strip() for p in line.split("=", 1))
            values[k], origins[k] = v, "%s:%d" % (conf_path, n)

    # 2. the environment
    for k in list(values):
        env = "COGITI_" + k.upper()
        if env in environ:
            values[k], origins[k] = environ[env], "$" + env

    # 3. flags, --key=value
    for arg in (argv or []):
        if arg.startswith("--") and "=" in arg:
            k, v = arg[2:].split("=", 1)
            k = k.replace("-", "_")
            values[k], origins[k] = v, "--%s" % k

    _check_paths(values, origins)
    return Config(values, origins)


def _check_paths(values, origins):
    """Stop at startup, naming the line, rather than falling back."""
    for key in MUST_EXIST:
        path = values.get(key, "")
        if path and not os.path.exists(path.split()[0]):
            raise ConfigError(
                "%s names %r, which does not exist (set at %s)"
                % (key, path, origins.get(key, "?")))


def require_output(cfg):
    """cogiti must have some way to tell the user something.

    With no presentation adapter and no speech adapter and no text output, it
    can still resolve, escalate, spawn jobs and reach the network — and nobody
    would ever learn what it decided. That is worse than not starting: it looks
    like it is working.

    So at least one output is required and the absence is a startup failure,
    named, like every other missing capability.
    """
    if cfg["output"] == "text":
        return "text"
    if cfg["presentation_adapter"] or cfg["speech_adapter"]:
        return cfg["presentation_adapter"] and "presentation" or "speech"
    raise ConfigError(
        "no output configured: set output=text, or name a presentation_adapter "
        "or a speech_adapter. cogiti will not start without a way to answer.")
