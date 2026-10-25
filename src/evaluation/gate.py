"""
Results gate (Phase 8, day 22): fail the build when the detectors get worse.

Reads the pipeline report (data/pipeline_report.json) and compares each
detector's precision and recall with a minimum. Code changes that quietly make
detection worse then fail CI, the same idea as a regression limit on a
production test station.

The minimums sit a little under today's dev-set results (rule 100 % / 100 %,
ML 92 % / 86 %), so one extra false alarm does not fail the build but a real
drop does. Raise them when the detectors improve; never lower them to make a
red build green without writing down why.

Run:  python -m src.evaluation.gate [--report data/pipeline_report.json]
Exit code: 0 pass, 1 below a minimum, 2 report missing or the pipeline failed.
"""
import argparse
import json
import sys
from pathlib import Path

MINIMUMS = {
    "rule": {"precision": 0.95, "recall": 0.95},
    "ml": {"precision": 0.85, "recall": 0.80},
}


def check(report: dict, minimums: dict = MINIMUMS) -> list[str]:
    """Return a list of problems; empty means the gate passes."""
    if not report.get("ok"):
        return [f"pipeline did not finish: {report.get('error') or 'unknown error'}"]
    problems = []
    for det, mins in minimums.items():
        got = report.get("detectors", {}).get(det)
        if got is None:
            problems.append(f"{det}: no results in the report")
            continue
        for metric, minimum in mins.items():
            value = got.get(metric)
            if value is None or value < minimum:
                shown = "n/a" if value is None else f"{value:.0%}"
                problems.append(f"{det} {metric} {shown} is below the minimum {minimum:.0%}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fail when detector results drop below the minimums")
    ap.add_argument("--report", type=Path, default=Path("data/pipeline_report.json"))
    args = ap.parse_args(argv)
    if not args.report.exists():
        print(f"gate: {args.report} not found; run python -m src.pipeline first")
        return 2
    report = json.loads(args.report.read_text(encoding="utf-8"))
    problems = check(report)
    for det, r in report.get("detectors", {}).items():
        p = "n/a" if r.get("precision") is None else f"{r['precision']:.0%}"
        print(f"gate: {det:<4} precision {p:>4}  recall {(r.get('recall') or 0):.0%}  "
              f"(minimum {MINIMUMS.get(det, {}).get('precision', 0):.0%} / {MINIMUMS.get(det, {}).get('recall', 0):.0%})")
    if problems:
        for msg in problems:
            print(f"gate: FAIL {msg}")
        return 2 if not report.get("ok") else 1
    print("gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
