#!/usr/bin/env python3
"""Validate Android tracking events from logcat against a tracking spec.

Public demo with fake data. It re-implements the validation idea behind an
internal QA dashboard; it is not that tool's source code.

Usage:
    python3 validator.py --spec spec/sample_tracking_spec.csv --log logs/sample_session.log
    adb logcat -v threadtime -s TrackingEvent:I | python3 validator.py --spec spec.csv --log -

Standard library only (Python 3.8+).
"""
import argparse
import csv
import html
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

TAG = "TrackingEvent"

# logcat -v threadtime: "MM-DD HH:MM:SS.mmm  PID  TID LEVEL TAG: message"
LOG_LINE = re.compile(
    r"^(?P<date>\d\d-\d\d)\s+(?P<time>\d\d:\d\d:\d\d\.\d{3})\s+\d+\s+\d+\s+"
    r"[VDIWEF]\s+(?P<tag>[^:]+?)\s*:\s(?P<msg>.*)$"
)

PASS, WARN, FAIL, NOT_TESTED = "PASS", "WARN", "FAIL", "NOT TESTED"


# ---------------------------------------------------------------- spec

@dataclass
class ParamRule:
    name: str
    type: str                      # string | int | number | bool
    required: bool
    allowed: Optional[List[str]]   # explicit values, or None
    minimum: Optional[float]       # range bounds for int / number
    maximum: Optional[float]


@dataclass
class SpecRow:
    row_id: str
    feature: str
    event: str
    trigger: str
    match: Dict[str, str]          # discriminator params, e.g. button_name=subscribe
    params: Dict[str, ParamRule] = field(default_factory=dict)


def parse_allowed(text):
    """'a|b|c' -> values; '1..5', '0..', '..10' -> range; '' -> anything."""
    text = (text or "").strip()
    if not text:
        return None, None, None
    if ".." in text:
        low, high = text.split("..", 1)
        return None, float(low) if low else None, float(high) if high else None
    return [v.strip() for v in text.split("|")], None, None


def parse_match(text):
    match = {}
    for part in filter(None, (p.strip() for p in (text or "").split(";"))):
        key, _, value = part.partition("=")
        match[key.strip()] = value.strip()
    return match


def load_spec(path):
    """One CSV line per (row_id, param); lines with the same row_id form one spec row."""
    rows: Dict[str, SpecRow] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for line in csv.DictReader(f):
            row_id = line["row_id"].strip()
            row = rows.get(row_id)
            if row is None:
                row = rows[row_id] = SpecRow(
                    row_id=row_id,
                    feature=line["feature"].strip(),
                    event=line["event"].strip(),
                    trigger=line["trigger"].strip(),
                    match=parse_match(line["match"]),
                )
            allowed, minimum, maximum = parse_allowed(line["allowed"])
            name = line["param"].strip()
            row.params[name] = ParamRule(
                name=name,
                type=line["type"].strip(),
                required=line["required"].strip().lower() in ("yes", "y", "true", "1"),
                allowed=allowed,
                minimum=minimum,
                maximum=maximum,
            )
    return list(rows.values())


# ---------------------------------------------------------------- checks

@dataclass
class Finding:
    level: str      # FAIL or WARN
    param: str
    message: str


@dataclass
class Occurrence:
    line_no: int
    time: str
    raw: str
    params: dict
    findings: List[Finding]

    @property
    def status(self):
        levels = {f.level for f in self.findings}
        return FAIL if FAIL in levels else WARN if WARN in levels else PASS


def type_ok(value, type_name):
    # bool is a subclass of int in Python, so rule it out explicitly
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "bool":
        return isinstance(value, bool)
    raise ValueError(f"unknown type in spec: {type_name}")


def check_params(row: SpecRow, params: dict) -> List[Finding]:
    findings = []
    for rule in row.params.values():
        if rule.name not in params:
            if rule.required:
                findings.append(Finding(FAIL, rule.name, "missing required param"))
            continue
        value = params[rule.name]
        if not type_ok(value, rule.type):
            findings.append(Finding(FAIL, rule.name,
                                    f"expected {rule.type}, got {type(value).__name__} ({json.dumps(value)})"))
            continue
        if rule.allowed is not None and str(value) not in rule.allowed:
            findings.append(Finding(FAIL, rule.name,
                                    f"value {json.dumps(value)} not in allowed list ({'|'.join(rule.allowed)})"))
        if rule.minimum is not None and value < rule.minimum:
            findings.append(Finding(FAIL, rule.name, f"{value} is below min {rule.minimum:g}"))
        if rule.maximum is not None and value > rule.maximum:
            findings.append(Finding(FAIL, rule.name, f"{value} is above max {rule.maximum:g}"))
    for name in params:
        if name not in row.params:
            findings.append(Finding(WARN, name, "param not in spec"))
    return findings


def find_row(spec: List[SpecRow], event: str, params: dict) -> Optional[SpecRow]:
    """Pick the most specific spec row whose event and match params fit."""
    candidates = [
        row for row in spec
        if row.event == event
        and all(str(params.get(k)) == v for k, v in row.match.items())
    ]
    return max(candidates, key=lambda r: len(r.match), default=None)


# ---------------------------------------------------------------- session

@dataclass
class Result:
    spec: List[SpecRow]
    seen: Dict[str, List[Occurrence]] = field(default_factory=dict)
    unknown: List[dict] = field(default_factory=list)       # events with no spec row
    parse_errors: List[dict] = field(default_factory=list)  # tagged lines that are not valid JSON
    lines_read: int = 0

    def row_status(self, row: SpecRow) -> str:
        occurrences = self.seen.get(row.row_id, [])
        if not occurrences:
            return NOT_TESTED
        statuses = {o.status for o in occurrences}
        return FAIL if FAIL in statuses else WARN if WARN in statuses else PASS

    def summary(self):
        counts = {PASS: 0, WARN: 0, FAIL: 0, NOT_TESTED: 0}
        for row in self.spec:
            counts[self.row_status(row)] += 1
        return counts


def validate(spec: List[SpecRow], lines) -> Result:
    result = Result(spec=spec)
    try:
        _consume(result, spec, lines)
    except KeyboardInterrupt:   # Ctrl+C ends a live adb session; keep what was seen so far
        pass
    return result


def _consume(result: Result, spec: List[SpecRow], lines):
    for line_no, raw in enumerate(lines, start=1):
        result.lines_read = line_no
        raw = raw.rstrip("\n")
        m = LOG_LINE.match(raw)
        if not m or m.group("tag").strip() != TAG:
            continue
        time = m.group("time")
        try:
            payload = json.loads(m.group("msg"))
            event, params = payload["event"], payload.get("params", {})
            if not isinstance(params, dict):
                raise ValueError("params is not an object")
        except (ValueError, KeyError, TypeError) as exc:
            result.parse_errors.append({"line_no": line_no, "time": time, "raw": raw, "error": str(exc)})
            continue
        row = find_row(spec, event, params)
        if row is None:
            result.unknown.append({"line_no": line_no, "time": time, "event": event, "params": params, "raw": raw})
            continue
        result.seen.setdefault(row.row_id, []).append(
            Occurrence(line_no, time, raw, params, check_params(row, params)))


# ---------------------------------------------------------------- output

def to_dict(result: Result):
    rows = []
    for row in result.spec:
        occurrences = result.seen.get(row.row_id, [])
        rows.append({
            "row_id": row.row_id,
            "feature": row.feature,
            "event": row.event,
            "trigger": row.trigger,
            "match": row.match,
            "status": result.row_status(row),
            "seen": len(occurrences),
            "occurrences": [{
                "line_no": o.line_no,
                "time": o.time,
                "status": o.status,
                "params": o.params,
                "findings": [f.__dict__ for f in o.findings],
            } for o in occurrences],
        })
    return {
        "summary": result.summary(),
        "rows": rows,
        "unknown_events": result.unknown,
        "parse_errors": result.parse_errors,
        "lines_read": result.lines_read,
    }


def print_text(result: Result, out=None):
    out = out or sys.stdout     # resolved at call time so redirected stdout is respected
    width = max(len(r.feature) for r in result.spec)
    for row in result.spec:
        status = result.row_status(row)
        occurrences = result.seen.get(row.row_id, [])
        print(f"{row.row_id}  {status:<10}  {row.feature:<{width}}  {row.event}  (seen {len(occurrences)})", file=out)
        for o in occurrences:
            for f in o.findings:
                print(f"      {o.time} line {o.line_no}: {f.level} {f.param}: {f.message}", file=out)
    for u in result.unknown:
        print(f"???  UNKNOWN     {u['event']} at {u['time']} line {u['line_no']}: no spec row", file=out)
    for e in result.parse_errors:
        print(f"!!!  PARSE ERROR line {e['line_no']} at {e['time']}: {e['error']}", file=out)
    s = result.summary()
    print(f"\n{len(result.spec)} spec rows: {s[PASS]} pass, {s[WARN]} warn, {s[FAIL]} fail, "
          f"{s[NOT_TESTED]} not tested · {len(result.unknown)} unknown event(s) · "
          f"{len(result.parse_errors)} unparsable line(s)", file=out)


def render_html(result: Result, spec_name: str, log_name: str) -> str:
    e = html.escape
    s = result.summary()
    css_status = {PASS: "pass", WARN: "warn", FAIL: "fail", NOT_TESTED: "none"}
    body_rows = []
    for row in result.spec:
        status = result.row_status(row)
        occurrences = result.seen.get(row.row_id, [])
        notes = "".join(
            f'<li><span class="lv {css_status[f.level]}">{f.level}</span> <code>{e(f.param)}</code> {e(f.message)} '
            f'<span class="at">{e(o.time)} · line {o.line_no}</span></li>'
            for o in occurrences for f in o.findings
        ) or ('<li class="muted">All checks passed</li>' if occurrences else '<li class="muted">Not fired in this session</li>')
        last = e(occurrences[-1].raw.split(": ", 1)[-1]) if occurrences else "—"
        match = " ".join(f"{k}={v}" for k, v in row.match.items())
        body_rows.append(
            f'<tr class="r-{css_status[status]}"><td class="id">{e(row.row_id)}</td>'
            f'<td><b>{e(row.feature)}</b><div class="muted">{e(row.trigger)}</div></td>'
            f'<td><code>{e(row.event)}</code>{f"<div class=muted><code>{e(match)}</code></div>" if match else ""}</td>'
            f'<td><span class="st {css_status[status]}">{status}</span><div class="muted">seen {len(occurrences)}</div></td>'
            f'<td><ul>{notes}</ul></td><td class="log"><code>{last}</code></td></tr>'
        )
    extras = "".join(
        f'<li><span class="lv warn">UNKNOWN</span> <code>{e(u["event"])}</code> has no spec row '
        f'<span class="at">{e(u["time"])} · line {u["line_no"]}</span></li>' for u in result.unknown
    ) + "".join(
        f'<li><span class="lv fail">PARSE</span> unreadable payload: {e(p["error"])} '
        f'<span class="at">{e(p["time"])} · line {p["line_no"]}</span></li>' for p in result.parse_errors
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tracking Validator Demo</title>
<style>
:root {{ --bg:#F1F3F6; --surface:#fff; --ink:#111C26; --muted:#566474; --line:#D5DCE4; --accent:#1E4CCB;
  --pass:#127A50; --pass-bg:#E1F3EA; --fail:#C33B27; --fail-bg:#FBE5E1; --warn:#8A5A0B; --warn-bg:#FBF0DC; --none-bg:#E9EDF2; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0D131A; --surface:#16202A; --ink:#E7ECF1; --muted:#93A1B0; --line:#27333F; --accent:#7E9CFF;
  --pass:#52C68E; --pass-bg:#123028; --fail:#F27A66; --fail-bg:#3A1D19; --warn:#E5AE52; --warn-bg:#352812; --none-bg:#1C2731; color-scheme: dark; }} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width:76rem; margin:0 auto; padding:32px max(16px, env(safe-area-inset-left, 0px)) 48px; }}
h1 {{ font-size:1.7rem; margin:0 0 6px; }}
code {{ font:12.5px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace; }}
a {{ color:var(--accent); }}
.muted, .at {{ color:var(--muted); font-size:13px; }}
.banner {{ border:1px solid var(--line); border-left:4px solid var(--warn); background:var(--warn-bg); padding:12px 16px; border-radius:8px; margin:16px 0 24px; }}
.tiles {{ display:flex; flex-wrap:wrap; gap:10px; margin:0 0 20px; }}
.tile {{ background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:10px 16px; min-width:7rem; }}
.tile b {{ display:block; font-size:1.6rem; line-height:1.1; }}
.tile.pass b {{ color:var(--pass); }} .tile.fail b {{ color:var(--fail); }} .tile.warn b {{ color:var(--warn); }}
.scroll {{ overflow-x:auto; background:var(--surface); border:1px solid var(--line); border-radius:10px; }}
table {{ border-collapse:collapse; width:100%; min-width:62rem; }}
th, td {{ text-align:left; vertical-align:top; padding:10px 12px; border-bottom:1px solid var(--line); }}
th {{ font:600 11px/1 ui-monospace, Menlo, monospace; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }}
td.id {{ font-family:ui-monospace, Menlo, monospace; color:var(--muted); }}
td ul {{ margin:0; padding:0; list-style:none; display:grid; gap:4px; }}
td:nth-child(2) {{ min-width:13rem; }}
td.log {{ width:22rem; }}
td.log code {{ word-break:break-all; color:var(--muted); }}
.st, .lv {{ font:600 11px/1 ui-monospace, Menlo, monospace; padding:4px 7px; border-radius:4px; white-space:nowrap; display:inline-block; }}
.pass {{ color:var(--pass); background:var(--pass-bg); }} .fail {{ color:var(--fail); background:var(--fail-bg); }}
.warn {{ color:var(--warn); background:var(--warn-bg); }} .none {{ color:var(--muted); background:var(--none-bg); }}
tr.r-fail td:first-child {{ box-shadow: inset 3px 0 var(--fail); }}
.extras {{ margin-top:20px; background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.extras ul {{ margin:8px 0 0; padding:0; list-style:none; display:grid; gap:6px; }}
</style></head>
<body><main>
<h1>Tracking Validator Demo</h1>
<p class="muted">Spec <code>{e(spec_name)}</code> · log <code>{e(log_name)}</code> · {result.lines_read} log lines read</p>
<div class="banner">Demo with fake data for a made-up fitness app. It re-implements the validation idea behind an internal QA dashboard; it is not that tool's code.
<a href="https://github.com/BuiKhanh99/BuiKhanh99.github.io/tree/main/tracking-demo">README and source</a> · <a href="../#cs-01">Back to portfolio</a></div>
<div class="tiles">
<div class="tile"><b>{len(result.spec)}</b><span class="muted">spec rows</span></div>
<div class="tile pass"><b>{s[PASS]}</b><span class="muted">pass</span></div>
<div class="tile warn"><b>{s[WARN]}</b><span class="muted">warn</span></div>
<div class="tile fail"><b>{s[FAIL]}</b><span class="muted">fail</span></div>
<div class="tile"><b>{s[NOT_TESTED]}</b><span class="muted">not tested</span></div>
</div>
<div class="scroll"><table>
<thead><tr><th>Row</th><th>Feature · trigger</th><th>Event · match</th><th>Status</th><th>Findings</th><th>Last payload</th></tr></thead>
<tbody>{"".join(body_rows)}</tbody></table></div>
<div class="extras"><b>Outside the spec</b><ul>{extras or '<li class="muted">None</li>'}</ul></div>
</main></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate logcat tracking events against a tracking spec.")
    ap.add_argument("--spec", required=True, help="spec CSV (one line per row_id + param)")
    ap.add_argument("--log", required=True, help="logcat file, or - to read stdin (e.g. piped from adb logcat)")
    ap.add_argument("--json", help="write the full result as JSON to this path")
    ap.add_argument("--html", help="write an HTML report to this path")
    args = ap.parse_args(argv)

    spec = load_spec(args.spec)
    source = sys.stdin if args.log == "-" else open(args.log, encoding="utf-8")
    try:
        result = validate(spec, source)
    finally:
        if source is not sys.stdin:
            source.close()

    print_text(result)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(to_dict(result), f, indent=2, ensure_ascii=False)
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(render_html(result, args.spec, "stdin" if args.log == "-" else args.log))
    return 1 if result.summary()[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
