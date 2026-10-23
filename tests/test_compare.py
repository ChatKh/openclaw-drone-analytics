"""Tests for the rule vs ML comparison output (Phase 5, day 12)."""
import xml.etree.ElementTree as ET

from src.evaluation.compare import Row, format_table, recall_svg
from src.evaluation.evaluator import TYPES, Report, TypeResult


def report(name, caught):
    by_type = {t: TypeResult(t, injected=2, detected=caught, alerts=caught, correct_alerts=caught) for t in TYPES}
    total = TypeResult("all", injected=8, detected=4 * caught, alerts=4 * caught, correct_alerts=4 * caught)
    return Report(name, 10.0, by_type, total, [], [])


def rows():
    return [Row("rule v2", report("rule", 2), 0, 0), Row("ml envelope", report("ml", 1), 0, 0)]


def test_table_has_one_line_per_detector():
    lines = format_table(rows()).splitlines()
    assert len(lines) == 3 and lines[1].startswith("rule v2") and "50%" in lines[2]


def test_chart_is_valid_svg_with_both_detectors():
    svg = recall_svg(rows(), "test chart")
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert "rule v2" in svg and "ml envelope" in svg and "prefers-color-scheme: dark" in svg
