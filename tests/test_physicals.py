"""Offline tests for /api/physicals/add (Sheriff/Zane, 19 Oct 2026).

Apollo puts a matter set to Physical on the workbook's "Physicals" tab: the
matter, its jurisdiction and its settlement date, under the last row. A matter
already on the tab is left alone; the team's columns are never written.
No network: the FakeGraph from test_sheet_guard. Matter numbers are invented.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_sheet_guard import tracker, FakeGraph  # noqa: E402


class Sheet:
    def __init__(self, values):
        self.values = values
        self.formulas = [list(r) for r in values]
        self.r1c1 = [list(r) for r in values]


HEAD = ["Settlement Date", "Jurisdiction", "Settlement Date ", "Transfer ", "Adjustments", "Agent ",
        "Bank + shortfall", "Stamp Duty ", "Sign Off", "Reason for Physical "]


def tab():
    return [[""] * 10, HEAD,
            ["90001 PHYSICAL", "QLD ", "29-Oct-26", "Drafted", "", "Dye Durham to book", "", "", "", ""],
            ["90002 PHYSICAL", "QLD ", "2-Nov-26", "ZF to draft", "", "", "", "", "", ""]]


class Graph(FakeGraph):
    def update_excel_range(self, d, i, name, addr, values):
        self.writes.append((name, addr, values))


class PhysicalsTest(unittest.TestCase):
    def setUp(self):
        self.g = Graph({"12 October - 16 October": Sheet([[""]]), "Physicals": Sheet(tab())})
        tracker.graph_client = self.g
        self.c = tracker.app.test_client()

    def post(self, **body):
        r = self.c.post("/api/physicals/add", json=body)
        return r.status_code, r.get_json()

    def test_adds_under_the_last_row(self):
        code, j = self.post(matterNumber="90003", state="nsw", settlementDate="2026-12-11")
        self.assertEqual(code, 200)
        self.assertEqual(j["row"], 5)
        self.assertEqual(self.g.writes, [("Physicals", "A5", [["90003 PHYSICAL"]]), ("Physicals", "B5", [["NSW"]]),
                                         ("Physicals", "C5", [["2026-12-11"]])])

    def test_already_there_is_left_alone(self):
        code, j = self.post(matterNumber="90001", state="QLD", settlementDate="2026-10-29")
        self.assertEqual((code, j.get("already"), j.get("row")), (200, True, 3))
        self.assertEqual(self.g.writes, [])

    def test_no_date_no_date_cell(self):
        _, j = self.post(matterNumber="90004", state="QLD")
        self.assertEqual(sorted(j["cells"]), ["A", "B"])

    def test_dry_run_writes_nothing(self):
        _, j = self.post(matterNumber="90005", state="QLD", settlementDate="2026-11-01", dry=True)
        self.assertEqual((j["dry"], j["row"]), (True, 5))
        self.assertEqual(self.g.writes, [])

    def test_no_tab(self):
        self.g.tabs.pop("Physicals")
        code, j = self.post(matterNumber="90006", state="QLD")
        self.assertEqual((code, j["code"]), (404, "no_tab"))

    def test_bad_matter(self):
        code, _ = self.post(matterNumber="abc")
        self.assertEqual(code, 400)

    def test_a_longer_number_is_not_a_match(self):
        _, j = self.post(matterNumber="9000", state="QLD", dry=True)
        self.assertFalse(j.get("already"))


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.g = Graph({"Physicals": Sheet(tab())})
        tracker.graph_client = self.g
        self.c = tracker.app.test_client()

    def post(self, **body):
        r = self.c.post("/api/physicals/update", json=body)
        return r.status_code, r.get_json()

    def test_note_goes_above_the_teams_text(self):
        _, j = self.post(matterNumber="90001", state="QLD", notes=[{"column": "transfer", "text": "Transfer sent 19/10/2026 by Zane (From apollo)"}])
        self.assertEqual(self.g.writes, [("Physicals", "D3", [["Transfer sent 19/10/2026 by Zane (From apollo)\nDrafted"]])])
        self.assertEqual(len(j["written"]), 1)

    def test_a_note_already_there_is_not_written_twice(self):
        self.g.tabs["Physicals"].values[2][3] = "Transfer sent 19/10/2026 by Zane (From apollo)\nDrafted"
        _, j = self.post(matterNumber="90001", notes=[{"column": "Transfer", "text": "Transfer sent 19/10/2026 by Zane (From apollo)"}])
        self.assertEqual((self.g.writes, len(j["already"])), ([], 1))

    def test_settlement_date_overwritten(self):
        self.post(matterNumber="90002", settlementDate="2026-11-09")
        self.assertEqual(self.g.writes, [("Physicals", "C4", [["2026-11-09"]])])

    def test_reason_and_two_notes_same_column(self):
        self.post(matterNumber="90002", reason="Bank not on PEXA",
                  notes=[{"column": "bank + shortfall", "text": "Bank booked 1"}, {"column": "bank + shortfall", "text": "Shortfall account: Yes 2"}])
        self.assertEqual(sorted(self.g.writes), [("Physicals", "G4", [["Shortfall account: Yes 2\nBank booked 1"]]),
                                                 ("Physicals", "J4", [["Bank not on PEXA"]])])

    def test_not_on_the_tab_is_added_with_its_notes(self):
        _, j = self.post(matterNumber="90009", state="NSW", settlementDate="2026-12-01", notes=[{"column": "sign off", "text": "Signed off"}])
        self.assertTrue(j["added"])
        self.assertEqual(sorted(a for _, a, _ in self.g.writes), ["A5", "B5", "C5", "I5"])

    def test_unknown_column_is_skipped_not_fatal(self):
        _, j = self.post(matterNumber="90001", notes=[{"column": "nope", "text": "x"}])
        self.assertEqual((self.g.writes, len(j["skipped"])), ([], 1))

    def test_dry(self):
        _, j = self.post(matterNumber="90001", notes=[{"column": "agent", "text": "Agent booked"}], dry=True)
        self.assertEqual((self.g.writes, list(j["cells"])), ([], ["F3"]))


if __name__ == "__main__":
    unittest.main()
