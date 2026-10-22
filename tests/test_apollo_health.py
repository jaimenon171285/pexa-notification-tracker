"""Apollo health check (Jai, 23 Oct 2026): DOWN after two bad runs, one email; UP once; never raises."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import apollo_health as H  # noqa: E402


class FakeResp:
    def __init__(self, code):
        self.status_code = code


class FakeSession:
    def __init__(self, codes):
        self.codes = codes
        self.calls = []

    def options(self, url, timeout=None, headers=None):
        self.calls.append((url, headers))
        c = self.codes.get(url.rsplit("/", 1)[1], 204)
        if c is None:
            raise H.requests.ConnectionError("down")
        return FakeResp(c)


class FakeGraph:
    def __init__(self):
        self.sent = []

    def send_email(self, to, subject, text, from_mailbox=None, cc_emails=None, body_html=None):
        self.sent.append({"to": to, "subject": subject, "from": from_mailbox, "cc": cc_emails, "html": body_html})


ALL_500 = {n: 500 for n in H.CHECKS}


def fresh():
    return {"bad_runs": 0, "down": False, "since": None, "last": None}


class TestApolloHealth(unittest.TestCase):
    def test_healthy_is_quiet(self):
        g, s = FakeGraph(), fresh()
        for i in range(3):
            self.assertIsNone(H.tick(g, f"t{i}", FakeSession({}), s))
        self.assertEqual(g.sent, [])

    def test_preflight_only_no_side_effects(self):
        sess = FakeSession({})
        H.run_checks(sess)
        self.assertEqual(len(sess.calls), len(H.CHECKS))
        self.assertTrue(all(h["Access-Control-Request-Method"] == "POST" for _, h in sess.calls))

    def test_down_after_two_bad_runs_one_email_cc_teams(self):
        g, s = FakeGraph(), fresh()
        self.assertIsNone(H.tick(g, "t1", FakeSession(ALL_500), s))
        self.assertEqual(H.tick(g, "t2", FakeSession(ALL_500), s), "down")
        self.assertIsNone(H.tick(g, "t3", FakeSession(ALL_500), s))
        self.assertEqual(len(g.sent), 1)
        m = g.sent[0]
        self.assertIn("DOWN", m["subject"])
        self.assertEqual(m["cc"], ["teams@legalworld.com.au"])
        self.assertEqual(m["to"], ["jai@legalworld.com.au"])
        self.assertIn("recreate-functions.js", m["html"])

    def test_back_up_once(self):
        g, s = FakeGraph(), fresh()
        H.tick(g, "t1", FakeSession(ALL_500), s)
        H.tick(g, "t2", FakeSession(ALL_500), s)
        self.assertEqual(H.tick(g, "t3", FakeSession({}), s), "up")
        self.assertIsNone(H.tick(g, "t4", FakeSession({}), s))
        self.assertEqual([m["subject"][:1] for m in g.sent], ["🔴", "✅"])
        self.assertIn("t2", g.sent[1]["html"])

    def test_one_function_failing_is_not_an_outage(self):
        g, s = FakeGraph(), fresh()
        one = {H.CHECKS[0]: 500}
        for i in range(4):
            H.tick(g, f"t{i}", FakeSession(one), s)
        self.assertEqual(g.sent, [])

    def test_no_answer_counts_as_failed(self):
        run = H.run_checks(FakeSession({n: None for n in H.CHECKS}))
        self.assertTrue(run["bad"])
        self.assertTrue(all(d.startswith("no answer") for d in run["results"].values()))

    def test_a_401_or_400_is_healthy(self):
        run = H.run_checks(FakeSession({n: 401 for n in H.CHECKS}))
        self.assertFalse(run["bad"])

    def test_email_failure_never_raises(self):
        class Boom(FakeGraph):
            def send_email(self, *a, **k):
                raise RuntimeError("graph down")
        s = fresh()
        H.tick(Boom(), "t1", FakeSession(ALL_500), s)
        self.assertIsNone(H.tick(Boom(), "t2", FakeSession(ALL_500), s))


if __name__ == "__main__":
    unittest.main()
