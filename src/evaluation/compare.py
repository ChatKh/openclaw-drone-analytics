"""
Rule vs ML on the same data (Phase 5, day 12).

Runs the rule detector and every ML mode on one database, scores each with the
evaluator, and splits false alarms into windy and calm flights (from
conditions.csv, which is analysis-only data and never shown to a detector).

Nothing here changes a threshold. Run it on the test set once, after the
choices were made on dev.

Run:
  DRONE_DB_PATH=data/test.db python -m src.evaluation.compare --data-dir data/test --svg docs/rule_vs_ml.svg
"""
import argparse
import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import joblib

from src.analytics import ml_detector, rule_detector
from src.db.database import connect
from src.evaluation.evaluator import TYPES, Report, evaluate

log = logging.getLogger("compare")


@dataclass
class Row:
    name: str
    report: Report
    windy_false: int
    calm_false: int


def _windy_missions(data_dir: Path) -> set[str] | None:
    path = data_dir / "conditions.csv"
    if not path.exists():
        return None
    with path.open() as f:
        return {r["mission_id"] for r in csv.DictReader(f) if r["windy"] == "True"}


def compare(conn, bundle: dict, windy: set[str] | None) -> list[Row]:
    rows = []

    def add(name: str, detector: str) -> None:
        rep = evaluate(conn, detector)
        w = sum(fa["mission_id"] in windy for fa in rep.false_alarms) if windy is not None else -1
        rows.append(Row(name, rep, w, len(rep.false_alarms) - max(w, 0)))

    rule_detector.run(conn)
    add("rule v2", "rule")
    for mode in ml_detector.MODES:
        if mode == ml_detector.DEFAULT_MODE:
            continue
        ml_detector.run(conn, bundle, mode)
        add(f"ml {mode}", "ml")
    ml_detector.run(conn, bundle, ml_detector.DEFAULT_MODE)  # leave the chosen mode stored
    add(f"ml {ml_detector.DEFAULT_MODE}", "ml")
    return rows


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def format_table(rows: list[Row]) -> str:
    head = f"{'detector':<13} {'precision':>9} {'recall':>7} {'false':>6} {'per_h':>6} {'windy/calm':>11}  " + \
           "  ".join(f"{t:>7}" for t in TYPES)
    lines = [head]
    for r in rows:
        t = r.report.total
        split = "-" if r.windy_false < 0 else f"{r.windy_false}/{r.calm_false}"
        per_type = "  ".join(f"{b.detected:>3}/{b.injected:<3}" for b in (r.report.by_type[k] for k in TYPES))
        lines.append(f"{r.name:<13} {_pct(t.precision):>9} {_pct(t.recall):>7} {t.false_alarms:>6} "
                     f"{r.report.false_alarms_per_hour or 0:>6.2f} {split:>11}  {per_type}")
    return "\n".join(lines)


def recall_svg(rows: list[Row], title: str) -> str:
    """Grouped bars: recall per anomaly type, rule vs the chosen ML mode. Static SVG, light and dark."""
    pick = [rows[0], rows[-1]]  # rule, chosen ML mode
    cats = TYPES + ["all"]
    W, H, L, R, T, B = 640, 360, 48, 16, 112, 44
    pw, ph = W - L - R, H - T - B
    gw = pw / len(cats)
    bw, gap = 30, 2

    def recall(r: Row, c: str) -> float:
        x = (r.report.total if c == "all" else r.report.by_type[c]).recall
        return x or 0.0

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
           f'role="img" aria-labelledby="t d" font-family="system-ui, -apple-system, Segoe UI, sans-serif">',
           f'<title id="t">{escape(title)}</title>',
           '<desc id="d">' + escape("; ".join(
               f"{r.name}: " + ", ".join(f"{c} {recall(r, c):.0%}" for c in cats) for r in pick)) + '</desc>',
           """<style>
  .bg{fill:#fcfcfb} .ink{fill:#0b0b0b} .ink2{fill:#52514e} .grid{stroke:#e4e3df} .base{stroke:#8a8984}
  .s1{fill:#2a78d6} .s2{fill:#eb6834}
  @media (prefers-color-scheme: dark){
    .bg{fill:#1a1a19} .ink{fill:#f0efec} .ink2{fill:#c3c2b7} .grid{stroke:#33332f} .base{stroke:#6d6c66}
    .s1{fill:#3987e5} .s2{fill:#d95926}
  }
</style>""",
           f'<rect class="bg" width="{W}" height="{H}" rx="8"/>',
           f'<text class="ink" x="{L}" y="26" font-size="15" font-weight="600">{escape(title)}</text>']

    # legend: one line per detector, with its precision and false alarms
    for i, r in enumerate(pick):
        fa = r.report.total.false_alarms
        y = 50 + 18 * i
        out.append(f'<rect class="s{i + 1}" x="{L}" y="{y}" width="12" height="12" rx="2"/>')
        out.append(f'<text class="ink2" x="{L + 18}" y="{y + 10}" font-size="12">'
                   f'<tspan class="ink" font-weight="600">{escape(r.name)}</tspan><tspan dx="6">precision '
                   f'{_pct(r.report.total.precision)}, {fa} false alarm{"" if fa == 1 else "s"}</tspan></text>')

    for v in (0, 0.25, 0.5, 0.75, 1.0):
        y = T + ph * (1 - v)
        cls = "base" if v == 0 else "grid"
        out.append(f'<line class="{cls}" x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" stroke-width="1"/>')
        out.append(f'<text class="ink2" x="{L - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end">{v:.0%}</text>')

    for ci, c in enumerate(cats):
        cx = L + gw * ci + gw / 2
        x0 = cx - bw - gap / 2
        for si, r in enumerate(pick):
            v = recall(r, c)
            h = ph * v
            x = x0 + si * (bw + gap)
            y = T + ph - h
            if h > 0:
                rr = min(4, h)
                # rounded top, square base
                out.append(f'<path class="s{si + 1}" d="M{x:.1f},{T + ph:.1f} V{y + rr:.1f} '
                           f'Q{x:.1f},{y:.1f} {x + rr:.1f},{y:.1f} H{x + bw - rr:.1f} '
                           f'Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + rr:.1f} V{T + ph:.1f} Z"/>')
            out.append(f'<text class="ink" x="{x + bw / 2:.1f}" y="{y - 5:.1f}" font-size="10" '
                       f'text-anchor="middle">{v:.0%}</text>')
        weight = ' font-weight="600"' if c == "all" else ""
        out.append(f'<text class="ink2" x="{cx:.1f}" y="{T + ph + 18:.1f}" font-size="12" '
                   f'text-anchor="middle"{weight}>{c}</text>')
    out.append(f'<text class="ink2" x="{L}" y="{H - 8}" font-size="11">Recall = share of injected faults caught '
               f'(same mission, same type, time within ±30 s)</text>')
    out.append("</svg>")
    return "\n".join(out)


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Rule vs ML on one database")
    ap.add_argument("--data-dir", default="data", help="folder with conditions.csv")
    ap.add_argument("--svg", help="write a recall chart here (e.g. docs/rule_vs_ml.svg)")
    ap.add_argument("--title", default="Rule vs ML: recall by fault type")
    args = ap.parse_args()

    if not ml_detector.MODEL_PATH.exists():
        raise SystemExit(f"{ml_detector.MODEL_PATH} not found. Train first: python -m src.analytics.ml_detector train")
    conn = connect()
    try:
        rows = compare(conn, joblib.load(ml_detector.MODEL_PATH), _windy_missions(Path(args.data_dir)))
    finally:
        conn.close()
    print(format_table(rows))
    print("\nwindy/calm = false alarms on windy flights / on calm flights; type columns = caught/injected")
    if args.svg:
        Path(args.svg).parent.mkdir(parents=True, exist_ok=True)
        Path(args.svg).write_text(recall_svg(rows, args.title))
        print(f"chart: {args.svg}")


if __name__ == "__main__":
    main()
