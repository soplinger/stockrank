#!/usr/bin/env python3
"""Stockrank: offline inventory planner for plain-text SKU files.

One readable file holds set lines (planning settings) and item lines (one
per SKU). Stockrank reports ABC classes, economic order quantities, safety
stock, reorder points, stock status, cycle-count schedules and a CI gate.
Standard library only; no network, no accounts.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
from dataclasses import dataclass, field
from statistics import NormalDist

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_GATE = 3

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REQUIRED_ITEM_FIELDS = ("annual", "cost", "sd", "lead")
STATUS_ORDER = ("OK", "WATCH", "REORDER", "OUT", "NO DEMAND")

# key: (kind, rule, rule text). Kinds: fraction (accepts 95% or 0.95),
# number, int.
SET_SPECS = {
    "service": ("fraction", lambda v: 0 < v < 1, "must be above 0% and below 100%"),
    "holding": ("fraction", lambda v: 0 < v <= 1, "must be above 0%"),
    "cut_a": ("fraction", lambda v: 0 < v < 1, "must be above 0% and below 100%"),
    "cut_b": ("fraction", lambda v: 0 < v <= 1, "must be above 0% and at most 100%"),
    "days": ("int", lambda v: v >= 1, "must be at least 1"),
    "watch": ("number", lambda v: v >= 0, "must not be negative"),
    "ordercost": ("number", lambda v: v >= 0, "must not be negative"),
    "count_a": ("int", lambda v: v >= 1, "must be at least 1 day"),
    "count_b": ("int", lambda v: v >= 1, "must be at least 1 day"),
    "count_c": ("int", lambda v: v >= 1, "must be at least 1 day"),
}

ITEM_SPECS = {
    "annual": ("number", lambda v: v >= 0, "must not be negative"),
    "cost": ("number", lambda v: v > 0, "must be above 0"),
    "sd": ("number", lambda v: v >= 0, "must not be negative"),
    "lead": ("number", lambda v: v >= 0, "must not be negative"),
    "onhand": ("number", lambda v: v >= 0, "must not be negative"),
    "onorder": ("number", lambda v: v >= 0, "must not be negative"),
    "ordercost": ("number", lambda v: v >= 0, "must not be negative"),
    "pack": ("int", lambda v: v >= 1, "must be at least 1"),
    "moq": ("int", lambda v: v >= 1, "must be at least 1"),
}


class LoadError(Exception):
    """Raised when a stock file cannot be read or has problems."""


@dataclass
class Settings:
    service: float = 0.95
    holding: float = 0.25
    cut_a: float = 0.80
    cut_b: float = 0.95
    days: int = 365
    watch: float = 7.0
    ordercost: float | None = None
    count_a: int = 30
    count_b: int = 90
    count_c: int = 180


@dataclass
class Item:
    sku: str
    line: int
    annual: float
    cost: float
    sd: float
    lead: float
    onhand: float = 0.0
    onorder: float = 0.0
    ordercost: float | None = None
    pack: int = 1
    moq: int = 1


@dataclass
class Plan:
    settings: Settings
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class Row:
    item: Item
    cls: str
    value: float
    daily: float
    safety: float
    rop: float
    position: float
    eoq: float
    qty: int
    status: str
    cover: float | None
    order_cost: float
    carry_cost: float

    @property
    def total_cost(self) -> float:
        return self.order_cost + self.carry_cost


# ---------------------------------------------------------------- parsing

def to_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"'{text}' is not a number") from None
    if not math.isfinite(value):
        raise ValueError(f"'{text}' is not a finite number")
    return value


def convert(text: str, kind: str) -> float | int:
    if kind == "fraction" and text.endswith("%"):
        number = to_float(text[:-1]) / 100
    else:
        number = to_float(text)
        if kind == "fraction" and number > 1:
            raise ValueError(f"'{text}' is above 1; write it as a percent, e.g. {text}%")
    if number < 0:
        raise ValueError(f"'{text}' must not be negative")
    if kind == "int":
        if number != int(number):
            raise ValueError(f"'{text}' must be a whole number")
        return int(number)
    return number


def read_pair(token: str, specs: dict, label: str) -> tuple[str, float | int]:
    key, sep, text = token.partition("=")
    key = key.lower()
    if not sep or not key or not text:
        raise ValueError(f"expected key=value, got '{token}'")
    if key not in specs:
        raise ValueError(f"unknown {label} '{key}'")
    kind, rule, rule_text = specs[key]
    value = convert(text, kind)
    if not rule(value):
        raise ValueError(f"{key} {rule_text}")
    return key, value


def parse_text(text: str, source: str = "stock") -> Plan:
    settings = Settings()
    plan = Plan(settings=settings)
    set_seen: set[str] = set()
    sku_lines: dict[str, int] = {}

    def fail(lineno: int, message: str) -> None:
        plan.errors.append(f"{source}:{lineno}: {message}")

    for lineno, raw in enumerate(text.splitlines(), start=1):
        tokens = raw.split("#", 1)[0].split()
        if not tokens:
            continue
        keyword, args = tokens[0].lower(), tokens[1:]

        if keyword == "set":
            for token in args:
                try:
                    key, value = read_pair(token, SET_SPECS, "setting")
                except ValueError as exc:
                    fail(lineno, str(exc))
                    continue
                if key in set_seen:
                    fail(lineno, f"duplicate setting '{key}'")
                    continue
                set_seen.add(key)
                setattr(settings, key, value)

        elif keyword == "item":
            if not args:
                fail(lineno, "item line needs a SKU name")
                continue
            sku, pairs = args[0], args[1:]
            if not NAME_RE.match(sku):
                fail(lineno, f"invalid SKU name '{sku}' (use letters, digits, '.', '_' or '-')")
                continue
            if sku in sku_lines:
                fail(lineno, f"duplicate SKU '{sku}' (first defined on line {sku_lines[sku]})")
                continue
            fields: dict[str, float | int] = {}
            line_ok = True
            for token in pairs:
                try:
                    key, value = read_pair(token, ITEM_SPECS, "item field")
                except ValueError as exc:
                    fail(lineno, str(exc))
                    line_ok = False
                    continue
                if key in fields:
                    fail(lineno, f"duplicate field '{key}'")
                    line_ok = False
                    continue
                fields[key] = value
            missing = [name for name in REQUIRED_ITEM_FIELDS if name not in fields]
            if missing:
                fail(lineno, f"missing {', '.join(missing)}")
                line_ok = False
            if not line_ok:
                continue
            sku_lines[sku] = lineno
            plan.items.append(Item(sku=sku, line=lineno, **fields))

        else:
            fail(lineno, f"unknown directive '{keyword}' (expected 'set' or 'item')")

    if settings.cut_a >= settings.cut_b:
        plan.errors.append(
            f"{source}: cut_a ({settings.cut_a * 100:g}%) must be below "
            f"cut_b ({settings.cut_b * 100:g}%)"
        )
    for item in plan.items:
        if item.ordercost is None:
            if settings.ordercost is None:
                plan.errors.append(
                    f"{source}:{item.line}: no ordering cost "
                    "(set ordercost= on the item or in a set line)"
                )
            else:
                item.ordercost = settings.ordercost
    if not plan.items and not plan.errors:
        plan.errors.append(f"{source}: no item lines found")
    return plan


def load_plan(path: str) -> Plan:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise LoadError(f"stockrank: cannot read {path}: {exc.strerror}") from None
    plan = parse_text(text, source=path)
    if plan.errors:
        summary = f"stockrank: {len(plan.errors)} problem(s) in {path}"
        raise LoadError("\n".join(plan.errors + [summary]))
    return plan


# ------------------------------------------------------------- the maths

def classify(values: dict[str, float], settings: Settings) -> dict[str, str]:
    """Pareto ABC classes: an item is A while the running share above it is under cut_a."""
    total = sum(values.values())
    classes: dict[str, str] = {}
    running = 0.0
    for sku, value in sorted(values.items(), key=lambda kv: (-kv[1], kv[0])):
        share = running / total if total > 0 else 1.0
        if value <= 0:
            cls = "C"
        elif share < settings.cut_a:
            cls = "A"
        elif share < settings.cut_b:
            cls = "B"
        else:
            cls = "C"
        classes[sku] = cls
        running += value
    return classes


def economic_order_quantity(annual: float, ordercost: float, unit_holding: float) -> float:
    if annual <= 0 or ordercost <= 0:
        return 0.0
    return math.sqrt(2 * annual * ordercost / unit_holding)


def round_order(eoq: float, pack: int, moq: int) -> int:
    # Round the pack ratio to 9 decimals before ceiling so float noise cannot add a pack.
    packs = math.ceil(round(eoq / pack, 9))
    return max(moq, packs * pack)


def yearly_costs(annual: float, ordercost: float, unit_holding: float,
                 qty: int, safety: float) -> tuple[float, float]:
    order = annual / qty * ordercost if annual > 0 else 0.0
    carry = (qty / 2 + safety) * unit_holding
    return order, carry


def stock_status(item: Item, daily: float, position: float, rop: float, watch: float) -> str:
    if item.annual == 0:
        return "NO DEMAND"
    if item.onhand == 0:
        return "OUT"
    if position <= rop:
        return "REORDER"
    if position <= rop + daily * watch:
        return "WATCH"
    return "OK"


def analyse(plan: Plan) -> list[Row]:
    s = plan.settings
    z = NormalDist().inv_cdf(s.service)
    values = {it.sku: it.annual * it.cost for it in plan.items}
    classes = classify(values, s)
    rows = []
    for it in plan.items:
        unit_holding = it.cost * s.holding
        daily = it.annual / s.days
        safety = z * it.sd * math.sqrt(it.lead)
        rop = daily * it.lead + safety
        position = it.onhand + it.onorder
        eoq = economic_order_quantity(it.annual, it.ordercost, unit_holding)
        qty = round_order(eoq, it.pack, it.moq)
        order_cost, carry_cost = yearly_costs(it.annual, it.ordercost, unit_holding, qty, safety)
        rows.append(Row(
            item=it,
            cls=classes[it.sku],
            value=values[it.sku],
            daily=daily,
            safety=safety,
            rop=rop,
            position=position,
            eoq=eoq,
            qty=qty,
            status=stock_status(it, daily, position, rop, s.watch),
            cover=it.onhand / daily if daily > 0 else None,
            order_cost=order_cost,
            carry_cost=carry_cost,
        ))
    rows.sort(key=lambda r: (r.cls, -r.value, r.item.sku))
    return rows


def sweep(plan: Plan, levels: list[float]) -> list[tuple[float, float, float]]:
    """Return (service, safety units, safety value) for each service level."""
    out = []
    for level in levels:
        z = NormalDist().inv_cdf(level)
        units = sum(z * it.sd * math.sqrt(it.lead) for it in plan.items)
        value = sum(z * it.sd * math.sqrt(it.lead) * it.cost for it in plan.items)
        out.append((level, units, value))
    return out


def count_schedule(rows: list[Row], settings: Settings) -> list[tuple[Row, int, list[int]]]:
    """Stagger cycle counts so items in one class do not all land on the same day."""
    intervals = {"A": settings.count_a, "B": settings.count_b, "C": settings.count_c}
    schedule = []
    for index, row in enumerate(rows):
        interval = intervals[row.cls]
        start = index % interval + 1
        days = list(range(start, settings.days + 1, interval))
        schedule.append((row, interval, days))
    return schedule


def gate_failures(rows: list[Row], min_cover: float) -> list[str]:
    failures = []
    for row in rows:
        sku = row.item.sku
        if row.status == "OUT":
            failures.append(f"{sku}: out of stock")
        elif row.cover is not None and row.cover < min_cover:
            failures.append(f"{sku}: {row.cover:.1f} days of cover, below {min_cover:g}")
    return failures


# --------------------------------------------------------------- output

def fmt(number: float) -> str:
    if float(number).is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def render_table(headers: list[str], body: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for cells in body:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))

    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    out = [line(headers), line(["-" * w for w in widths])]
    out.extend(line(cells) for cells in body)
    return "\n".join(out)


REPORT_HEADERS = ["SKU", "CLS", "VALUE/yr", "DAILY", "EOQ", "ORDER", "SAFETY",
                  "ROP", "POSITION", "COVER", "STATUS", "COST/yr"]


def report_cells(row: Row) -> list[str]:
    return [
        row.item.sku,
        row.cls,
        fmt(round(row.value, 2)),
        f"{row.daily:.2f}",
        fmt(round(row.eoq, 1)),
        fmt(row.qty),
        f"{row.safety:.1f}",
        f"{row.rop:.1f}",
        fmt(row.position),
        "-" if row.cover is None else f"{row.cover:.1f}",
        row.status,
        fmt(round(row.total_cost, 2)),
    ]


def render_text_report(plan: Plan, rows: list[Row]) -> str:
    s = plan.settings
    counts = {status: 0 for status in STATUS_ORDER}
    classes = {"A": 0, "B": 0, "C": 0}
    for row in rows:
        counts[row.status] += 1
        classes[row.cls] += 1
    on_hand = sum(row.item.onhand * row.item.cost for row in rows)
    safety_value = sum(row.safety * row.item.cost for row in rows)
    running = sum(row.order_cost + row.carry_cost for row in rows)
    lines = [
        render_table(REPORT_HEADERS, [report_cells(row) for row in rows]),
        "",
        f"Items: {len(rows)} (A {classes['A']}, B {classes['B']}, C {classes['C']})",
        "Status: " + ", ".join(f"{name} {counts[name]}" for name in STATUS_ORDER),
        f"Stock value on hand: {fmt(round(on_hand, 2))}",
        f"Safety stock value at {s.service * 100:g}% service: {fmt(round(safety_value, 2))}",
        f"Ordering and carrying cost per year at order quantities: {fmt(round(running, 2))}",
    ]
    return "\n".join(lines)


def report_csv(rows: list[Row]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["sku", "class", "annual_value", "daily", "eoq", "order_qty",
                     "safety", "rop", "position", "status", "cover_days", "annual_cost"])
    for row in rows:
        writer.writerow([
            row.item.sku, row.cls, round(row.value, 2), round(row.daily, 4),
            round(row.eoq, 2), row.qty, round(row.safety, 2), round(row.rop, 2),
            row.position, row.status,
            "" if row.cover is None else round(row.cover, 2),
            round(row.total_cost, 2),
        ])
    return buffer.getvalue()


def report_json(rows: list[Row]) -> str:
    data = [
        {
            "sku": row.item.sku,
            "class": row.cls,
            "annual_value": round(row.value, 2),
            "daily": round(row.daily, 4),
            "eoq": round(row.eoq, 2),
            "order_qty": row.qty,
            "safety": round(row.safety, 2),
            "rop": round(row.rop, 2),
            "position": row.position,
            "status": row.status,
            "cover_days": None if row.cover is None else round(row.cover, 2),
            "annual_cost": round(row.total_cost, 2),
        }
        for row in rows
    ]
    return json.dumps(data, indent=2)


# ------------------------------------------------------------------ CLI

def arg_number(text: str) -> float:
    try:
        return float(convert(text, "number"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def arg_fraction(text: str) -> float:
    try:
        value = convert(text, "fraction")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    if not 0 < value <= 1:
        raise argparse.ArgumentTypeError(f"'{text}' must be above 0% and at most 100%")
    return value


def arg_levels(text: str) -> list[float]:
    levels = []
    for part in text.split(","):
        try:
            level = to_float(part.strip()) / 100
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
        if not 0 < level < 1:
            raise argparse.ArgumentTypeError(f"service level '{part}' must be between 0 and 100")
        levels.append(level)
    return levels


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockrank",
        description="Offline inventory planner: ABC classes, EOQ, reorder points and a CI gate.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="lint a stock file with line-numbered errors")
    p.add_argument("file")

    p = sub.add_parser("report", help="ABC, EOQ, reorder point and status table")
    p.add_argument("file")
    p.add_argument("--format", choices=("text", "csv", "json"), default="text")

    p = sub.add_parser("counts", help="staggered cycle-count schedule for the year")
    p.add_argument("file")

    p = sub.add_parser("sweep", help="safety stock at several service levels")
    p.add_argument("file")
    p.add_argument("--levels", type=arg_levels, default=[0.90, 0.95, 0.99],
                   help="comma-separated percentages (default 90,95,99)")

    p = sub.add_parser("eoq", help="economic order quantity for one item")
    p.add_argument("--annual", type=arg_number, required=True, help="units per year")
    p.add_argument("--cost", type=arg_number, required=True, help="unit cost")
    p.add_argument("--ordercost", type=arg_number, required=True, help="cost per order")
    p.add_argument("--holding", type=arg_fraction, default=0.25, help="holding rate (default 25%%)")
    p.add_argument("--pack", type=int, default=1, help="pack size to round to")
    p.add_argument("--moq", type=int, default=1, help="minimum order quantity")
    p.add_argument("--days", type=int, default=365, help="days per year (default 365)")

    p = sub.add_parser("gate", help="CI gate: fail on out-of-stock or thin cover")
    p.add_argument("file")
    p.add_argument("--min-cover", type=arg_number, default=0.0,
                   help="fail when days of cover fall below this")
    return parser


def cmd_eoq(args: argparse.Namespace) -> int:
    if args.cost <= 0:
        print("stockrank: --cost must be above 0", file=sys.stderr)
        return EXIT_INVALID
    if args.pack < 1 or args.moq < 1 or args.days < 1:
        print("stockrank: --pack, --moq and --days must be at least 1", file=sys.stderr)
        return EXIT_INVALID
    unit_holding = args.cost * args.holding
    eoq = economic_order_quantity(args.annual, args.ordercost, unit_holding)
    qty = round_order(eoq, args.pack, args.moq)
    order_cost, carry_cost = yearly_costs(args.annual, args.ordercost, unit_holding, qty, 0.0)
    print(f"EOQ: {eoq:,.2f} -> order {qty:,} (pack {args.pack}, moq {args.moq})")
    if args.annual > 0:
        orders = args.annual / qty
        print(f"Orders per year: {orders:.2f} (every {args.days / orders:.1f} days)")
    print(f"Ordering cost per year: {order_cost:,.2f}")
    print(f"Carrying cost per year: {carry_cost:,.2f}")
    print(f"Total per year: {order_cost + carry_cost:,.2f}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "eoq":
            return cmd_eoq(args)
        plan = load_plan(args.file)
    except LoadError as exc:
        print(exc, file=sys.stderr)
        return EXIT_INVALID

    if args.command == "check":
        print(f"OK: {args.file} has {len(plan.items)} item(s) and no problems")
        return EXIT_OK

    rows = analyse(plan)

    if args.command == "report":
        if args.format == "csv":
            print(report_csv(rows), end="")
        elif args.format == "json":
            print(report_json(rows))
        else:
            print(render_text_report(plan, rows))
        return EXIT_OK

    if args.command == "counts":
        body = []
        for row, interval, days in count_schedule(rows, plan.settings):
            shown = ", ".join(str(day) for day in days[:6])
            if len(days) > 6:
                shown += ", ..."
            body.append([row.item.sku, row.cls, f"every {interval}", str(len(days)), shown])
        print(render_table(["SKU", "CLS", "INTERVAL", "COUNTS/yr", "DAYS OF YEAR"], body))
        return EXIT_OK

    if args.command == "sweep":
        body = []
        for level, units, value in sweep(plan, args.levels):
            body.append([f"{level * 100:g}%", f"{units:,.1f}", fmt(round(value, 2))])
        print(render_table(["SERVICE", "SAFETY UNITS", "SAFETY VALUE"], body))
        return EXIT_OK

    if args.command == "gate":
        failures = gate_failures(rows, args.min_cover)
        for failure in failures:
            print(f"FAIL {failure}")
        if failures:
            print(f"Gate failed: {len(failures)} item(s)")
            return EXIT_GATE
        print(f"Gate passed: {len(rows)} item(s) checked")
        return EXIT_OK

    parser.error(f"unknown command {args.command}")
    return EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
