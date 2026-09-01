"""Slice 1: the job and protocol layer.

Stdlib unittest, no dependencies. These are the cases docs/jobs.md says must
exist — 'if that test does not exist, cancellation does not work, it has only
not been observed failing.'
"""
import json, os, subprocess, sys, tempfile, time, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import db as D, jobs, trust                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fakes", "agent.py")
RUN = json.dumps({"v": 1, "type": "run", "job": "J", "prompt": {},
                  "tools": [], "budget": {}}) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = D.open_db(os.path.join(self.tmp, "jobs.db"))
        self.procs = []

    def tearDown(self):
        # A test that leaves a process behind is a test that will flake, and
        # one that leaves a pipe open exhausts descriptors long before it says
        # anything useful. Both are the failure this suite exists to catch, so
        # neither is tolerated in the suite itself.
        for row in D.live_jobs(self.db):
            jobs.cancel(self.db, row["id"])
        for p in self.procs:
            for f in (p.stdin, p.stdout, p.stderr):
                if f and not f.closed:
                    f.close()
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)

    def track(self, proc):
        self.procs.append(proc)
        return proc

    def scenario(self, name):
        return os.path.join(HERE, "scenarios", name)


class TestEgress(Base):
    def test_allowlisted_host_passes(self):
        self.assertEqual(trust.check("https://api.coingecko.com/x",
                                     ["api.coingecko.com"]), "api.coingecko.com")

    def test_wildcard_is_subdomains_only(self):
        trust.check("https://api.coingecko.com/x", ["*.coingecko.com"])
        with self.assertRaises(trust.EgressDenied):
            trust.check("https://coingecko.com/x", ["*.coingecko.com"])

    def test_host_not_on_the_list_is_denied(self):
        with self.assertRaises(trust.EgressDenied):
            trust.check("https://evil.example/x", ["api.coingecko.com"])

    def test_no_declared_hosts_denies_everything(self):
        with self.assertRaises(trust.EgressDenied):
            trust.check("https://api.coingecko.com/x", [])

    def test_private_address_denied_by_default(self):
        with self.assertRaises(trust.EgressDenied):
            trust.check("http://192.168.1.179/x", ["192.168.1.179"])

    def test_private_address_allowed_when_the_job_was_granted_it(self):
        # "why is the printer not answering" — granted from the user's request,
        # never from anything the agent read.
        self.assertEqual(
            trust.check("http://192.168.1.179/x", ["192.168.1.179"],
                        allow_private=True), "192.168.1.179")

    def test_the_grant_does_not_bypass_the_allowlist(self):
        with self.assertRaises(trust.EgressDenied):
            trust.check("http://192.168.1.55/x", ["192.168.1.179"],
                        allow_private=True)

    def test_non_http_scheme_denied(self):
        with self.assertRaises(trust.EgressDenied):
            trust.check("file:///etc/shadow", ["*"])


class TestCancel(Base):
    def test_group_kill_reaches_a_grandchild_and_logs_no_false_escape(self):
        jid, proc = jobs.spawn(
            self.db, "agent", "stubborn", "s1",
            [sys.executable, FAKE, "--script", self.scenario("stubborn.json")],
            stdin_text=RUN)
        self.track(proc)
        proc.stdout.readline()                      # it has started
        pgid = D.get_job(self.db, jid)["pgid"]
        self.assertEqual(len(jobs._group_members(pgid)), 2)   # it and its grandchild

        jobs.cancel(self.db, jid)

        self.assertEqual(jobs._group_members(pgid), [])
        self.assertEqual(D.get_job(self.db, jid)["state"], "cancelled")
        escapes = [r["line"] for r in D.tail_log(self.db, jid) if "escaped" in r["line"]]
        self.assertEqual(escapes, [], "a reaped zombie must not be reported as an escape")


class TestBackpressure(Base):
    def test_fan_out_cap_queues_rather_than_refusing(self):
        aid, _p = jobs.spawn(self.db, "agent", "parent", "s1",
                            [sys.executable, "-c", "import time;time.sleep(30)"])
        self.track(_p)
        spawned = queued = 0
        for i in range(6):
            try:
                jobs.spawn(self.db, "tool", "t%d" % i, "s1",
                           [sys.executable, "-c", "import time;time.sleep(30)"],
                           parent_job=aid)
                spawned += 1
            except jobs.Backpressure:
                queued += 1
        self.assertEqual(spawned, jobs.LIMITS["concurrent_tool_jobs_per_agent"])
        self.assertEqual(queued, 2)
        jobs.cancel(self.db, aid)

    def test_cancelling_a_parent_cancels_its_children(self):
        aid, _p = jobs.spawn(self.db, "agent", "parent", "s1",
                            [sys.executable, "-c", "import time;time.sleep(30)"])
        self.track(_p)
        cid, _p2 = jobs.spawn(self.db, "tool", "child", "s1",
                            [sys.executable, "-c", "import time;time.sleep(30)"],
                            parent_job=aid)
        self.track(_p2)
        jobs.cancel(self.db, aid)
        self.assertEqual(D.get_job(self.db, cid)["state"], "cancelled")


class TestRecovery(Base):
    def test_running_jobs_become_orphaned_at_startup(self):
        jid, _p3 = jobs.spawn(self.db, "agent", "survivor", "s1",
                            [sys.executable, "-c", "import time;time.sleep(30)"])
        self.track(_p3)
        again = D.open_db(os.path.join(self.tmp, "jobs.db"))
        self.assertEqual(jobs.recover(again), [jid])
        row = D.get_job(again, jid)
        self.assertEqual((row["state"], row["error_kind"]), ("failed", "orphaned"))
        jobs.cancel(again, jid)


class TestProtocol(Base):
    def test_capability_probe_is_one_line(self):
        out = subprocess.run([sys.executable, FAKE, "--capabilities"],
                             capture_output=True, text=True, timeout=10).stdout
        msg = json.loads(out.strip())
        self.assertEqual((msg["v"], msg["type"]), (1, "capabilities"))

    def test_two_tools_outstanding_answered_out_of_order(self):
        p = self.track(subprocess.Popen(
            [sys.executable, FAKE, "--script", self.scenario("two-tools.json")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1))
        p.stdin.write(RUN); p.stdin.flush()
        seen = []
        for line in p.stdout:
            m = json.loads(line); seen.append(m["type"])
            if m["type"] == "tool" and m["id"] == "t2":
                for tid in ("t2", "t1"):            # deliberately reversed
                    p.stdin.write(json.dumps(
                        {"v": 1, "type": "tool_result", "id": tid,
                         "ok": True, "value": {}}) + "\n")
                p.stdin.flush()
            if m["type"] in ("result", "failed"):
                break
        p.wait(timeout=10)
        self.assertEqual(seen[-1], "result")
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
