"""SQLite, WAL, migrations.

The job table is the durable record. The live truth is the process table, and
the two agree only if transitions are written promptly — which is why every
state change here is a single statement and none of them are batched.

Schema is `docs/jobs.md` §2, unchanged.
"""

import sqlite3
import time

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  title         TEXT NOT NULL,
  state         TEXT NOT NULL,
  session_id    TEXT NOT NULL,
  parent_job    TEXT,
  pgid          INTEGER,
  created_ns    INTEGER NOT NULL,
  created_wall  TEXT NOT NULL,
  updated_ns    INTEGER NOT NULL,
  deadline_ns   INTEGER,
  result_json   TEXT,
  error_kind    TEXT,
  error_detail  TEXT,
  cost_tokens   INTEGER,
  progress      TEXT
);

CREATE INDEX IF NOT EXISTS job_state  ON job(state);
CREATE INDEX IF NOT EXISTS job_parent ON job(parent_job);

CREATE TABLE IF NOT EXISTS job_log (
  job_id  TEXT NOT NULL,
  seq     INTEGER NOT NULL,
  ns      INTEGER NOT NULL,
  stream  TEXT NOT NULL,
  line    TEXT NOT NULL,
  PRIMARY KEY (job_id, seq)
);
"""

# The states a job may be in, and who moves it there. Listed so that a typo
# in a state name fails here rather than silently creating a seventh state
# that nothing queries for.
STATES = {"spawn", "running", "needs-input", "done", "failed", "cancelled"}
TERMINAL = {"done", "failed", "cancelled"}


def now_ns():
    """Monotonic, for durations. Never wall clock: the device may have no
    battery-backed clock and NTP may step it mid-job."""
    return time.monotonic_ns()


def now_wall():
    """For 'you asked me this morning'. Not for arithmetic."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def open_db(path):
    db = sqlite3.connect(path, isolation_level=None)   # autocommit; see below
    db.row_factory = sqlite3.Row
    # WAL so a reader — the thing drawing a panel — never blocks the writer.
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    # NORMAL rather than FULL: a job row lost to a power cut is recovered by
    # the orphan sweep at startup anyway, and FULL costs an fsync per
    # transition on a device that may be writing to an SD card.
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    db.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
    return db


# ------------------------------------------------------------------ jobs --

def insert_job(db, job_id, kind, title, session_id, parent_job=None,
               deadline_ns=None):
    """A row before a process, always.

    The row exists in `spawn` before fork, so that a crash between fork and
    the pgid write leaves evidence rather than an untracked process group.
    """
    ns = now_ns()
    db.execute(
        "INSERT INTO job (id, kind, title, state, session_id, parent_job,"
        " created_ns, created_wall, updated_ns, deadline_ns)"
        " VALUES (?,?,?,'spawn',?,?,?,?,?,?)",
        (job_id, kind, title, session_id, parent_job, ns, now_wall(), ns,
         deadline_ns))


def set_pgid(db, job_id, pgid):
    """Written before anything else happens, per docs/jobs.md §4."""
    db.execute("UPDATE job SET pgid=?, state='running', updated_ns=?"
               " WHERE id=?", (pgid, now_ns(), job_id))


def set_state(db, job_id, state, **fields):
    if state not in STATES:
        raise ValueError("unknown job state %r" % state)
    cols = ", ".join("%s=?" % k for k in fields)
    args = list(fields.values())
    db.execute(
        "UPDATE job SET state=?, updated_ns=?%s WHERE id=?"
        % ("" if not cols else ", " + cols),
        [state, now_ns()] + args + [job_id])


def get_job(db, job_id):
    return db.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()


def children(db, job_id):
    return db.execute("SELECT * FROM job WHERE parent_job=?", (job_id,)).fetchall()


def live_jobs(db):
    return db.execute(
        "SELECT * FROM job WHERE state IN ('spawn','running','needs-input')"
    ).fetchall()


def count_live(db, kind=None, parent_job=None):
    q = "SELECT COUNT(*) FROM job WHERE state IN ('spawn','running','needs-input')"
    args = []
    if kind:
        q += " AND kind=?"; args.append(kind)
    if parent_job:
        q += " AND parent_job=?"; args.append(parent_job)
    return db.execute(q, args).fetchone()[0]


def sweep_orphans(db):
    """Every job that was running when cogiti died has no process now: its
    child died with the process group. Marked `failed`/`orphaned` at startup,
    before anything else runs, so the table never claims a process that is not
    there. Returns the ids, because the caller may want to say so.

    docs/architecture.md §5, step 2.
    """
    rows = live_jobs(db)
    ids = [r["id"] for r in rows]
    if ids:
        db.execute(
            "UPDATE job SET state='failed', error_kind='orphaned',"
            " error_detail='cogiti restarted; the process group is gone',"
            " updated_ns=? WHERE id IN (%s)" % ",".join("?" * len(ids)),
            [now_ns()] + ids)
    return ids


# ------------------------------------------------------------------- log --

LOG_RING = 2000     # "the last N lines, N in the low thousands"


def append_log(db, job_id, stream, line):
    row = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM job_log"
                     " WHERE job_id=?", (job_id,)).fetchone()[0]
    db.execute("INSERT INTO job_log (job_id, seq, ns, stream, line)"
               " VALUES (?,?,?,?,?)", (job_id, row, now_ns(), stream, line))
    # A ring, not an archive: an unbounded log is a device that fills its disk
    # because someone ran a build once.
    if row % 256 == 0:
        db.execute("DELETE FROM job_log WHERE job_id=? AND seq <= ?",
                   (job_id, row - LOG_RING))


def tail_log(db, job_id, n=50):
    rows = db.execute(
        "SELECT * FROM job_log WHERE job_id=? ORDER BY seq DESC LIMIT ?",
        (job_id, n)).fetchall()
    return list(reversed(rows))
