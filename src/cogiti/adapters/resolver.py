"""cogiti's half of the resolver port, bound to reflexi through ctypes.

`docs/ports.md`: an utterance in, a decision out. Never an action, never a
model call, never the network.

**This port is a linked library, not a process.** It is the one exception in
`ports.md` — there is nothing to connect to, nothing to reconnect to, and no
failure mode where the adapter is "away". That is not an implementation detail
of reflexi: it is the reason the fast path exists. cogiti resolves *every
partial transcript*, and a fork per partial would cost milliseconds against
this call's microseconds, spending the whole budget the fast path was built to
save.

Everything below is the ABI in `../reflexi/include/reflexi.h`. A struct laid
out wrongly here does not crash — it silently reads the wrong field, which is
why `tests/test_slice4_resolver.py` checks the offsets against the C compiler
rather than against this file.
"""

import ctypes
import os

# reflexi.h. Changing either of these means the structures below are wrong.
SLOTS_MAX = 8
SLOT_LEN = 64
UTTERANCE_MAX = 512

OK, E_ARG, E_IO, E_FORMAT, E_VERSION, E_CONTRACT, E_MEMORY, E_TOO_LONG = range(8)

HANDLE, CONFIRM, ESCALATE = 0, 1, 2
VERDICTS = {HANDLE: "handle", CONFIRM: "confirm", ESCALATE: "escalate"}
TIERS = {0: "none", 1: "pattern", 2: "similar", 3: "context"}
SLOT_TYPES = {0: "text", 1: "number", 2: "percent", 3: "duration",
              4: "datetime", 5: "gazetteer", 6: "enum"}


class ResolverError(Exception):
    pass


class _Slot(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("type", ctypes.c_int),
        ("value", ctypes.c_char * SLOT_LEN),
        ("text", ctypes.c_char * SLOT_LEN),
        ("defaulted", ctypes.c_bool),
    ]


class _Decision(ctypes.Structure):
    _fields_ = [
        ("intent_id", ctypes.c_char_p),
        ("confidence", ctypes.c_float),
        ("verdict", ctypes.c_int),
        ("tier", ctypes.c_int),
        ("slots", _Slot * SLOTS_MAX),
        ("slot_count", ctypes.c_int),
        ("missing_slot", ctypes.c_char_p),
        ("runner_up_id", ctypes.c_char_p),
        ("runner_up", ctypes.c_float),
        ("rejected", ctypes.c_bool),
        ("normalized", ctypes.c_char * UTTERANCE_MAX),
    ]


TRACE_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p)
CLOCK_FN = ctypes.CFUNCTYPE(ctypes.c_int64, ctypes.c_void_p)


class _Options(ctypes.Structure):
    _fields_ = [
        ("blob_path", ctypes.c_char_p),
        ("blob_data", ctypes.c_void_p),
        ("blob_size", ctypes.c_size_t),
        ("config_path", ctypes.c_char_p),
        ("device_location", ctypes.c_char_p),
        ("trace", TRACE_FN),
        ("trace_user", ctypes.c_void_p),
        ("clock_ns", CLOCK_FN),
        ("clock_user", ctypes.c_void_p),
    ]


class Decision:
    """What cogiti acts on. A plain object, not the ctypes struct.

    The struct's `intent_id` points into the blob and stays valid until close;
    its `value` buffers do not survive the next resolve. Copying here means a
    decision can be kept, traced and compared without anyone having to know
    that — and a decision held past the next utterance is exactly the bug that
    would otherwise appear once, in production, months later.
    """

    __slots__ = ("intent_id", "confidence", "verdict", "tier", "slots",
                 "missing_slot", "runner_up_id", "runner_up", "rejected",
                 "normalized")

    def __init__(self, d):
        self.intent_id = d.intent_id.decode() if d.intent_id else None
        self.confidence = round(d.confidence, 4)
        self.verdict = VERDICTS.get(d.verdict, "escalate")
        self.tier = TIERS.get(d.tier, "none")
        self.missing_slot = d.missing_slot.decode() if d.missing_slot else None
        self.runner_up_id = d.runner_up_id.decode() if d.runner_up_id else None
        self.runner_up = round(d.runner_up, 4)
        self.rejected = bool(d.rejected)
        self.normalized = d.normalized.decode("utf-8", "replace")
        self.slots = {}
        for i in range(min(d.slot_count, SLOTS_MAX)):
            s = d.slots[i]
            name = s.name.decode() if s.name else "slot%d" % i
            self.slots[name] = {
                "value": s.value.decode("utf-8", "replace"),
                "text": s.text.decode("utf-8", "replace"),
                "type": SLOT_TYPES.get(s.type, "text"),
                # Never dropped. "the weather where you always mean" and "the
                # weather in Sofia because you said Sofia" are different
                # answers, and a confirmation that claims the user said
                # something they did not is wrong.
                "defaulted": bool(s.defaulted),
            }

    def __repr__(self):
        return "<%s %s %.2f %s%s>" % (
            self.verdict, self.intent_id, self.confidence, self.tier,
            " missing=%s" % self.missing_slot if self.missing_slot else "")


class Resolver:
    def __init__(self, library, blob, config=None, device_location=None,
                 on_trace=None):
        for path, what in ((library, "library"), (blob, "blob")):
            if not os.path.exists(path):
                raise ResolverError("resolver %s does not exist: %s" % (what, path))

        self._lib = ctypes.CDLL(library)
        self._bind()

        # Kept on self: ctypes does not hold a reference to a callback, and a
        # collected trampoline is a segfault at the next resolve.
        self._trace_cb = TRACE_FN(self._on_trace) if on_trace else TRACE_FN()
        self._on_trace_user = on_trace

        opts = _Options(
            blob_path=blob.encode(),
            config_path=config.encode() if config else None,
            device_location=(device_location.encode()
                             if device_location else None),
            trace=self._trace_cb,
        )
        self._handle = ctypes.c_void_p()
        rc = self._lib.reflexi_open(ctypes.byref(opts), ctypes.byref(self._handle))
        if rc != OK:
            raise ResolverError("reflexi_open: %s" % self.strerror(rc))
        self._decision = _Decision()          # reused; no allocation per turn

    def _bind(self):
        L = self._lib
        L.reflexi_open.argtypes = [ctypes.POINTER(_Options),
                                   ctypes.POINTER(ctypes.c_void_p)]
        L.reflexi_open.restype = ctypes.c_int
        L.reflexi_resolve.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                      ctypes.POINTER(_Decision)]
        L.reflexi_resolve.restype = ctypes.c_int
        L.reflexi_close.argtypes = [ctypes.c_void_p]
        L.reflexi_close.restype = None
        L.reflexi_reset_context.argtypes = [ctypes.c_void_p]
        L.reflexi_reset_context.restype = None
        L.reflexi_strerror.argtypes = [ctypes.c_int]
        L.reflexi_strerror.restype = ctypes.c_char_p
        L.reflexi_version.argtypes = []
        L.reflexi_version.restype = ctypes.c_char_p

    def _on_trace(self, _user, line):
        try:
            self._on_trace_user(line.decode("utf-8", "replace"))
        except Exception:                                     # noqa: BLE001
            # A raising callback would unwind through C. Whatever the trace
            # was for, it is not worth taking the turn down.
            pass

    # ------------------------------------------------------------ resolve --

    def resolve(self, utterance):
        """The whole port. Microseconds, no allocation, no network."""
        if len(utterance.encode()) >= UTTERANCE_MAX:
            # reflexi would return E_TOO_LONG; an over-long utterance is a
            # thing to escalate, not an error to raise at the turn machine.
            return None
        rc = self._lib.reflexi_resolve(self._handle, utterance.encode(),
                                       ctypes.byref(self._decision))
        if rc != OK:
            return None
        return Decision(self._decision)

    def reset_context(self):
        """Forget the previous turn, so the next 'and tomorrow?' resolves to
        nothing. The conversation ended, or the speaker changed."""
        self._lib.reflexi_reset_context(self._handle)

    def strerror(self, rc):
        s = self._lib.reflexi_strerror(rc)
        return s.decode() if s else "status %d" % rc

    @property
    def version(self):
        return self._lib.reflexi_version().decode()

    def close(self):
        if getattr(self, "_handle", None):
            self._lib.reflexi_close(self._handle)
            self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:                                     # noqa: BLE001
            pass
