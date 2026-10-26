"""Unit tests for validator.py. Run from the demo folder: python3 -m unittest -v"""
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import validator as v  # noqa: E402

SPEC_CSV = """row_id,feature,event,trigger,match,param,type,required,allowed
A1,Open,app_open,,,open_from,string,yes,launcher|notification
B1,Buy,purchase,,,price,number,yes,0.01..
B1,Buy,purchase,,,qty,int,no,1..10
C1,Rate,rating,,,stars,int,yes,1..5
D1,Click,button_click,,,button_name,string,yes,
D2,Subscribe,button_click,,button_name=subscribe,button_name,string,yes,subscribe
D2,Subscribe,button_click,,button_name=subscribe,plan,string,yes,monthly|yearly
E1,Flag,toggle,,,enabled,bool,yes,
"""


def logline(payload, time="10:00:00.000", tag="TrackingEvent"):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return f"10-26 {time}  100  100 I {tag}: {body}\n"


class ValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.spec_path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as f:
            f.write(SPEC_CSV)
        cls.spec = v.load_spec(cls.spec_path)

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.spec_path)

    def run_lines(self, *payloads):
        return v.validate(self.spec, [logline(p) for p in payloads])

    def status(self, result, row_id):
        row = next(r for r in self.spec if r.row_id == row_id)
        return result.row_status(row)

    def findings(self, result, row_id):
        return [(f.level, f.param) for o in result.seen.get(row_id, []) for f in o.findings]

    # --- the five rule types -------------------------------------------------

    def test_valid_event_passes(self):
        r = self.run_lines({"event": "app_open", "params": {"open_from": "launcher"}})
        self.assertEqual(self.status(r, "A1"), v.PASS)

    def test_missing_required_param_fails(self):
        r = self.run_lines({"event": "app_open", "params": {}})
        self.assertEqual(self.status(r, "A1"), v.FAIL)
        self.assertIn((v.FAIL, "open_from"), self.findings(r, "A1"))

    def test_missing_optional_param_is_fine(self):
        r = self.run_lines({"event": "purchase", "params": {"price": 1.5}})
        self.assertEqual(self.status(r, "B1"), v.PASS)

    def test_extra_param_warns(self):
        r = self.run_lines({"event": "app_open", "params": {"open_from": "launcher", "debug": 1}})
        self.assertEqual(self.status(r, "A1"), v.WARN)
        self.assertIn((v.WARN, "debug"), self.findings(r, "A1"))

    def test_wrong_type_fails(self):
        r = self.run_lines({"event": "purchase", "params": {"price": "4.99"}})
        self.assertEqual(self.status(r, "B1"), v.FAIL)

    def test_bool_is_not_accepted_as_int_or_number(self):
        r = self.run_lines({"event": "rating", "params": {"stars": True}})
        self.assertEqual(self.status(r, "C1"), v.FAIL)

    def test_int_is_accepted_as_number(self):
        r = self.run_lines({"event": "purchase", "params": {"price": 5}})
        self.assertEqual(self.status(r, "B1"), v.PASS)

    def test_bool_type(self):
        ok = self.run_lines({"event": "toggle", "params": {"enabled": False}})
        bad = self.run_lines({"event": "toggle", "params": {"enabled": "false"}})
        self.assertEqual(self.status(ok, "E1"), v.PASS)
        self.assertEqual(self.status(bad, "E1"), v.FAIL)

    def test_value_not_in_allowed_list_fails(self):
        r = self.run_lines({"event": "app_open", "params": {"open_from": "default"}})
        self.assertEqual(self.status(r, "A1"), v.FAIL)

    def test_range_bounds(self):
        low = self.run_lines({"event": "rating", "params": {"stars": 0}})
        high = self.run_lines({"event": "rating", "params": {"stars": 6}})
        edge = self.run_lines({"event": "rating", "params": {"stars": 5}})
        self.assertEqual(self.status(low, "C1"), v.FAIL)
        self.assertEqual(self.status(high, "C1"), v.FAIL)
        self.assertEqual(self.status(edge, "C1"), v.PASS)

    def test_open_ended_range(self):
        r = self.run_lines({"event": "purchase", "params": {"price": 0}})
        self.assertEqual(self.status(r, "B1"), v.FAIL)

    # --- row matching and session coverage -----------------------------------

    def test_row_not_fired_is_not_tested(self):
        r = self.run_lines({"event": "app_open", "params": {"open_from": "launcher"}})
        self.assertEqual(self.status(r, "C1"), v.NOT_TESTED)

    def test_most_specific_row_wins(self):
        r = self.run_lines(
            {"event": "button_click", "params": {"button_name": "subscribe", "plan": "yearly"}},
            {"event": "button_click", "params": {"button_name": "close"}},
        )
        self.assertEqual(len(r.seen["D2"]), 1)
        self.assertEqual(len(r.seen["D1"]), 1)
        self.assertEqual(self.status(r, "D2"), v.PASS)

    def test_one_failing_occurrence_fails_the_row(self):
        r = self.run_lines(
            {"event": "app_open", "params": {"open_from": "launcher"}},
            {"event": "app_open", "params": {"open_from": "default"}},
        )
        self.assertEqual(self.status(r, "A1"), v.FAIL)
        self.assertEqual([o.status for o in r.seen["A1"]], [v.PASS, v.FAIL])

    def test_unknown_event_is_reported(self):
        r = self.run_lines({"event": "debug_ping", "params": {}})
        self.assertEqual([u["event"] for u in r.unknown], ["debug_ping"])

    # --- log parsing ---------------------------------------------------------

    def test_malformed_payload_is_counted(self):
        r = v.validate(self.spec, [logline('{"event":"app_open","params":{')])
        self.assertEqual(len(r.parse_errors), 1)
        self.assertEqual(r.seen, {})

    def test_other_tags_and_noise_are_ignored(self):
        lines = [
            logline({"event": "app_open", "params": {"open_from": "launcher"}}, tag="SomethingElse"),
            "--------- beginning of main\n",
            "not a logcat line\n",
        ]
        r = v.validate(self.spec, lines)
        self.assertEqual((r.seen, r.unknown, r.parse_errors), ({}, [], []))

    def test_stdin_style_iterable(self):
        stream = io.StringIO(logline({"event": "app_open", "params": {"open_from": "notification"}}))
        r = v.validate(self.spec, stream)
        self.assertEqual(self.status(r, "A1"), v.PASS)

    # --- the published sample ------------------------------------------------

    def test_sample_session_summary(self):
        spec = v.load_spec(os.path.join(ROOT, "spec", "sample_tracking_spec.csv"))
        with open(os.path.join(ROOT, "logs", "sample_session.log"), encoding="utf-8") as f:
            r = v.validate(spec, f)
        self.assertEqual(r.summary(), {v.PASS: 3, v.WARN: 1, v.FAIL: 4, v.NOT_TESTED: 2})
        self.assertEqual(len(r.unknown), 1)
        self.assertEqual(len(r.parse_errors), 1)

    def test_exit_code_signals_failures(self):
        out = io.StringIO()
        stdout, sys.stdout = sys.stdout, out
        try:
            code = v.main(["--spec", os.path.join(ROOT, "spec", "sample_tracking_spec.csv"),
                           "--log", os.path.join(ROOT, "logs", "sample_session.log")])
        finally:
            sys.stdout = stdout
        self.assertEqual(code, 1)
        self.assertIn("4 fail", out.getvalue())


if __name__ == "__main__":
    unittest.main()
