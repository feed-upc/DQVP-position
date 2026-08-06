# The expressiveness ladder

Four new data quality requirements, each needing strictly more of the profile than the
one before it. The point is not the requirements — it is what each one costs, and which
ones the design absorbs without a new construct.

The position paper identified three barriers to expressing DQRs in plain ODRL and
resolved each with one property. Working up this ladder surfaced two more.

| | Barrier | Construct | Status |
|---|---|---|---|
| B1 | Column identity — `odrl:target` names an asset, not a column | `dqvp:targetAttribute` | in v0.2 |
| B2 | Multi-parameter arity — one `rightOperand` cannot carry standard + threshold | `dqvp:referenceStandard` | in v0.2 |
| B3 | Conditional logic — operators compose whole constraints, not row predicates | `dqvp:RowFilter` | in v0.2 |
| **B4** | **Multi-attribute arity** — a constraint can name only one attribute | **`dqvp:comparedAttribute`** | **added v0.3** |
| **B5** | **Group quantification** — a threshold is only ever evaluated globally | **`dqvp:groupBy`** | **added v0.3** |
| B6 | Cross-asset reference — the plan assumes a single asset and a single Load | `dqvp:referenceAsset` | identified, not implemented |

## The four rungs

All figures are measured against `data/Patient_Summary.csv` (500 records) by
`operationalization-pipeline/test_expressiveness.py`.

### L1 · `c-uniqueness` — a new metric of an existing shape

> The national healthcare patient identifier must be unique in 100% of records.

`dqvp:UniqueRate` over `ScalarColumnShape`. **No new profile property and no new rule** —
exactly what the design promises. Measured 100.0%, SATISFIED.

### L2 · `c-volume` — a table-level metric

> The dataset must contain at least 400 records.

`dqvp:RowCount` over `TableShape`. Two things make this harder than L1: it carries **no
`targetAttribute`** (it is a property of the dataset, not a column), and its threshold is
an **absolute count rather than a percentage**. Needed one new skeleton rule (P3).
Measured 500, SATISFIED.

### L3 · `c-crossborder` — relating two columns of one record

> In at least 90% of records the treating hospital's country must match the patient's
> country of affiliation.

Needs a second attribute, which the profile could not name — **barrier B4**. Resolved with
`dqvp:comparedAttribute` plus a `PairColumnShape` skeleton (P4). Measured 2.61%,
VIOLATED — the synthetic generator assigns `hospitalCountry` independently of
`countryOfAffiliation`, so the data models no domestic-care bias at all.

### L4 · `c-percountry` — a constraint that must hold per partition

> For **every** country of affiliation, the patient identifier must be at least 95% complete.

Needs quantification over partitions — **barrier B5** — resolved with `dqvp:groupBy`.

This rung earns its place because of what it catches:

```
globally           98.0%  >= 95   SATISFIED
worst partition    90.0%  >= 95   VIOLATED    (7 of 32 countries below threshold)
```

The aggregate passes the very threshold that individual partitions fail. Without
`dqvp:groupBy` the requirement cannot be stated, and the global reading silently reports
compliance.

## What each rung actually cost

| | profile | `config.ttl` | `plan.n3` | `templates.ttl` | `codegen.py` |
|---|---|---|---|---|---|
| L1 uniqueness | 1 metric, 1 dimension | 2 rows | — | 4 rows | 1 branch |
| L2 row count | 1 metric | 1 row | **1 new rule** | 4 rows | 1 branch |
| L3 cross-attribute | 1 metric, **1 property** | 2 rows | **1 new rule** + projection | 4 rows | 1 branch |
| L4 group-wise | **1 property** | — | 1 projection, 1 parameter rule | 4 edited | grouping in `_measure_sql` |

## Three findings

**The data-driven claim holds for code generation, not for execution.** `MODEL.md` states
that a new metric of an existing shape touches no rules. That is true of the Jinja
templates — but `codegen._measure_sql` dispatches on metric name in Python, so every new
metric needs a Python branch there regardless. The plan and the code generator are data;
the reference executor is not.

**The derived render-values assumed every metric was a percentage.** The rules computing
`mostly = threshold/100` and `complement = 100 − threshold` fired for any metric with a
threshold. `dqvp:RowCount` would have acquired `mostly = 4.0` from a threshold of 400 —
harmless only because no row-count template reads it. Both rules are now guarded on
`pc:isPercentage`, which `config.ttl` declares per metric.

**Grouping is orthogonal to shape, but engine support for it is not.** `dqvp:groupBy`
needed no new skeleton — like `dqvp:filter` it is a parameter, so it composes with any
existing metric. Engines diverge sharply though: only SQL/dbt expresses universal
quantification over partitions natively (`GROUP BY` + `HAVING`). Great Expectations,
SodaCL and PyDeequ have no per-group form, and would otherwise have silently emitted an
aggregate check — reporting a pass while partitions violate. Those templates now emit an
explicit `NOT EXPRESSIBLE` note instead, following the precedent already set for PyDeequ
and row filters.

For the reference executor, universal quantification collapses to the worst partition:
`∀g. r_g ≥ t` iff `min_g(r_g) ≥ t`, and the maximum for an upper bound. Operators with no
worst case (`eq`, `neq`) are refused rather than silently evaluated globally.

## The next rung

**B6, cross-asset reference.** *Every patient identifier appearing in the Encounters
dataset must exist in Patient Summary.* This breaks an assumption the whole pipeline
rests on: `odrl:permission` names one `odrl:target`, and P1 mints exactly one `Load`.
Referential integrity needs a second asset, a second Load and a join, so it is the first
requirement that changes the **plan skeleton** rather than adding a parameter to it.

## Verified end to end

`run.py` compiles all seven constraints with EYE and executes them (8s on the native
reasoner). Every verdict below comes from the reasoner-derived plan, not from the test
harness:

```
c-completeness   98.0000  VIOLATED     c-crossborder     2.6104  VIOLATED   (L3)
c-compliance     98.1964  SATISFIED    c-percountry     90.0000  VIOLATED   (L4)
c-consistency   100.0000  SATISFIED    c-uniqueness    100.0000  SATISFIED  (L1)
                                       c-volume        500.0000  SATISFIED  (L2)
```

The `column2` and `groupColumn` parameters appear in `artifacts/plan.ttl`, confirming that
rules P3 and P4 fire and that the grouping parameter propagates. All four engines generate
code for all seven checks.

One caveat: `codegen.run_gx` and the generated GX expectations target the Great
Expectations 0.18 fluent API (`validator.expect_column_values_to_*`, `context.sources`),
both removed in GX 1.0. Pin `great_expectations<1.0` to run stage 3's GX cross-check, or
port the `gx` rows in `templates.ttl` to the 1.x `gx.expectations.*` classes. This predates
the ladder and affects the three original constraints equally.

## Reproducing

```bash
cd operationalization-pipeline
python run.py                    # genuine end-to-end, needs EYE on PATH
python test_expressiveness.py    # plan built in Python; verifies codegen + execution
```

`test_expressiveness.py` builds the plan graph itself, mirroring `rules/plan.n3`, and
hands it to the real `generate()` and `execute()`. It therefore verifies the templates,
the binder, the reference SQL and the verdicts — **but not that `plan.n3` derives those
triples**. Only `run.py` does that, and it needs the EYE reasoner.
