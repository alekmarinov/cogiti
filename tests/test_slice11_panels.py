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

    def test_a_site_icon_is_not_a_photograph(self):
        """Asked for four USB sticks, two of the pages' own og:image tags led
        to a 30x30 and a 100x90 — a site icon, offered in the same field and
        with the same confidence as a 2048x1536 product shot."""
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
               + (30).to_bytes(4, "big") + (30).to_bytes(4, "big"))
        self.assertEqual(images._dimensions(png, "png"), (30, 30))
        big = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
               + (1200).to_bytes(4, "big") + (800).to_bytes(4, "big"))
        self.assertEqual(images._dimensions(big, "png"), (1200, 800))
        self.assertLess(30, images.MIN_SIDE)
        self.assertGreater(800, images.MIN_SIDE)

    def test_dimensions_of_something_unreadable_do_not_raise(self):
        """A picture whose header cannot be read is kept, not refused: the
        check is here to catch favicons, not to be a second decoder."""
        self.assertEqual(images._dimensions(b"nonsense", "png"), (0, 0))
        self.assertEqual(images._dimensions(b"\xff\xd8\xff", "jpg"), (0, 0))

    def test_there_is_a_ceiling_on_how_many_are_kept(self):
        """Age alone was not enough: a run of questions inside the hour
        accumulates four megabytes at a time, and the appliance has about
        four gigabytes free."""
        d = tempfile.mkdtemp()
        import time as _t
        for i in range(8):
            p = os.path.join(d, "%d.jpg" % i)
            open(p, "wb").close()
            os.utime(p, (_t.time() - i, _t.time() - i))   # 0 newest
        images.sweep(d, keep_s=3600, keep_n=3)
        self.assertEqual(sorted(os.listdir(d)), ["0.jpg", "1.jpg", "2.jpg"])

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


class TestChoosingAPicture(unittest.TestCase):
    """cogiti reads the markup; the model judges relevance.

    Neither can do the other's half: a page reaches the model as extracted
    text with the img tags stripped, and cogiti has no idea what was asked.
    `og:image` was cogiti guessing at relevance alone, and on a roundup page
    it answers "what represents this page" — the banner — which is why the
    pictures kept arriving irrelevant.
    """

    PAGE = (b'<html><head>'
            b'<meta property="og:image" content="https://x.test/banner.jpg">'
            b'</head><body>'
            b'<img src="/logo.png" alt="site logo">'
            b'<img src="data:image/png;base64,AAAA" alt="inline">'
            b'<img src="https://x.test/chrome.svg" alt="an icon">'
            b'<img src="//cdn.test/SanDisk_Ultra_Flair.jpg?w=1&amp;h=2">'
            b'<img src="/photos/kingston.jpg" alt="Kingston DataTraveler">'
            b'</body></html>')

    def candidates(self):
        import cogiti.images as im
        real = im._read
        im._read = lambda *a, **k: (self.PAGE, "https://x.test/review")
        try:
            return im.candidates("https://x.test/review")
        finally:
            im._read = real

    def test_it_offers_what_the_screen_can_draw(self):
        urls = [c["url"] for c in self.candidates()]
        self.assertIn("https://cdn.test/SanDisk_Ultra_Flair.jpg?w=1&h=2", urls)
        self.assertIn("https://x.test/photos/kingston.jpg", urls)

    def test_entities_are_unescaped(self):
        """`&amp;` is one ampersand. Leaving it made every URL with a query
        string — which is most of a CDN's — a 404 waiting to happen."""
        urls = " ".join(c["url"] for c in self.candidates())
        self.assertIn("w=1&h=2", urls)
        self.assertNotIn("&amp;", urls)

    def test_furniture_and_undrawable_formats_are_left_out(self):
        """The first pass over a real review site returned tracking pixels,
        SVG chrome and a literal SPONSORED_IMAGE_URL before it reached a
        photograph. stb_image cannot draw SVG in any case."""
        urls = " ".join(c["url"] for c in self.candidates())
        for junk in ("logo.png", "chrome.svg", "data:"):
            self.assertNotIn(junk, urls)

    def test_described_pictures_come_first(self):
        """alt is the page's own words for the picture. It is often empty,
        which is why the file name goes back too — SanDisk_Ultra_Flair.jpg
        tells a model everything it needs."""
        cs = self.candidates()
        self.assertTrue(cs[0]["alt"], "an undescribed picture was offered first")


class TestWhatTheToolTellsTheModel(unittest.TestCase):
    def test_it_points_at_find_pictures_first(self):
        """Measured failure: asked for a present for a nine-year-old, it
        called find_pictures, was handed twenty-four photographs, and then
        passed `image_page` for every panel — because the description still
        said to prefer that, written before find_pictures existed. All three
        panels came back with no picture."""
        from cogiti import panels_tool
        d = panels_tool.tool()["description"]
        self.assertIn("call `find_pictures`", d)
        self.assertLess(d.index("find_pictures"), d.index("image_page"),
                        "the fallback is described before the route that works")
        self.assertIn("fallback", d)


class TestAnAnswerThatDrawsNothing(unittest.TestCase):
    """Reported from a device: the confirmation question would not go away.

    A question is drawn as an answer card. `Cancelled.` and `Updating. I'll
    tell you when it's done.` are both spoken-only — the table gives that
    command `present = "none"` — so they draw nothing, and the sweep was being
    told to spare the very id they were never going to redraw. The question
    outlived the thing it was asking about.
    """

    def setUp(self):
        self.a = FakeAdapter()
        self.p = present.Presenter(self.a)

    def drew(self):
        return [o for o in self.a.ops if o.get("op") == "create"]

    def destroyed(self):
        return [o["id"] for o in self.a.ops if o.get("op") == "destroy"]

    def test_a_spoken_only_answer_takes_the_last_card_down(self):
        self.p.result({"type": "result", "say": "Shall I install the "
                                                "available updates?",
                       "show": "Shall I install the available updates?"})
        self.assertTrue(self.drew(), "the question was never drawn")
        self.a.ops.clear()

        # The answer to it: spoken, and presenting nothing.
        self.p.result({"type": "result", "say": "Cancelled."})
        self.assertIn(present.ANSWER, self.destroyed(),
                      "the question stayed on the screen after it was answered")

    def test_a_failure_still_keeps_its_own_card(self):
        """`failed` draws the "couldn't:" line under the answer id, so that
        one must still be spared or it would be destroyed and recreated."""
        self.p.result({"type": "result", "say": "hi", "show": "hi"})
        self.a.ops.clear()
        self.p.result({"type": "failed", "kind": "table", "message": "no"})
        self.assertNotIn(present.ANSWER, self.destroyed(),
                         "the failure line destroyed the card it was drawing")


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


class TestWhatIsOnScreen(unittest.TestCase):
    """So that "tell me more about the first one" has a first one.

    The device drew three products, said their names out loud, and met the
    obvious follow-up with no idea what was being pointed at — because the
    only record of the panels was a set of object ids on the way to a socket.
    """

    def setUp(self):
        self.a = FakeAdapter()
        self.p = present.Presenter(self.a)

    def test_it_reports_them_in_the_order_drawn(self):
        self.p.panels([{"title": "Sketch Pad"}, {"title": "Crystal Kit"},
                       {"title": "Soccer Ball"}])
        self.assertEqual(self.p.on_screen(),
                         ["Sketch Pad", "Crystal Kit", "Soccer Ball"])

    def test_an_empty_stage_has_no_first_one(self):
        self.assertEqual(self.p.on_screen(), [])

    def test_they_survive_the_answer_that_describes_them(self):
        """The answer arrives while they are still up, and is usually the
        sentence naming them."""
        self.p.panels([{"title": "Sketch Pad"}, {"title": "Crystal Kit"}])
        self.p.result({"type": "result", "say": "two ideas"})
        self.assertEqual(len(self.p.on_screen()), 2)

    def test_they_are_forgotten_when_they_leave_the_screen(self):
        """A stale list is worse than none: it would have the device
        confidently describing something nobody can see."""
        self.p.panels([{"title": "Sketch Pad"}, {"title": "Crystal Kit"}])
        self.p.result({"type": "result", "say": "two ideas"})
        self.p.result({"type": "result", "say": "something else entirely"})
        self.assertEqual(self.p.on_screen(), [])


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
