import math
import subprocess
import sys
from pathlib import Path
from statistics import NormalDist

import pytest

import stockrank as sr

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "stockrank.py"
EXAMPLE = ROOT / "examples" / "shelf.stock"


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def one_item(extra="", settings="set service=95% holding=25% ordercost=10"):
    return f"{settings}\nitem A annual=3650 cost=2 sd=4 lead=9 {extra}\n"


def test_parses_settings_and_items():
    text = "\nset service=95% holding=25% ordercost=12 cut_a=80% cut_b=95%\n" \
           "item BOLT annual=4800 cost=0.35 sd=14 lead=10 onhand=260 pack=100 moq=200\n"
    plan = sr.parse_text(text, source="x.stock")
    assert plan.errors == []
    assert plan.settings.service == pytest.approx(0.95)
    assert plan.settings.holding == pytest.approx(0.25)
    assert plan.settings.ordercost == pytest.approx(12.0)
    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.sku == "BOLT"
    assert item.line == 3
    assert item.pack == 100
    assert item.moq == 200
    assert item.ordercost == pytest.approx(12.0)


def test_percent_and_fraction_are_both_accepted():
    assert sr.convert("95%", "fraction") == pytest.approx(0.95)
    assert sr.convert("0.95", "fraction") == pytest.approx(0.95)


def test_bare_percent_number_is_rejected_with_hint():
    with pytest.raises(ValueError, match="percent"):
        sr.convert("95", "fraction")


def test_errors_carry_file_and_line_numbers():
    text = (
        "set ordercost=5\n"
        "item A annual=1 cost=0 sd=0 lead=0\n"
        "item B annual=1 cost=1 sd=0\n"
        "widgets X\n"
    )
    plan = sr.parse_text(text, source="shelf.stock")
    assert "shelf.stock:2: cost must be above 0" in plan.errors
    assert "shelf.stock:3: missing lead" in plan.errors
    assert "shelf.stock:4: unknown directive 'widgets' (expected 'set' or 'item')" in plan.errors


def test_unknown_item_field_is_reported():
    text = "set ordercost=5\nitem A annual=1 cost=1 sd=0 lead=0 colour=red\n"
    plan = sr.parse_text(text, source="x.stock")
    assert plan.errors == ["x.stock:2: unknown item field 'colour'"]


def test_duplicate_sku_points_at_first_definition():
    text = (
        "set ordercost=5\n"
        "item A annual=1 cost=1 sd=0 lead=0\n"
        "item A annual=2 cost=1 sd=0 lead=0\n"
    )
    plan = sr.parse_text(text, source="x.stock")
    assert plan.errors == ["x.stock:3: duplicate SKU 'A' (first defined on line 2)"]


def test_missing_ordering_cost_is_reported():
    plan = sr.parse_text("item A annual=1 cost=1 sd=0 lead=0\n", source="x.stock")
    assert plan.errors == ["x.stock:1: no ordering cost (set ordercost= on the item or in a set line)"]


def test_empty_file_has_no_items_error():
    plan = sr.parse_text("# nothing here\n")
    assert plan.errors == ["stock: no item lines found"]


def test_cut_offs_must_be_ordered():
    text = "set cut_a=96% cut_b=95%\nitem A annual=1 cost=1 sd=0 lead=0 ordercost=1\n"
    plan = sr.parse_text(text, source="x.stock")
    assert any("must be below cut_b" in message for message in plan.errors)


def test_abc_classes_follow_cumulative_share():
    settings = sr.Settings(cut_a=0.80, cut_b=0.95)
    classes = sr.classify({"A1": 800.0, "B1": 150.0, "C1": 50.0}, settings)
    assert classes == {"A1": "A", "B1": "B", "C1": "C"}


def test_eoq_formula_and_pack_rounding():
    eoq = sr.economic_order_quantity(1000, 20, 2.0)
    assert eoq == pytest.approx(math.sqrt(20000))
    assert sr.round_order(eoq, 10, 1) == 150
    assert sr.round_order(eoq, 1, 200) == 200


def test_exact_pack_multiple_is_not_rounded_up():
    assert sr.round_order(100.0, 10, 1) == 100


def test_safety_stock_and_reorder_point_match_formula():
    plan = sr.parse_text(one_item("onhand=100"), source="x.stock")
    assert plan.errors == []
    row = sr.analyse(plan)[0]
    z = NormalDist().inv_cdf(0.95)
    assert row.daily == pytest.approx(10.0)
    assert row.safety == pytest.approx(z * 4 * math.sqrt(9))
    assert row.rop == pytest.approx(10 * 9 + z * 4 * math.sqrt(9))


def test_yearly_costs_match_hand_formula():
    order, carry = sr.yearly_costs(1000, 20, 2.0, 150, 0.0)
    assert order == pytest.approx(1000 / 150 * 20)
    assert carry == pytest.approx((150 / 2) * 2.0)


def test_stock_status_rules():
    def item(**kwargs):
        base = dict(sku="A", line=1, annual=365.0, cost=1.0, sd=0.0, lead=5.0)
        base.update(kwargs)
        return sr.Item(**base)

    assert sr.stock_status(item(onhand=0.0), 1.0, 0.0, 5.0, 7.0) == "OUT"
    assert sr.stock_status(item(onhand=10.0), 1.0, 10.0, 12.0, 7.0) == "REORDER"
    assert sr.stock_status(item(onhand=15.0), 1.0, 15.0, 12.0, 7.0) == "WATCH"
    assert sr.stock_status(item(onhand=20.0), 1.0, 20.0, 12.0, 7.0) == "OK"
    assert sr.stock_status(item(annual=0.0, onhand=5.0), 0.0, 5.0, 0.0, 7.0) == "NO DEMAND"


def test_gate_flags_thin_cover_and_out_of_stock():
    text = (
        "set ordercost=5\n"
        "item THIN annual=365 cost=1 sd=0 lead=5 onhand=5\n"
        "item GONE annual=365 cost=1 sd=0 lead=5 onhand=0\n"
    )
    plan = sr.parse_text(text, source="x.stock")
    rows = sr.analyse(plan)
    failures = sr.gate_failures(rows, min_cover=14)
    assert "THIN: 5.0 days of cover, below 14" in failures
    assert "GONE: out of stock" in failures


def test_sweep_safety_value_rises_with_service_level():
    plan = sr.parse_text(one_item("onhand=10"), source="x.stock")
    results = sr.sweep(plan, [0.90, 0.95, 0.99])
    values = [value for _, _, value in results]
    assert values[0] < values[1] < values[2]
    z = NormalDist().inv_cdf(0.95)
    assert values[1] == pytest.approx(z * 4 * math.sqrt(9) * 2)


def test_count_schedule_staggers_start_days():
    text = (
        "set ordercost=5 count_a=30 count_b=90 count_c=180\n"
        "item BIG annual=1000 cost=1 sd=0 lead=1 onhand=5\n"
        "item DEAD annual=0 cost=1 sd=0 lead=1 onhand=5\n"
    )
    plan = sr.parse_text(text, source="x.stock")
    rows = sr.analyse(plan)
    schedule = sr.count_schedule(rows, plan.settings)
    big, dead = schedule
    assert big[0].item.sku == "BIG" and big[1] == 30
    assert big[2][:3] == [1, 31, 61]
    assert dead[0].item.sku == "DEAD" and dead[1] == 180
    assert dead[2] == [2, 182, 362]


def test_example_file_classes_and_statuses():
    plan = sr.load_plan(str(EXAMPLE))
    rows = {row.item.sku: row for row in sr.analyse(plan)}
    assert rows["BOLT-M6-20"].cls == "A"
    assert rows["SCREW-WOOD"].cls == "B"
    assert rows["PLUG-RAW"].cls == "C"
    assert rows["SEALANT-300"].status == "OUT"
    assert rows["PLUG-RAW"].status == "NO DEMAND"


def test_cli_check_passes_example():
    result = run_cli("check", str(EXAMPLE))
    assert result.returncode == 0
    assert result.stdout.startswith("OK:")


def test_cli_check_exits_one_with_line_numbered_error(tmp_path):
    bad = tmp_path / "bad.stock"
    bad.write_text("item A annual=x cost=1 sd=0 lead=0 ordercost=1\n", encoding="utf-8")
    result = run_cli("check", str(bad))
    assert result.returncode == 1
    assert "bad.stock:1: 'x' is not a number" in result.stderr


def test_cli_gate_exits_three_on_out_of_stock(tmp_path):
    stock = tmp_path / "gone.stock"
    stock.write_text("set ordercost=5\nitem A annual=10 cost=1 sd=0 lead=1 onhand=0\n",
                     encoding="utf-8")
    result = run_cli("gate", str(stock))
    assert result.returncode == 3
    assert "FAIL A: out of stock" in result.stdout


def test_cli_csv_report_header():
    result = run_cli("report", str(EXAMPLE), "--format", "csv")
    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == (
        "sku,class,annual_value,daily,eoq,order_qty,safety,rop,"
        "position,status,cover_days,annual_cost"
    )


def test_cli_json_report_is_valid_json():
    import json

    result = run_cli("report", str(EXAMPLE), "--format", "json")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert len(data) == 8


def test_cli_eoq_command_prints_hand_checked_quantity():
    result = run_cli("eoq", "--annual", "1000", "--cost", "8", "--ordercost", "20",
                     "--holding", "25%", "--pack", "10")
    assert result.returncode == 0
    assert "EOQ: 141.42 -> order 150" in result.stdout
