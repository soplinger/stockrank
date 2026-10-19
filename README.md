# Stockrank

**Offline inventory planner: one plain-text SKU file in, ABC classes, economic order quantities, reorder points and a CI stock gate out.**

Stockrank reads a readable file of planning settings and one line per SKU, then tells you which items matter most, how much to order, when to reorder, how much safety stock to hold and whether stock is in a state that should fail a build. It is a single Python file with the standard library only: no servers, no accounts, no API keys, no network calls.

## Features

- **Plain-text input** with `set` lines for planning settings and `item` lines for each SKU, so the file can live in git next to your other config.
- **Line-numbered errors** for every problem in the file (bad numbers, unknown fields, duplicate SKUs, missing required fields, cut-offs in the wrong order), reported together rather than one at a time.
- **Pareto ABC classification** by annual usage value, with configurable cut-offs (default 80% and 95%).
- **Economic order quantity** from the Wilson formula, rounded up to your pack size and raised to your minimum order quantity.
- **Safety stock and reorder point** from a service level, daily demand and the standard deviation of daily demand, using the normal distribution.
- **Stock status** per item: `OUT`, `REORDER`, `WATCH`, `OK` or `NO DEMAND`, based on inventory position (on hand plus on order).
- **Days of cover** for each item, so you can see how long current stock lasts.
- **Yearly cost model** showing ordering cost and carrying cost at the order quantity you would actually place.
- **Staggered cycle-count schedule** by class, so counts for many items do not all land on the same day.
- **Service-level sweep** that shows how safety stock units and value grow at 90%, 95%, 99% or any levels you choose.
- **Quick EOQ calculator** for a single item without writing a file.
- **Three report formats**: an aligned text table with totals, CSV for spreadsheets, and JSON for scripts.
- **CI gate** that fails the build on out-of-stock items or cover below a minimum, with distinct exit codes.

## Quick start

From the project directory, check the example file, then look at the plan:

```sh
python3 stockrank.py check examples/shelf.stock
python3 stockrank.py report examples/shelf.stock
```

The example file is a small hardware shelf. Its settings block looks like this:

```text
set service=95% holding=25% cut_a=80% cut_b=95% ordercost=12
set days=365 watch=7 count_a=30 count_b=90 count_c=180

item BOLT-M6-20   annual=4800 cost=0.35 sd=14  lead=10 onhand=260  onorder=0   pack=100 moq=200
```

Each item line reads as: sold 4800 a year, costs 0.35 each, daily demand varies with a standard deviation of 14, takes 10 days to arrive, 260 on the shelf, 0 on order, ordered in packs of 100 with a minimum of 200.

Other things to try:

```sh
python3 stockrank.py counts examples/shelf.stock
python3 stockrank.py sweep examples/shelf.stock --levels 90,95,99
python3 stockrank.py report examples/shelf.stock --format csv
python3 stockrank.py eoq --annual 1000 --cost 8 --ordercost 20 --holding 25% --pack 10
python3 stockrank.py gate examples/shelf.stock --min-cover 14
```

## File format

- Lines starting with `#` are comments; text after `#` on a line is ignored.
- `set` lines hold planning settings and apply to every item. Each key can appear once.
- `item SKU` lines name one SKU and list fields as `key=value`. SKU names may use letters, digits, `.`, `_` and `-`.
- Rates accept a percent sign (`95%`) or a fraction (`0.95`). A bare `95` is rejected with a hint.

| Setting | Default | Meaning |
| --- | --- | --- |
| `service` | 95% | Target probability of not running out during lead time |
| `holding` | 25% | Yearly carrying cost as a share of unit cost |
| `cut_a`, `cut_b` | 80%, 95% | ABC cumulative-value cut-offs |
| `days` | 365 | Days per year, used for daily demand |
| `watch` | 7 | Days of demand above the reorder point before status is `WATCH` |
| `ordercost` | none | Cost per order, used when an item does not set its own |
| `count_a`, `count_b`, `count_c` | 30, 90, 180 | Cycle-count interval in days per class |

| Item field | Required | Meaning |
| --- | --- | --- |
| `annual` | yes | Units sold per year |
| `cost` | yes | Unit cost, above 0 |
| `sd` | yes | Standard deviation of daily demand, in units |
| `lead` | yes | Lead time in days |
| `onhand` | no | Units on the shelf (default 0) |
| `onorder` | no | Units already ordered (default 0) |
| `ordercost` | no | Cost per order; falls back to the `set` value |
| `pack` | no | Order in multiples of this (default 1) |
| `moq` | no | Minimum order quantity (default 1) |

## How it works

- **Daily demand** is `annual / days`.
- **Safety stock** is `z × sd × √lead`, where `z` is the normal quantile for the service level.
- **Reorder point** is `daily × lead + safety stock`.
- **EOQ** is `√(2 × annual × ordercost / (cost × holding))`, rounded up to the pack size and raised to the MOQ.
- **Yearly cost** is `annual / qty × ordercost` for ordering, plus `(qty / 2 + safety stock) × cost × holding` for carrying.
- **Status**: `OUT` when on hand is zero with demand; `REORDER` when inventory position is at or below the reorder point; `WATCH` when it is within `watch` days of demand above that point; otherwise `OK`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success, or the gate passed |
| 1 | The stock file has problems, or a file cannot be read |
| 2 | Bad command-line arguments |
| 3 | The gate failed: an item is out of stock or below `--min-cover` |

## Running the tests

Tests use pytest; the standard library does everything else.

```sh
pip install pytest
python3 -m pytest -q
```

The tests cover the parser (including line-numbered errors), unit handling, ABC classification, EOQ and pack rounding, safety stock and reorder points, cost formulas, stock status rules, the gate, cycle-count staggering, and the command-line exit codes.

## Project layout

```text
stockrank.py          the planner (single file, standard library only)
examples/shelf.stock  sample SKU file used in the quick start
tests/                pytest suite
pytest.ini            test configuration
```

## Notes and limits

- Formulas assume independent, normally distributed daily demand and a fixed lead time. Real demand is rarely that tidy; treat safety stock as a planning estimate.
- The EOQ model assumes steady demand, a fixed order cost and no quantity discounts.
- Stockrank plans from the figures you give it. It does not track receipts or sales over time.
