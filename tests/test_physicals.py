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


if __name__ == "__main__":
    unittest.main()
