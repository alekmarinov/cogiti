"""Slice 2: cogiti's half of the agent protocol, and the tool broker.

Runs a real HTTP server on loopback, a real fake adapter, and real tool jobs.
Nothing is mocked: the point is the seam.
"""
import asyncio, json, os, sys, tempfile, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import db as D, jobs                                # noqa: E402
from cogiti.adapters import agent                               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = [sys.executable, os.path.join(HERE, "fakes", "agent.py")]
SRV = None
PORT = None


def setUpModule():
    """One server for the module, on a port the OS picks.

    Per-class setUp bound a fixed port four times over and three of them
    failed with EADDRINUSE. A fixed port is also a collision with whatever
    else happens to be running on a development machine.
    """
    global SRV, PORT
    SRV = HTTPServer(("127.0.0.1", 0), Handler)
    PORT = SRV.server_address[1]
    threading.Thread(target=SRV.serve_forever, daemon=True).start()


def tearDownModule():
    SRV.shutdown()
    SRV.server_close()          # or the socket is reported unclosed


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://192.168.1.1/secret")
            self.end_headers()
            return
        body = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = D.open_db(os.path.join(self.tmp, "jobs.db"))

    def scenario(self, n):
        """Scenario files carry {PORT}; the real one is only known at runtime."""
        src = open(os.path.join(HERE, "scenarios", n)).read()
        dst = os.path.join(self.tmp, n)
        with open(dst, "w") as f:
            f.write(src.replace("{PORT}", str(PORT)))
        return FAKE + ["--script", dst]

    async def escalate(self, scenario, tools, allow_private=True):
        run = agent.AgentRun(self.db, self.scenario(scenario), "s1")
        return await run.run({"text": "a question"}, tools,
                             {"wall_ms": 30000}, allow_private=allow_private), run


class TestBroker(Base):
    async def test_two_tools_fetched_in_parallel(self):
        tools = [{"name": "http", "hosts": ["127.0.0.1"]}]
        res, run = await self.escalate("fetch-two.json", tools)
        self.assertEqual(res["type"], "result")
        self.assertEqual(res["say"], "both fetched")
        kids = D.children(self.db, run.job_id)
        self.assertEqual(len(kids), 2, "each brokered call is its own job")
        self.assertTrue(all(k["kind"] == "tool" for k in kids))
        self.assertTrue(all(k["pgid"] for k in kids), "each in its own group")
        self.assertEqual(D.get_job(self.db, run.job_id)["state"], "done")

    async def test_a_host_off_the_allowlist_is_refused_not_fetched(self):
        tools = [{"name": "http", "hosts": ["127.0.0.1"]}]
        res, run = await self.escalate("forbidden-host.json", tools)
        self.assertEqual(res["type"], "result")
        self.assertEqual(D.children(self.db, run.job_id), [],
                         "a denied call must never become a tool job")
        log = " ".join(r["line"] for r in D.tail_log(self.db, run.job_id))
        self.assertIn("egress-denied", log, "denial is a security event, logged")

    async def test_an_ungranted_tool_has_no_channel(self):
        tools = [{"name": "http", "hosts": ["127.0.0.1"]}]
        res, run = await self.escalate("ungranted-tool.json", tools)
        self.assertEqual(res["type"], "result")
        self.assertEqual(D.children(self.db, run.job_id), [])
        log = " ".join(r["line"] for r in D.tail_log(self.db, run.job_id))
        self.assertIn("ungranted-tool", log)


class TestRedirect(Base):
    async def test_a_redirect_is_returned_not_followed(self):
        """The egress check ran against the first url. Following a 302 to a
        private address would land somewhere it never checked."""
        from cogiti.tools import http
        r = http.fetch("http://127.0.0.1:%d/redirect" % PORT)
        self.assertEqual(r["status"], 302)
        self.assertEqual(r.get("redirected_to"), "http://192.168.1.1/secret")
        self.assertNotIn("secret", r.get("body", ""))


class TestCapabilities(Base):
    async def test_probe_and_requirement(self):
        caps = await agent.capabilities(FAKE)
        self.assertTrue(caps["tools"])
        agent.require(caps, ["tools", "questions"])
        with self.assertRaises(agent.ProtocolError):
            agent.require(caps, ["telepathy"])


class TestProtocolFailures(Base):
    async def test_no_terminal_event_is_a_protocol_failure(self):
        os.makedirs(os.path.join(HERE, "scenarios"), exist_ok=True)
        p = os.path.join(HERE, "scenarios", "silent.json")
        with open(p, "w") as f:
            json.dump({"steps": [{"emit": {"type": "thought", "text": "…"}}]}, f)
        res, run = await self.escalate("silent.json", [])
        self.assertEqual(res["type"], "failed")
        self.assertEqual(res["kind"], "protocol")
        self.assertEqual(D.get_job(self.db, run.job_id)["state"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
