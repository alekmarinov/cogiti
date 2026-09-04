"""Pictures on the screen, and the limits on where they come from.

A product photograph is the one thing an escalation cannot produce for
itself, and the URL is chosen from a page somebody else wrote — which makes
it the least trustworthy input in the system and the only place cogiti
downloads something a model named.
"""

import os, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cogiti import images, present, trust                      # noqa: E402


class TestWhereAPictureMayComeFrom(unittest.TestCase):
    """An allowlist cannot work here — nobody can enumerate the image CDNs of
    everything a person might ask about — so the rule is the one that
    actually protects something: https, and never this network."""

    def refused(self, url):
        with self.assertRaises(trust.EgressDenied) as e:
            trust.public_only(url)
        return str(e.exception)

    def test_plain_http_is_refused(self):
        self.assertIn("only https", self.refused("http://example.com/a.png"))

    def test_a_private_address_is_refused(self):
        self.assertIn("non-global", self.refused("https://192.168.1.1/a.png"))

    def test_a_name_resolving_inwards_is_refused(self):
        """The shape of it: a host that looks ordinary and points at the
        router. Checking the name and not the address is checking nothing."""
        self.assertIn("not a global", self.refused("https://localhost/a.png"))

    def test_an_ordinary_url_passes(self):
        self.assertEqual(trust.public_only("https://example.com/a.png"),
                         "example.com")


class TestWhatCountsAsAPicture(unittest.TestCase):
    def test_the_file_has_to_agree_with_the_header(self):
        """A served image/png that is HTML is a page that wanted to be
        fetched, not a picture."""
        self.assertIsNone(images._sniff(b"<!doctype html><html>"))
        self.assertEqual(images._sniff(b"\x89PNG\r\n\x1a\n...."), "png")
        self.assertEqual(images._sniff(b"\xff\xd8\xff\xe0...."), "jpg")

    def test_old_pictures_are_swept(self):
        """Four megabytes at a time, and nothing refers to one once its card
        is gone."""
        d = tempfile.mkdtemp()
        old = os.path.join(d, "old.jpg")
        new = os.path.join(d, "new.jpg")
        for p in (old, new):
            open(p, "wb").close()
        os.utime(old, (0, 0))
        images.sweep(d, keep_s=60)
        self.assertEqual(os.listdir(d), ["new.jpg"])


class FakeAdapter:
    def __init__(self):
        self.ops = []

    def send(self, **op):
        self.ops.append(op)
        return True

    def supports(self, *a):
        return True


class TestPanels(unittest.TestCase):
    def setUp(self):
        self.a = FakeAdapter()
        self.p = present.Presenter(self.a)

    def test_each_panel_is_a_group_the_adapter_lines_up(self):
        """One group per thing; the adapter gives groups on the stage a
        common width. No coordinate is sent, here or anywhere."""
        self.p.panels([{"title": "A", "image": "/tmp/a.jpg", "lines": "x\ny"},
                       {"title": "B", "lines": "z"}])
        groups = [o for o in self.a.ops if o.get("kind") == "group"]
        self.assertEqual(len(groups), 2)
        self.assertEqual([c["kind"] for c in groups[0]["children"]],
                         ["image", "text", "text"])
        self.assertNotIn("x", str(groups[0].get("region", "")))
        for g in groups:
            self.assertNotIn("width", g)
            self.assertNotIn("x", g)

    def test_a_panel_without_a_picture_still_draws(self):
        """One unreachable photograph must not lose the specifications too:
        the words were the answer and the picture was the illustration."""
        self.p.panels([{"title": "A", "lines": "still here"}])
        g = [o for o in self.a.ops if o.get("kind") == "group"][0]
        self.assertEqual([c["kind"] for c in g["children"]], ["text", "text"])

    def test_the_answer_does_not_destroy_what_this_turn_drew(self):
        """The regression this exists for. The model searched, found a
        product, fetched the photograph, drew the panel and said "the specs
        are on the screen" — and the answer arriving a second later
        destroyed them, because from the presenter a panel drawn ten seconds
        ago looks like an answer to the previous question."""
        self.p.panels([{"title": "A", "lines": "specs"}])
        self.a.ops.clear()
        self.p.result({"type": "result", "say": "it is on the screen"})
        destroyed = [o["id"] for o in self.a.ops if o.get("op") == "destroy"]
        self.assertEqual([d for d in destroyed if "panels" in d], [])

    def test_the_next_answer_does_clear_them(self):
        """They belong to their turn and not to the pinboard."""
        self.p.panels([{"title": "A", "lines": "specs"}])
        self.p.result({"type": "result", "say": "one"})
        self.a.ops.clear()
        self.p.result({"type": "result", "say": "two"})
        destroyed = [o["id"] for o in self.a.ops if o.get("op") == "destroy"]
        self.assertTrue([d for d in destroyed if "panels" in d],
                        "a panel outlived the conversation")


class TestThinkingOutLoud(unittest.TestCase):
    """The reasoning stays up while the work goes on behind it."""

    def setUp(self):
        self.a = FakeAdapter()
        self.p = present.Presenter(self.a)

    def ids_destroyed(self):
        return [o["id"] for o in self.a.ops if o.get("op") == "destroy"]

    def test_the_holding_line_does_not_wipe_the_reasoning(self):
        """Twenty thoughts were drawn and cleared five seconds in, so a
        minute of searching showed a blank screen and a head. The moment
        somebody most needs to see the work is the moment they have just been
        told to wait."""
        self.p.thought("looking it up")
        self.a.ops.clear()
        self.p.result({"type": "result", "say": "I'm still working on that.",
                       "pending": True})
        self.assertNotIn(present.THOUGHTS, self.ids_destroyed())

    def test_the_real_answer_does_wipe_it(self):
        self.p.thought("looking it up")
        self.p.result({"type": "result", "say": "still working",
                       "pending": True})
        self.a.ops.clear()
        self.p.result({"type": "result", "say": "here it is"})
        self.assertIn(present.THOUGHTS, self.ids_destroyed())


if __name__ == "__main__":
    unittest.main(verbosity=2)
