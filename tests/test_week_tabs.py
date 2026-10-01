"""Offline tests for field-map FIX 5 and FIX 8 and late notes (2026-10-01).

  - Apollo writes ONLY to weekly tabs: a tab whose whole name is a week's date
    range. Never 'PEXA CHECK …', Import, TTB, Master Data, invoices, MWSD, a copy
    like '5 Oct - 9 Oct (2)', or whatever helper tab the next monthly file adds.
    This replaced the hand-kept skip list in the note write, both syncs, peek,
    sheet-headers and the recolour endpoint.
  - /api/week-matters: the matter numbers on each weekly tab (Apollo's replay
    tool asks it which matters a late note can land on).
  - late=True: a late note goes UNDER the cell's text and the cell keeps its
    colour (an empty cell is written and painted as usual).
  - FIX 8: SHAREPOINT_ITEM_ID + SHAREPOINT_DRIVE_ID are enough on their own —
    an empty SHAREPOINT_EXCEL_URL no longer switches everything off.

No network, no workbook: the in-memory FakeGraph from test_sheet_guard, which
records every write; this one also records every tab it READS. Matter numbers
are invented.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import os
import unittest

import test_sheet_guard as G   # sets the offline environment and imports app

tracker = G.tracker

# The October 2026 file's real tab names, plus the kinds of helper tab a skip
# list never knew about.
OCTOBER_TABS = ["MWSD", "Master Data", "28 September - 2 October", "PEXA CHECK 28September-2October",
                "5 October - 9 October", "PEXA CHECK 5October-9October", "12 October - 16 October",
                "PEXA CHECK 12October-16October", "invoices", "TTB", "Import"]


class ReadLoggingGraph(G.FakeGraph):
    """FakeGraph that also records which tabs were read, and reads a range."""

    def __init__(self, tabs):
        super().__init__(tabs)
        self.reads = []

    def get_excel_used_range(self, d, i, name):
        self.reads.append(name)
        return super().get_excel_used_range(d, i, name)

    def get_excel_used_range_with_formulas(self, d, i, name, r1c1=False):
        self.reads.append(name)
        return super().get_excel_used_range_with_formulas(d, i, name, r1c1)

    def get_excel_range(self, d, i, name, addr):
        self.reads.append(name)
        assert addr == "A1:A600", addr
        return [[r[0]] for r in self.tabs[name].values]


def install(tabs):
    fake = ReadLoggingGraph(tabs)
    tracker.graph_client = fake
    return fake


def tab(*rows):
    return G.build_tab(list(rows))


class IsWeekTab(unittest.TestCase):
    def test_week_names_are_weekly_tabs(self):
        for name in ("28 September - 2 October", "5 October - 9 October", "12 October - 16 October",
                     "5-9 Oct", "28 Sep-2 Oct", " 5 Oct - 9 Oct ", "28 Sept - 2 Oct", "29 June - 3 July",
                     "5 Oct - 9 Oct", "5 October - 9 October 2026", "28 SEPTEMBER - 2 OCTOBER"):
            self.assertTrue(tracker._is_week_tab(name), name)

    def test_everything_else_is_not(self):
        for name in ("PEXA CHECK 28September-2October", "PEXA CHECK 5 October - 9 October",
                     "pexa check 5-9 Oct", "TTB", "TTB (2)", "Import", "Master Data", "MWSD", "invoices",
                     "Physicals", "Sheet1", "Sheet2", "5 October - 9 October (2)", "Physicals 5-9 Oct",
                     "TTB 5-9 Oct", "5 Octopus - 9 Octopus", "5 - 9", "", None, "Notes"):
            self.assertFalse(tracker._is_week_tab(name), name)

    def test_the_october_file(self):
        self.assertEqual(tracker._week_tabs(OCTOBER_TABS),
                         ["28 September - 2 October", "5 October - 9 October", "12 October - 16 October"])

    def test_the_skip_list_is_gone(self):
        self.assertFalse(hasattr(tracker, "_PUSH_SKIP_SHEETS"))
        with open(os.path.join(G.ROOT, "app.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("skip_sheets", src)
        self.assertNotIn('"ttb (2)"', src)


def workbook_with_helpers(colA="90101 PURCHASE"):
    """A week tab plus helper tabs whose column A ALSO matches the matter and
    which carry the same headers — exactly what the old skip list let through."""
    tabs = {name: tab({"colA": colA}) for name in OCTOBER_TABS}
    return tabs


class NotesOnlyOnWeekTabs(unittest.TestCase):
    def test_note_lands_on_week_tabs_only_and_helpers_are_never_read(self):
        fake = install(workbook_with_helpers())
        out = tracker._push_sheet_note("90101", "FSO sent (From apollo)", kind="fso")
        self.assertTrue(out["success"], out)
        self.assertEqual(out["updated"], ["28 September - 2 October!J2", "5 October - 9 October!J2",
                                          "12 October - 16 October!J2"])
        self.assertEqual({w[1] for w in fake.writes}, {"28 September - 2 October", "5 October - 9 October",
                                                       "12 October - 16 October"})
        for helper in ("TTB", "Import", "Master Data", "MWSD", "invoices", "PEXA CHECK 5October-9October"):
            self.assertNotIn(helper, fake.reads, helper)

    def test_not_on_a_week_tab_even_if_on_a_helper_tab(self):
        fake = install({"TTB": tab({"colA": "90102 PURCHASE"}), "Import": tab({"colA": "90102 PURCHASE"}),
                        "5 October - 9 October": tab({"colA": "90103 SALE"})})
        out = tracker._push_sheet_note("90102", "x", kind="fso")
        self.assertFalse(out["success"])
        self.assertEqual(out["error"], "matter not found on any weekly tab")
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.reads, ["5 October - 9 October"])

    def test_a_copied_week_tab_is_not_written(self):
        fake = install({"5 Oct - 9 Oct": tab({"colA": "90104 PURCHASE"}),
                        "5 Oct - 9 Oct (2)": tab({"colA": "90104 PURCHASE"})})
        out = tracker._push_sheet_note("90104", "x", kind="fso")
        self.assertEqual(out["updated"], ["5 Oct - 9 Oct!J2"])
        self.assertNotIn("5 Oct - 9 Oct (2)", fake.reads)


class SyncsOnlyOnWeekTabs(unittest.TestCase):
    def test_possession(self):
        fake = install(workbook_with_helpers("90110 PURCHASE"))
        tracker._fetch_possession_map = lambda: {"90110": "Vacant"}
        out = tracker._sync_possession()
        self.assertTrue(out["success"], out)
        self.assertEqual([t["sheet"] for t in out["tabs"]],
                         ["28 September - 2 October", "5 October - 9 October", "12 October - 16 October"])
        self.assertEqual({w[1] for w in fake.writes},
                         {"28 September - 2 October", "5 October - 9 October", "12 October - 16 October"})
        self.assertNotIn("TTB", fake.reads)

    def test_responsible(self):
        fake = install(workbook_with_helpers("90111 SALE"))
        tracker._fetch_responsible_map = lambda: ({"90111": {"value": "Zane", "fill": "#156082", "font": "#FFFFFF"}}, ["Zane"])
        out = tracker._sync_responsible(dry_run=True)
        self.assertTrue(out["success"], out)
        self.assertEqual([t["sheet"] for t in out["tabs"]],
                         ["28 September - 2 October", "5 October - 9 October", "12 October - 16 October"])
        for helper in ("TTB", "Import", "Master Data", "invoices"):
            self.assertNotIn(helper, fake.reads)


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.c = tracker.app.test_client()

    def test_sheet_headers_lists_week_tabs_only(self):
        install(workbook_with_helpers())
        d = self.c.get("/api/sheet-headers").get_json()
        self.assertTrue(d["success"])
        self.assertEqual([s["sheet"] for s in d["sheets"]], ["28 September - 2 October", "5 October - 9 October"])

    def test_sheet_headers_fails_when_there_is_no_week_tab(self):
        # The wrong file, or a month not set up: Apollo's health check reads
        # success:false and tells Jai.
        install({"TTB": tab({"colA": "90120 SALE"}), "Import": tab({"colA": "90120 SALE"})})
        r = self.c.get("/api/sheet-headers")
        self.assertEqual(r.status_code, 500)
        self.assertFalse(r.get_json()["success"])
        self.assertIn("no weekly tab", r.get_json()["error"])

    def test_peek_reads_week_tabs_only(self):
        fake = install(workbook_with_helpers("90121 PURCHASE"))
        d = self.c.get("/api/adj-note/peek?matter=90121&column=J").get_json()
        self.assertEqual([r["cell"].split("!")[0] for r in d["rows"]],
                         ["28 September - 2 October", "5 October - 9 October", "12 October - 16 October"])
        self.assertNotIn("TTB", fake.reads)

    def test_recolour_week_tabs_only(self):
        fake = install(workbook_with_helpers("90122 PURCHASE"))
        for name in fake.tabs:
            fake.tabs[name].fill["J2"] = "#A02B93"
        d = self.c.post("/api/adj-note/recolour", json={"matterNumber": "90122", "kind": "fso",
                                                        "onlyIfFill": "#A02B93", "dry": True}).get_json()
        self.assertEqual(sorted(x["cell"] for x in d["recoloured"]),
                         ["12 October - 16 October!J2", "28 September - 2 October!J2", "5 October - 9 October!J2"])

    def test_repair_refuses_a_helper_tab(self):
        install({"TTB": tab({"colA": "90123 PURCHASE"})})
        r = self.c.post("/api/repair-formula", json={"tab": "TTB", "matter": "90123", "token": "test-token"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a weekly tab", r.get_json()["error"])


class WeekMatters(unittest.TestCase):
    def test_numbers_per_week_tab(self):
        tabs = workbook_with_helpers("90130 PURCHASE")
        tabs["5 October - 9 October"] = tab({"colA": "90131 SALE"}, {"colA": "90130 PURCHASE"},
                                            {"colA": "901310 TRANSFER"}, {"colA": ""}, {"colA": "see JM"})
        tabs["5 October - 9 October"].values[3][0] = 90132          # a number typed with no suffix
        fake = install(tabs)
        d = tracker.app.test_client().get("/api/week-matters").get_json()
        self.assertTrue(d["success"])
        self.assertEqual([t["sheet"] for t in d["tabs"]],
                         ["28 September - 2 October", "5 October - 9 October", "12 October - 16 October"])
        self.assertEqual(d["tabs"][1]["matters"], ["90131", "90130", "90132"])
        self.assertEqual(d["matters"], ["90130", "90131", "90132"])
        self.assertNotIn("TTB", fake.reads)
        self.assertEqual(fake.writes, [])


class LateNotes(unittest.TestCase):
    def test_late_note_goes_under_and_keeps_the_colour(self):
        fake = install({"5 October - 9 October": tab({"colA": "90140 PURCHASE", "fso": "FSO sent 30/09 (From apollo)"})})
        out = tracker._push_sheet_note("90140", "FSO sent 21/09 (From apollo)", kind="fso", late=True)
        self.assertTrue(out["success"], out)
        self.assertTrue(out["late"])
        self.assertEqual(fake.writes, [("cell", "5 October - 9 October", "J2",
                                        "FSO sent 30/09 (From apollo)\nFSO sent 21/09 (From apollo)")])
        self.assertNotIn("J2", fake.tabs["5 October - 9 October"].fill)   # colour untouched

    def test_late_note_into_an_empty_cell_is_painted(self):
        fake = install({"5 October - 9 October": tab({"colA": "90141 PURCHASE"})})
        tracker._push_sheet_note("90141", "FSO sent 21/09 (From apollo)", kind="fso", late=True)
        self.assertEqual(fake.writes[0][3], "FSO sent 21/09 (From apollo)")
        self.assertEqual(fake.tabs["5 October - 9 October"].fill["J2"], tracker.APOLLO_KINDS["fso"]["fill"])

    def test_live_note_still_prepends_and_paints(self):
        fake = install({"5 October - 9 October": tab({"colA": "90142 PURCHASE", "fso": "older"})})
        out = tracker._push_sheet_note("90142", "newer", kind="fso")
        self.assertFalse(out["late"])
        self.assertEqual(fake.writes[0][3], "newer\nolder")
        self.assertIn("J2", fake.tabs["5 October - 9 October"].fill)

    def test_endpoint_takes_late_true_only(self):
        c = tracker.app.test_client()
        fake = install({"5 October - 9 October": tab({"colA": "90143 PURCHASE", "fso": "older"})})
        c.post("/api/adj-note", json={"matterNumber": "90143", "note": "late", "kind": "fso", "late": True})
        self.assertEqual(fake.writes[0][3], "older\nlate")
        fake = install({"5 October - 9 October": tab({"colA": "90144 PURCHASE", "fso": "older"})})
        c.post("/api/adj-note", json={"matterNumber": "90144", "note": "live", "kind": "fso", "late": "yes"})
        self.assertEqual(fake.writes[0][3], "live\nolder")             # only a real true is late

    def test_formula_guard_still_wins_on_a_late_note(self):
        fake = install({"5 October - 9 October": tab({"colA": "90145 PURCHASE"})})
        out = tracker._push_sheet_note("90145", "TTB late", kind="ttb", late=True)
        self.assertEqual(out["code"], "formula_cell")
        self.assertEqual(fake.writes, [])

    def test_unknown_kind_is_400_not_500(self):
        install({"5 October - 9 October": tab({"colA": "90146 PURCHASE"})})
        r = tracker.app.test_client().post("/api/adj-note", json={"matterNumber": "90146", "note": "n", "kind": "nope"})
        self.assertEqual(r.status_code, 400)


class WorkbookConfig(unittest.TestCase):
    KEYS = ("SHAREPOINT_EXCEL_URL", "SHAREPOINT_ITEM_ID", "SHAREPOINT_DRIVE_ID")

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in self.KEYS}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def set_env(self, **kw):
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ.update(kw)

    def test_ids_alone_are_enough(self):
        self.set_env(SHAREPOINT_ITEM_ID="item", SHAREPOINT_DRIVE_ID="drive")
        self.assertIsNone(tracker._workbook_not_configured())
        fake = install({"5 October - 9 October": tab({"colA": "90150 PURCHASE"})})
        out = tracker._push_sheet_note("90150", "n", kind="fso")
        self.assertTrue(out["success"], out)
        self.assertEqual(len(fake.writes), 1)

    def test_item_without_drive_still_needs_the_link(self):
        self.set_env(SHAREPOINT_ITEM_ID="item")
        self.assertIn("not configured", tracker._workbook_not_configured())
        install({"5 October - 9 October": tab({"colA": "90151 PURCHASE"})})
        self.assertFalse(tracker._push_sheet_note("90151", "n", kind="fso")["success"])

    def test_nothing_set(self):
        self.set_env()
        err = tracker._workbook_not_configured()
        self.assertIn("SHAREPOINT_EXCEL_URL", err)
        install({"5 October - 9 October": tab({"colA": "90152 PURCHASE"})})
        for fn in (lambda: tracker._push_sheet_note("90152", "n", kind="fso"),
                   lambda: tracker._sync_possession(), lambda: tracker._sync_responsible()):
            out = fn()
            self.assertFalse(out["success"])
        r = tracker.app.test_client().get("/api/sheet-headers")
        self.assertEqual(r.status_code, 500)

    def test_link_alone_as_before(self):
        self.set_env(SHAREPOINT_EXCEL_URL="https://example.invalid/workbook")
        self.assertIsNone(tracker._workbook_not_configured())


if __name__ == "__main__":
    unittest.main(verbosity=2)
