"""Offline tests for the Adjustments-column work of 22 Oct 2026.

  - the two new Apollo kinds: 'sasprepared' (light blue, black text) and
    'adjagreed' (the team's done-green), both in the Adjustments column
  - /api/adj-note/replace-line: one wrong-dated Apollo line put right — only
    that exact line, the cell's colour never written, a formula cell refused,
    dry by default, token-guarded

The cells below are built from 75689's real case (no names): the 26 Oct tab
got "Adj served on O/S 17/10/2026" from a row-move re-send that should have
said 08/10/2026; on 68384 the right line was already in the cell and the
re-send added a wrong-dated twin under it.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# The fake workbook and the offline environment (no .env, no jobs, no DB).
from test_sheet_guard import FakeGraph, build_tab, tracker  # noqa: E402

TAB = "26 October - 30 October"
OLD = "19 October - 23 October"
WRONG = "Adj served on O/S 17/10/2026 by Pat Tester (From apollo)"
RIGHT = "Adj served on O/S 08/10/2026 by Pat Tester (From apollo)"
TEAM = "agreed with o/s - JM"


class WritingGraph(FakeGraph):
    """FakeGraph whose range write lands in the sheet, so a re-read sees it."""

    def update_excel_range(self, d, i, name, addr, block):
        self.writes.append(("range", name, addr, block))
        sh = self.tabs[name]
        r, c = self._rc(addr)
        sh.values[r][c] = sh.formulas[r][c] = sh.r1c1[r][c] = block[0][0]
        return {}


def install(tabs):
    fake = WritingGraph(tabs)
    tracker.graph_client = fake
    return fake


def tab_with_adjustments(cells):
    """A weekly tab: one row per (colA, Adjustments cell text)."""
    sh = build_tab([{"colA": a} for a, _ in cells])
    for i, (_, text) in enumerate(cells):
        sh.values[i + 1][5] = sh.formulas[i + 1][5] = sh.r1c1[i + 1][5] = text
    return sh


class NewKinds(unittest.TestCase):
    def test_sas_prepared_is_light_blue_black_text_in_adjustments(self):
        spec = tracker.APOLLO_KINDS["sasprepared"]
        self.assertEqual(spec["fill"], "#ADD8E6")
        self.assertEqual(spec.get("font", tracker.APOLLO_FONT), "#000000")
        self.assertEqual(spec["headers"], tracker.APOLLO_KINDS["adjustments"]["headers"])

    def test_adj_agreed_is_the_done_green_in_adjustments(self):
        spec = tracker.APOLLO_KINDS["adjagreed"]
        self.assertEqual(spec["fill"], "#C6EFCE")
        self.assertEqual(spec["headers"], tracker.APOLLO_KINDS["adjustments"]["headers"])

    def test_apollo_camel_case_kinds_are_accepted(self):
        install({TAB: tab_with_adjustments([("90601 PURCHASE", "")])})
        for kind in ("sasPrepared", "adjAgreed"):
            out = tracker._push_sheet_note("90601", f"{kind} 22/10/2026 by Pat Tester (From apollo)", kind=kind)
            self.assertTrue(out["success"], out)
        fills = tracker.graph_client.tabs[TAB].fill
        self.assertEqual(fills["F2"], "#C6EFCE")   # the last one written paints the cell


class ReplaceLineIn(unittest.TestCase):
    def test_replaced_where_it_stood(self):
        cell = "\n".join([RIGHT.replace("Adj served", "Searches all received"), WRONG, TEAM])
        new, action = tracker._replace_line_in(cell, WRONG, RIGHT)
        self.assertEqual(action, "replace")
        self.assertEqual(new.split("\n"), [RIGHT.replace("Adj served", "Searches all received"), RIGHT, TEAM])

    def test_twin_of_the_right_line_is_taken_out(self):
        cell = "\n".join([TEAM, RIGHT, WRONG])
        new, action = tracker._replace_line_in(cell, WRONG, RIGHT)
        self.assertEqual(action, "remove_duplicate")
        self.assertEqual(new, "\n".join([TEAM, RIGHT]))

    def test_other_lines_kept_byte_for_byte(self):
        cell = "  " + TEAM + "  \n" + WRONG + "\r\n\nlast line "
        new, _ = tracker._replace_line_in(cell, WRONG, RIGHT)
        self.assertEqual(new, "  " + TEAM + "  \n" + RIGHT + "\n\nlast line ")

    def test_part_of_a_line_is_never_matched(self):
        for cell in (WRONG + " - checked", "see " + WRONG, WRONG.replace("17/10", "18/10"), "", None):
            self.assertEqual(tracker._replace_line_in(cell, WRONG, RIGHT), (None, None), cell)


class ReplaceLineEndpoint(unittest.TestCase):
    def setUp(self):
        self.fake = install({
            OLD: tab_with_adjustments([("90600 PURCHASE", RIGHT)]),
            TAB: tab_with_adjustments([("90600 PURCHASE", "\n".join([TEAM, RIGHT.replace("Adj served", "Searches all received"), WRONG])),
                                       ("906001 PURCHASE", WRONG)]),   # another matter that starts the same
        })
        self.fake.tabs[TAB].fill["F2"] = "#C6EFCE"   # the team's own green
        self.c = tracker.app.test_client()
        self.body = {"matterNumber": "90600", "kind": "adjustments", "from": WRONG, "to": RIGHT,
                     "tabs": [TAB], "token": "test-token"}

    def post(self, **over):
        return self.c.post("/api/adj-note/replace-line", json=dict(self.body, **over))

    def test_token_required(self):
        self.assertEqual(self.post(token=None).status_code, 401)
        self.assertEqual(self.post(token="nope").status_code, 401)

    def test_dry_by_default_says_what_it_would_do(self):
        r = self.post()
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertTrue(d["dry"])
        self.assertEqual([(x["cell"], x["action"]) for x in d["results"]], [(f"{TAB}!F2", "replace")])
        self.assertEqual(d["results"][0]["after"].split("\n")[-1], RIGHT)
        self.assertEqual(d["changed"], 0)
        self.assertEqual(self.fake.writes, [])
        self.post(dry="maybe")
        self.assertEqual(self.fake.writes, [])

    def test_real_run_changes_only_that_line_and_never_the_colour(self):
        d = self.post(dry=False).get_json()
        self.assertEqual(d["changed"], 1, d)
        self.assertEqual(self.fake.writes, [("range", TAB, "F2", [["\n".join([TEAM, RIGHT.replace("Adj served", "Searches all received"), RIGHT])]])])
        self.assertEqual(self.fake.tabs[TAB].fill["F2"], "#C6EFCE")
        self.assertFalse(any(w[0] in ("fill", "font", "clearfill", "cell") for w in self.fake.writes))
        # 906001 is another matter: untouched; the old tab was not asked for
        self.assertEqual(self.fake.tabs[TAB].values[2][5], WRONG)
        # a second run finds nothing to do
        again = self.post(dry=False).get_json()
        self.assertEqual([x["action"] for x in again["results"]], ["not_found"])
        self.assertEqual(len(self.fake.writes), 1)

    def test_the_right_line_already_there_the_twin_goes(self):
        self.fake.tabs[TAB].values[1][5] = "\n".join([TEAM, RIGHT, WRONG])
        d = self.post(dry=False).get_json()
        self.assertEqual([x["action"] for x in d["results"]], ["removed_duplicate"])
        self.assertEqual(self.fake.tabs[TAB].values[1][5], "\n".join([TEAM, RIGHT]))

    def test_formula_cell_refused(self):
        sh = self.fake.tabs[TAB]
        sh.formulas[1][5] = "=Z2"
        d = self.post(dry=False).get_json()
        self.assertEqual([x["action"] for x in d["results"]], ["formula_cell"])
        self.assertEqual(self.fake.writes, [])

    def test_every_weekly_tab_when_none_named(self):
        d = self.post(tabs=None).get_json()
        self.assertEqual(sorted((x["cell"], x["action"]) for x in d["results"]),
                         sorted([(f"{OLD}!F2", "not_found"), (f"{TAB}!F2", "replace")]))

    def test_not_on_those_tabs_is_404_not_found(self):
        r = self.post(matterNumber="90999")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["code"], "not_found")
        r = self.post(tabs=["2 November - 6 November"])
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["missingTabs"], ["2 november - 6 november"])

    def test_refusals(self):
        self.assertEqual(self.post(kind="nope").status_code, 400)
        self.assertEqual(self.post(matterNumber="9a").status_code, 400)
        self.assertEqual(self.post(to=WRONG).status_code, 400)                        # nothing to change
        self.assertEqual(self.post(**{"from": TEAM}).status_code, 400)                # the team's text: never
        self.assertEqual(self.post(to="Adj served on O/S 08/10/2026").status_code, 400)
        self.assertEqual(self.post(to=RIGHT + "\nmore").status_code, 400)
        self.assertEqual(self.fake.writes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
