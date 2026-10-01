"""Offline tests for the Apollo sheet-write fixes (2026-10-01).

  - the formula guard (notes, Possession, Responsible never write a formula cell)
  - Responsible: Apollo's `people` list makes "Nate" (and anyone later) Apollo's own
  - the retired "Push to Spreadsheet" endpoints answer 410
  - /api/repair-formula: token guard, dry run by default, R1C1 copy from a healthy row

No network, no workbook: the Graph client is replaced with an in-memory fake that
records every write. Matter numbers and names are invented.

Run from the repo root:  python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Never pick up the developer's real .env, never start a job, never touch the real DB.
import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
os.environ["TRACKER_ROLE"] = "offline-test"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["SHAREPOINT_EXCEL_URL"] = "https://example.invalid/workbook"
os.environ["FIREBASE_WORKSPACE_TOKEN"] = "test-token"
os.environ.pop("APOLLO_NOTE_TOKEN", None)

import app as tracker  # noqa: E402

TTB_R1C1 = '=IF(ISNUMBER(MATCH(LEFT(TRIM(RC[-8]),5),TTB!C[-2],0)),"YES","NO")'


def ttb_a1(row):
    return f'=IF(ISNUMBER(MATCH(LEFT(TRIM(A{row}),5),TTB!G:G,0)),"YES","NO")'


HEADER = ["", "Settlement Date", "Jurisdiction", "Possession", "Responsible", "Adjustments",
          "SD", "JM Bank Notes", "TTB Check", "Draft FSO Sent", "Sign Off"]


class FakeSheet:
    """One weekly tab: rows of (values, formulas, r1c1), starting at A1."""

    def __init__(self, rows):
        self.values = [list(r[0]) for r in rows]
        self.formulas = [list(r[1]) for r in rows]
        self.r1c1 = [list(r[2]) for r in rows]
        self.fill = {}
        self.font = {}


def matter_row(colA, row_no, ttb="formula", possession="", responsible="", fso="", width=len(HEADER)):
    """A matter row. ttb='formula' gives the team's live formula; anything else is frozen text."""
    v = [""] * width
    v[0], v[3], v[4], v[9] = colA, possession, responsible, fso
    f = list(v)
    r = list(v)
    if ttb == "formula":
        v[8] = "YES"
        f[8] = ttb_a1(row_no)
        r[8] = TTB_R1C1
    else:
        v[8] = f[8] = r[8] = ttb
    return v, f, r


def build_tab(rows_spec):
    """rows_spec: list of dicts for matter rows; the header is row 1."""
    rows = [(HEADER, HEADER, HEADER)]
    for i, spec in enumerate(rows_spec):
        rows.append(matter_row(row_no=i + 2, **spec))
    return FakeSheet(rows)


class FakeGraph:
    def __init__(self, tabs):
        self.tabs = tabs          # name -> FakeSheet
        self.writes = []          # every write, in order

    # reads
    def resolve_sharing_url(self, url):
        return "drive", "item"

    def get_excel_worksheets(self, d, i):
        return list(self.tabs)

    def _addr(self, name, sh):
        return f"'{name}'!A1:{tracker._col_letter(len(sh.values[0]) - 1)}{len(sh.values)}"

    def get_excel_used_range(self, d, i, name):
        sh = self.tabs[name]
        return [list(r) for r in sh.values], self._addr(name, sh)

    def get_excel_used_range_with_formulas(self, d, i, name, r1c1=False):
        sh = self.tabs[name]
        return {"values": [list(r) for r in sh.values], "formulas": [list(r) for r in sh.formulas],
                "formulasR1C1": [list(r) for r in sh.r1c1] if r1c1 else [],
                "address": self._addr(name, sh)}

    @staticmethod
    def _rc(cell):
        import re
        m = re.fullmatch(r"([A-Z]+)(\d+)", cell)
        col = 0
        for ch in m.group(1):
            col = col * 26 + (ord(ch) - 64)
        return int(m.group(2)) - 1, col - 1

    def get_excel_cell(self, d, i, name, cell):
        sh = self.tabs[name]
        r, c = self._rc(cell)
        return {"value": sh.values[r][c], "formula": sh.formulas[r][c],
                "formulaR1C1": sh.r1c1[r][c], "address": f"'{name}'!{cell}"}

    def get_excel_cell_fill(self, d, i, name, cell):
        return self.tabs[name].fill.get(cell, "")   # Graph answers "" for no fill

    def get_excel_cell_font_color(self, d, i, name, cell):
        return self.tabs[name].font.get(cell, "#000000")

    # writes
    def update_excel_cell(self, d, i, name, cell, value, fill=None, font=None):
        self.writes.append(("cell", name, cell, value))
        sh = self.tabs[name]
        r, c = self._rc(cell)
        sh.values[r][c] = sh.formulas[r][c] = sh.r1c1[r][c] = value
        if fill:
            sh.fill[cell] = fill
        return {}

    def update_excel_range(self, d, i, name, addr, block):
        self.writes.append(("range", name, addr, block))
        return {}

    def set_excel_cell_fill(self, d, i, name, cell, fill):
        self.writes.append(("fill", name, cell, fill))
        self.tabs[name].fill[cell] = fill

    def set_excel_cell_font_color(self, d, i, name, cell, color):
        self.writes.append(("font", name, cell, color))
        self.tabs[name].font[cell] = color

    def clear_excel_cell_fill(self, d, i, name, cell):
        self.writes.append(("clearfill", name, cell))
        self.tabs[name].fill.pop(cell, None)

    def set_excel_cell_formula_r1c1(self, d, i, name, cell, f):
        self.writes.append(("formulaR1C1", name, cell, f))
        sh = self.tabs[name]
        r, c = self._rc(cell)
        sh.r1c1[r][c] = f
        sh.formulas[r][c] = tracker._r1c1_to_a1(f, r + 1, c + 1)
        sh.values[r][c] = "YES"
        return {}

    def insert_excel_column(self, *a, **k):
        raise AssertionError("nothing may insert a column")


def install(tabs):
    fake = FakeGraph(tabs)
    tracker.graph_client = fake
    return fake


class FormulaGuardNotes(unittest.TestCase):
    def test_ttb_note_refused_on_formula_cell(self):
        fake = install({"5 Oct - 9 Oct": build_tab([{"colA": "90001 PURCHASE"}])})
        out = tracker._push_sheet_note("90001", "TTB 01/10/2026: $1.00 held in trust (From apollo)", kind="ttb")
        self.assertFalse(out["success"])
        self.assertEqual(out["error"], "formula cell — not written")
        self.assertEqual(out["code"], "formula_cell")
        self.assertEqual(out["refused"], ["5 Oct - 9 Oct!I2"])
        self.assertEqual(fake.writes, [])

    def test_note_into_plain_cell_still_prepends(self):
        fake = install({"5 Oct - 9 Oct": build_tab([{"colA": "90002 SALE", "fso": "Agreed 29/9 TR"}])})
        out = tracker._push_sheet_note("90002", "FSO sent (From apollo)", kind="fso")
        self.assertTrue(out["success"], out)
        self.assertIsNone(out["code"])
        self.assertEqual(fake.writes[0][:3], ("cell", "5 Oct - 9 Oct", "J2"))
        self.assertEqual(fake.writes[0][3], "FSO sent (From apollo)\nAgreed 29/9 TR")

    def test_frozen_text_cell_is_not_a_formula(self):
        # A damaged TTB cell (already text) is not protected by the guard — the
        # Apollo side stops sending ttb notes instead (SHEET_TTB_NOTE_ENABLED).
        fake = install({"5 Oct - 9 Oct": build_tab([{"colA": "90003 PURCHASE", "ttb": "old\nYES"}])})
        out = tracker._push_sheet_note("90003", "TTB note", kind="ttb")
        self.assertTrue(out["success"])
        self.assertEqual(len(fake.writes), 1)

    def test_written_on_one_tab_refused_on_another(self):
        fake = install({
            "28 Sep - 2 Oct": build_tab([{"colA": "90004 PURCHASE"}]),
            "5 Oct - 9 Oct": build_tab([{"colA": "90004 PURCHASE", "ttb": ""}]),
        })
        out = tracker._push_sheet_note("90004", "TTB note", kind="ttb")
        self.assertTrue(out["success"])
        self.assertEqual(out["updated"], ["5 Oct - 9 Oct!I2"])
        self.assertEqual(out["refused"], ["28 Sep - 2 Oct!I2"])
        self.assertIn("28 Sep - 2 Oct!I2: formula cell — not written", out["errors"])
        self.assertEqual([w[1] for w in fake.writes], ["5 Oct - 9 Oct"])

    def test_not_found_is_still_not_found(self):
        install({"5 Oct - 9 Oct": build_tab([{"colA": "90005 PURCHASE"}])})
        out = tracker._push_sheet_note("90099", "x", kind="fso")
        self.assertEqual(out["error"], "matter not found on any weekly tab")
        self.assertIsNone(out["code"])

    def test_adj_note_endpoint_answers_409(self):
        install({"5 Oct - 9 Oct": build_tab([{"colA": "90001 PURCHASE"}])})
        r = tracker.app.test_client().post("/api/adj-note", json={"matterNumber": "90001", "note": "n", "kind": "ttb"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "formula_cell")


class FormulaGuardSyncs(unittest.TestCase):
    def tab_with_formula_in(self, col_idx, a1):
        sh = build_tab([{"colA": "90010 PURCHASE"}, {"colA": "90011 SALE"}])
        sh.formulas[1][col_idx] = a1
        sh.values[1][col_idx] = "computed"
        return sh

    def test_possession_skips_formula_cell(self):
        fake = install({"5 Oct - 9 Oct": self.tab_with_formula_in(3, "=Z2")})
        tracker._fetch_possession_map = lambda: {"90010": "Vacant", "90011": "TENANTED"}
        out = tracker._sync_possession()
        tab = out["tabs"][0]
        self.assertEqual(tab["formula_cells_not_written"], ["D2"])
        self.assertEqual(tab["ranges"], ["D3"])
        self.assertEqual([w[2] for w in fake.writes if w[0] == "range"], ["D3"])

    def test_responsible_skips_formula_cell(self):
        fake = install({"5 Oct - 9 Oct": self.tab_with_formula_in(4, "=Z2")})
        tracker._fetch_responsible_map = lambda: (
            {"90010": {"value": "Zane", "fill": "#156082", "font": "#FFFFFF"},
             "90011": {"value": "Thomas", "fill": "#A02B93", "font": "#FFFFFF"}}, [])
        out = tracker._sync_responsible()
        tab = out["tabs"][0]
        self.assertEqual(tab["formula_cells_not_written"], ["E2"])
        self.assertEqual(tab["ranges"], ["E3"])
        self.assertNotIn("E2", [w[2] for w in fake.writes])


class ResponsiblePeople(unittest.TestCase):
    def setUp(self):
        self.fake = install({"5 Oct - 9 Oct": build_tab([
            {"colA": "90020 TRANSFER", "responsible": "Nate"},     # moved to Sheriff in Apollo
            {"colA": "90021 PURCHASE", "responsible": "ask JM first"},  # a human note
            {"colA": "90022 SALE", "responsible": "Pat"},          # a future person Apollo names
        ])})
        self.mapping = {
            "90020": {"value": "Sheriff", "fill": "#98FC04", "font": "#000000"},
            "90021": {"value": "Zane", "fill": "#156082", "font": "#FFFFFF"},
            "90022": {"value": "Thomas", "fill": "#A02B93", "font": "#FFFFFF"},
        }

    def test_people_list_makes_nate_and_pat_rewritable(self):
        tracker._fetch_responsible_map = lambda: (self.mapping, ["Zane", "Thomas", "Sheriff", "Nate", "Pat"])
        out = tracker._sync_responsible(dry_run=True)
        tab = out["tabs"][0]
        self.assertEqual(tab["ranges"], ["E2", "E4"])
        self.assertEqual(tab["left_alone_human"], ["E3=ask JM first"])
        self.assertIn("nate", out["apollo_names"])
        self.assertIn("pat", out["apollo_names"])
        self.assertEqual(self.fake.writes, [])   # dry run writes nothing

    def test_without_people_a_name_apollo_sends_is_still_its_own(self):
        # An Apollo that predates `people`: "Nate" is still recognised when Apollo
        # is sending it for any matter; "Pat" (named nowhere) stays a human note.
        mapping = dict(self.mapping, **{"90030": {"value": "Nate", "fill": "#FFC000", "font": "#000000"}})
        tracker._fetch_responsible_map = lambda: (mapping, [])
        out = tracker._sync_responsible(dry_run=True)
        tab = out["tabs"][0]
        self.assertEqual(tab["ranges"], ["E2"])
        self.assertEqual(sorted(tab["left_alone_human"]), ["E3=ask JM first", "E4=Pat"])

    def test_old_fixed_set_alone_would_have_frozen_nate(self):
        self.assertNotIn("nate", tracker.RESPONSIBLE_OURS)
        self.assertIn("nate", tracker._responsible_ours({}, ["Nate"]))
        self.assertIn("nate", tracker._responsible_ours({"1": {"value": " Nate "}}, None))


class RetiredPush(unittest.TestCase):
    def test_endpoints_answer_410(self):
        fake = install({"5 Oct - 9 Oct": build_tab([{"colA": "90040 PURCHASE"}])})
        c = tracker.app.test_client()
        for path, body in (("/api/push-to-excel", {"ids": [1]}), ("/api/recover-missing-pushes", {"days": 3}),
                           ("/api/auto-push", {})):
            r = c.post(path, json=body)
            self.assertEqual(r.status_code, 410, path)
            self.assertTrue(r.get_json()["retired"], path)
            self.assertIn("column G", r.get_json()["error"])
        self.assertFalse(tracker._do_push_to_excel([1, 2])["success"])
        self.assertEqual(fake.writes, [])

    def test_dashboard_has_no_push_button(self):
        for rel in ("templates/dashboard.html", "static/app.js"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("pushToExcel(", text, rel)
            self.assertNotIn("pushSingleToExcel(", text, rel)
            self.assertNotIn(">Push to Spreadsheet<", text, rel)


class R1C1Preview(unittest.TestCase):
    def test_ttb_formula(self):
        self.assertEqual(tracker._r1c1_to_a1(TTB_R1C1, 23, 9), ttb_a1(23))

    def test_absolute_and_ranges(self):
        f = tracker._r1c1_to_a1
        self.assertEqual(f("=R5C3+RC", 10, 2), "=$C$5+B10")
        self.assertEqual(f("=SUM(R[-2]C:R[-1]C)", 10, 2), "=SUM(B8:B9)")
        self.assertEqual(f("=COUNTIF(C7,RC[-1])", 4, 3), "=COUNTIF($G:$G,B4)")
        self.assertEqual(f("=SUM(C[-1]:C[1])", 4, 3), "=SUM(B:D)")
        self.assertEqual(f("=VLOOKUP(RC1,'PEXA CHECK RC'!C1:C6,6,FALSE)", 12, 12),
                         "=VLOOKUP($A12,'PEXA CHECK RC'!$A:$F,6,FALSE)")
        self.assertEqual(f('=IF(RC[-1]="RC","R1C1","")', 2, 2), '=IF(A2="RC","R1C1","")')


class WeekTabMatch(unittest.TestCase):
    SHEETS = ["PEXA CHECK 28 September - 2 October", "28 September - 2 October",
              "5 October - 9 October", "12 October - 16 October", "TTB", "Master Data"]

    def test_short_names_find_the_long_tab(self):
        f = tracker._find_weekly_tab
        self.assertEqual(f(self.SHEETS, "28 Sep-2 Oct"), "28 September - 2 October")
        self.assertEqual(f(self.SHEETS, "5-9 Oct"), "5 October - 9 October")
        self.assertEqual(f(self.SHEETS, " 5 october - 9 october "), "5 October - 9 October")
        self.assertIsNone(f(self.SHEETS, "19-23 Oct"))
        self.assertIsNone(f(self.SHEETS, "TTB check"))
        self.assertIsNone(f(self.SHEETS + ["5 Oct - 9 Oct (2)"], "5-9 Oct"))   # two match: refuse


class RepairFormula(unittest.TestCase):
    TAB = "28 Sep - 2 Oct"

    def setUp(self):
        self.fake = install({self.TAB: build_tab([
            {"colA": "90050 PURCHASE"},
            {"colA": "90051 PURCHASE", "ttb": "TTB 01/10/2026: $5.00 held in trust (From apollo)\nYES"},
            {"colA": "90052 SALE"},
            {"colA": "90053 PURCHASE"},
        ])})
        self.fake.tabs[self.TAB].fill["I3"] = "#DDEBF7"
        self.c = tracker.app.test_client()

    def post(self, body):
        return self.c.post("/api/repair-formula", json=body)

    def test_token_required(self):
        self.assertEqual(self.post({"tab": self.TAB, "matter": "90051"}).status_code, 401)
        self.assertEqual(self.post({"tab": self.TAB, "matter": "90051", "token": "nope"}).status_code, 401)

    def test_dry_by_default(self):
        r = self.post({"tab": self.TAB, "matter": "90051", "token": "test-token"})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["dry"])
        self.assertEqual(d["cell"], "I3")
        self.assertEqual(d["writeR1C1"], TTB_R1C1)
        self.assertEqual(d["expectedA1"], ttb_a1(3))
        self.assertIn(d["donorCell"], ("I2", "I4"))
        self.assertEqual(d["healthyRows"], 3)
        self.assertIn("held in trust", d["willDiscard"])
        self.assertEqual(self.fake.writes, [])
        # an odd "dry" value is still a dry run
        self.post({"tab": self.TAB, "matter": "90051", "token": "test-token", "dry": "maybe"})
        self.assertEqual(self.fake.writes, [])

    def test_real_run_writes_r1c1_and_reads_back(self):
        r = self.post({"tab": self.TAB, "matter": "90051", "token": "test-token", "dry": False})
        d = r.get_json()
        self.assertEqual(r.status_code, 200, d)
        self.assertTrue(d["verified"])
        self.assertEqual(self.fake.writes[0], ("formulaR1C1", self.TAB, "I3", TTB_R1C1))
        self.assertIn(("clearfill", self.TAB, "I3"), self.fake.writes)   # Apollo's blue-grey off
        self.assertEqual(d["after"]["formula"], ttb_a1(3))
        # only that one cell was touched
        self.assertEqual({w[2] for w in self.fake.writes}, {"I3"})

    def test_someone_elses_fill_stays(self):
        self.fake.tabs[self.TAB].fill["I3"] = "#FF0000"
        d = self.post({"tab": self.TAB, "matter": "90051", "token": "test-token", "dry": False}).get_json()
        self.assertTrue(d["verified"])
        self.assertFalse(d["look"]["clearApolloFill"])
        self.assertNotIn(("clearfill", self.TAB, "I3"), self.fake.writes)

    def test_already_a_formula_is_left_alone(self):
        d = self.post({"tab": self.TAB, "matter": "90050", "token": "test-token", "dry": False}).get_json()
        self.assertTrue(d["alreadyFormula"])
        self.assertEqual(self.fake.writes, [])

    def test_refusals(self):
        tok = "test-token"
        self.assertEqual(self.post({"tab": "No Such Tab", "matter": "90051", "token": tok}).status_code, 404)
        self.assertEqual(self.post({"tab": self.TAB, "matter": "90999", "token": tok}).status_code, 404)
        self.assertEqual(self.post({"tab": self.TAB, "matter": "9a", "token": tok}).status_code, 400)
        # healthy rows disagree -> refuse
        sh = self.fake.tabs[self.TAB]
        sh.r1c1[1][8] = "=1"
        sh.r1c1[3][8] = "=2"
        r = self.post({"tab": self.TAB, "matter": "90051", "token": tok, "dry": False})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.fake.writes, [])

    def test_unanimous_dry_run_says_so(self):
        d = self.post({"tab": self.TAB, "matter": "90051", "token": "test-token"}).get_json()
        self.assertEqual(d["healthyRows"], 3)
        self.assertEqual(d["rowsWithThisFormula"], d["healthyRows"])

    def test_a_majority_is_not_enough(self):
        # 2 healthy rows agree, 1 points at a stale 'TTB (2)' tab: refuse, dry AND real.
        sh = self.fake.tabs[self.TAB]
        stale = TTB_R1C1.replace("TTB!", "'TTB (2)'!")
        sh.r1c1[4][8] = stale
        sh.formulas[4][8] = ttb_a1(5).replace("TTB!", "'TTB (2)'!")
        tok = "test-token"
        for dry in (True, False):
            r = self.post({"tab": self.TAB, "matter": "90051", "token": tok, "dry": dry})
            d = r.get_json()
            self.assertEqual(r.status_code, 409, d)
            self.assertFalse(d["success"])
            self.assertNotIn("writeR1C1", d)
            self.assertEqual(d["healthyRows"], 3)
            self.assertEqual(sorted((v["r1c1"], v["rows"], tuple(v["cells"])) for v in d["variants"]),
                             sorted([(TTB_R1C1, 2, ("I2", "I4")), (stale, 1, ("I5",))]))
        self.assertEqual(self.fake.writes, [])

    def test_matter_twice_on_the_tab_is_refused(self):
        sh = self.fake.tabs[self.TAB]
        sh.values[4][0] = sh.formulas[4][0] = sh.r1c1[4][0] = "90051 PURCHASE (2)"
        r = self.post({"tab": self.TAB, "matter": "90051", "token": "test-token", "dry": False})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.fake.writes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
