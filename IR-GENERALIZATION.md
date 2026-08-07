# Generalizing the Policy Checker IR

A design note. The target is the 25 low-level functionalities catalogued by Papastergios,
Ehrlinger and Gounaris, *Unfolding Data Quality Dimensions in Practice: A Survey* (ACM JDIQ
18.1, 2026; arXiv 2507.17507) across seven open-source DQ tools — four of which
(**dbt Core, Deequ, GX, Soda Core**) this pipeline already targets.

Using their taxonomy rather than our own matters: it is external, peer-reviewed, and it turns
"the IR is extensible" into a coverage number we do not get to define.

## Where the current substrate stops

The IR today commits to one shape:

```
Filter → Measure → Compare → Report
         ↑                 ↑
    scalar over a      scalar OP literal
    single relation
```

Everything else is a special case bolted to that: `Resolve → CodeList` exists only for
reference standards, `comparedAttribute` exists only for pairwise equality, `groupBy`
collapses to a worst-case aggregate. Each was the right local move; together they say the
substrate is too narrow.

## Coverage against the 25

| Bucket | Functionalities | n | Status |
|---|---|---|---|
| **Fits today** — scalar over one relation | f-01(set) f-02 f-03 f-04 f-07 f-15 f-16 f-17 f-19 | 9 | shape covered; 6 metrics written |
| **Needs row predicate + explicit aggregate** | f-05 f-06 f-08 f-12 f-24 | 5 | axis B |
| **Needs typed measurement outputs** | f-13 f-14 f-18 f-20 f-21 f-22 | 6 | axis A |
| **Needs schema as the subject** | f-09 f-10 | 2 | axis D |
| **Needs multi-asset join** | f-11 | 1 | axis E (this is B6) |
| **Needs a historical baseline** | f-23 f-25 | 2 | axis C |

So the honest position: **the current substrate can reach 9 of 25**, and four structural
generalizations reach the rest. Note none of them is a rewrite — every one generalizes
something the IR already special-cases.

## The four axes

### A. Typed measurement outputs

`MeasureValue` is implicitly a scalar. Histogram, quantile, most-common and correlation
checks are not scalars.

```
pc:MeasureValue  →  pc:Scalar | pc:Vector | pc:Distribution | pc:Set | pc:Matrix | pc:Mask
```

`Compare` then dispatches on the measured type instead of assuming `value OP literal`. This
is the axis that costs the most across engines, because each has its own way of comparing a
distribution.

### B. Row predicate + explicit aggregate — *the highest-value one*

Today a metric like `AttributeAgreementRate` computes the row-wise comparison **and** the
aggregation in one opaque step. Splitting them:

```
Filter → RowPredicate → Aggregate → Compare → Report
             ↓              ↓
           pc:Mask      pc:Scalar
```

That single change:

- unlocks f-05 (row comparison), f-06 (SQL expression), and most of f-24
- makes **B4 a special case** rather than a bespoke metric — `comparedAttribute` becomes
  one predicate among many, not its own shape
- makes the aggregation explicit, so `all`, `any`, `rate` and `count` stop being baked into
  each metric's name
- makes `groupBy` compose naturally: aggregate per partition, then aggregate the partitions

It also matches the survey's own granularity axis: their **Row** level is exactly a
predicate over rows that something then aggregates.

### C. Reference operands as first-class

`Resolve → CodeList` is a reference operand with the general case hardcoded away. Generalize:

```
pc:Reference  →  literal | codeList | attribute | asset | declaredDistribution | priorMeasurement
```

`priorMeasurement` is the interesting one — it makes the provenance graph an *input*, which
is what f-23 (anomaly) and f-25 (drift) need. The measurements already land in the graph;
nothing currently reads them back.

### D. Subject of measurement

Load yields a Table. f-09 and f-10 measure the **schema**, not the rows:

```
pc:Load → pc:Table          (rows)
pc:Load → pc:Schema         (columns, types, order)
```

Cheap, and it closes two functionalities.

### E. Multi-input Measure with an explicit Join

f-11 and B6 are the same requirement. A workflow gains a second Load and a `pc:Join`; Measure
takes two Table inputs. This is the only axis that changes the **plan skeleton** rather than
adding a parameter, which is why it has been deferred so far.

## Status: axis B is done

`dqvp:rowOperator` splits the record-level comparison from the threshold comparison,
and the plan gained a `RowPredicate -> Aggregate` skeleton (P5). Measured by
`coverage_survey.py`:

```
implemented (a metric exists, rendered by >=1 engine) :  5 / 25
shape-covered (config + templates, no new rule)       :  7 / 25
reachable without structural change                   : 12 / 25
```

f-05 and f-06 moved from unreachable to implemented. `dqvp:AttributeAgreementRate`
is now the special case it always was — `RowComparison` with the record operator
fixed to `odrl:eq`.

**Splitting the two operators surfaced a barrier the earlier constructs had hidden.**
*"The discharge date must not precede the admission date in at least 99% of records"*
needs two comparisons — `>=` within each record, `>= 99` on the resulting rate — and
`odrl:Constraint` provides one `odrl:operator` slot. Every pairwise metric before this
had frozen its record-level comparison into the metric name, which is why equality was
the only one available. Call it **B7, predicate-threshold conflation**.

Engine support, computed rather than asserted:

| record operator | dbt | Soda | Deequ | GX |
|---|---|---|---|---|
| eq, gt, gteq | yes | yes | yes | direct |
| lt, lteq | yes | yes | yes | columns swapped |
| neq | yes | yes | yes | **refused** |

GX has no column-pair inequality expectation, so the template emits
`NOT EXPRESSIBLE` rather than a check that looks right and tests something else.

Two type assumptions leaked out while doing this, both now fixed: `measure_op()` and
the derived render-value rules were keyed on the `pc:Measure` *class* rather than on
`pc:metric`, so every row-predicate check was silently starved of `mostly` and
`complement`. Shapes multiply; keying on the property rather than the class is what
keeps them from having to.

## Status: axis D is done

`pc:Describe` derives a `pc:Schema` from the loaded Table, and a `SchemaShape` branch
measures it — no Filter, because a row predicate over a schema is meaningless.
`dqvp:expectedAttribute` (repeatable) and `dqvp:expectedDatatype` (an XSD IRI, mapped
per engine) carry the requirement.

```
implemented (a metric exists, rendered by >=1 engine) :  7 / 25
shape-covered (config + templates, no new rule)       :  7 / 25
reachable without structural change                   : 14 / 25
```

Verified to discriminate rather than trivially pass: 4-of-4 expected attributes gives
100 and SATISFIED, 3-of-4 gives 75 and VIOLATED; `dateOfBirth` as `xsd:date` gives 100,
as `xsd:integer` gives 0.

**And it immediately found something.** Running the same policy over CSV, JSON and
Parquet, the schema check reports 0 on Parquet: that file declares `dateOfBirth` as
`VARCHAR` while CSV and JSON inference give `DATE`. A real inconsistency between three
serializations of one dataset, caught by the check.

That forces a qualification on the format-invariance claim, and a useful one.
**Record-level metrics are format-invariant; schema metrics are not, and cannot be** —
their subject is precisely what a serialization changes. The end-to-end suite now
asserts invariance for record metrics and asserts the *divergence* for the schema
metric, rather than pretending the two behave alike.

Deequ has no set-of-columns constraint, so `SchemaConformance` refuses there — a third
independent `NOT EXPRESSIBLE`, alongside GX's missing column-pair inequality and the
per-group evaluation gap.

## Status: axis C is done

`pc:Recall` reads the measurement history, `pc:Deviate` compares the current
measurement against it, and the pair is **spliced between Measure and Compare** rather
than being a shape — so it composes with any scalar metric, the way `groupBy` does. A
metric plus a baseline is a drift requirement, whatever the metric.

```
implemented (a metric exists, rendered by >=1 engine) :  8 / 25
shape-covered (config + templates, no new rule)       :  8 / 25
reachable without structural change                   : 16 / 25
```

Demonstrated across three runs: no history → **no verdict** (a drift check on a first
run has nothing to say, and saying nothing is the honest answer); unchanged data →
deviation 0.0, SATISFIED; 6% of the identifiers blanked → deviation 6.0 against a
tolerance of 2, VIOLATED.

The history is keyed on **metric, asset, column and grouping** rather than on the
metric alone — `c-completeness` and `c-percountry` both measure `NonNullRate` of the
same column on the same asset, but one is global and the other the worst partition, and
mixing their histories would silently corrupt both baselines.

### The asymmetry worth noticing

Of the four engines, only **Deequ** can express this, and only with a configured
`MetricsRepository`. GX evaluates a batch against declared bounds. SodaCL's
change-over-time checks need Soda Cloud. In dbt, comparing against previous runs is a
snapshot or incremental *pipeline pattern*, not a test.

The reference executor evaluates it without difficulty, because it holds the provenance
graph — the measurements were always being written, nothing had ever read them back.

**That is the strongest argument yet for the contract layer.** Here is a class of
requirement that three of four engines cannot state at all, expressed once in the
policy and evaluated against a history the framework already had. Not a translation
convenience — an expressiveness gain over every engine it targets.

## Status: axis E is done

The first requirement whose plan is not a chain over one table. The workflow gains a
**second Load**, for the reference asset, and `Measure` takes both tables.

```
implemented (a metric exists, rendered by >=1 engine) :  9 / 25
shape-covered (config + templates, no new rule)       :  8 / 25
reachable without structural change                   : 17 / 25
```

`c-enc-patient` measures **96.0%** of encounter identifiers resolving into Patient
Summary, against a 99% threshold — VIOLATED, and exactly the orphan rate the encounter
data was seeded with.

### What kept the three-level model intact

The obvious move is to let a workflow simply *have* two assets. That would have
dissolved the invariant that makes a workflow deployable, so instead:

- `pc:onAsset` stays **single** — the asset being validated, and where the check runs
- `pc:referencesAsset` declares **what else the check must be able to read**

One of the two assets is the subject and the other is a lookup, and collapsing them
loses that distinction along with any idea of where to run the thing. The suite now
asserts `loads == 1 + |referencesAsset|` rather than `loads == 1`.

**No `pc:Join` operation, contrary to the original sketch.** For this functionality the
join *is* the measurement, so an intermediate joined table would add a node without
adding expressiveness. A Join earns its place when something needs the joined rows
themselves — comparing aggregates across two assets, say — and not before.

### Engine split

dbt renders a subquery; SodaCL has a first-class `values in (x) must exist in y (z)`
check. **GX and PyDeequ both refuse**: a GX suite validates one batch and a PyDeequ
constraint is scoped to one DataFrame, so neither can reach a second asset from inside
a check.

## Status: axis A — the design was wrong

Axis A was specified as typed measurement outputs plus a polymorphic `Compare`:

```
pc:MeasureValue → pc:Scalar | pc:Vector | pc:Distribution | pc:Set | pc:Matrix
```

**That was a modelling error, and working through the six functionalities exposed it.**

A constraint has to reach a verdict. Reaching a verdict from a distribution means
*summarising* it. So the summarisation is part of the requirement, not part of the
comparison — and it belongs in the metric, where the existing scalar machinery applies
unchanged:

| Looked like | Is actually |
|---|---|
| f-14 a quantile **vector** | a *named* quantile — `dqvp:quantile 0.5` — a scalar in the attribute's units |
| f-18 a ranked **set** | the share the top value holds — `ModeShare`, a percentage |
| f-20 a **set** of encodings | the rate of records not disguising absence — `ExplicitNullRate` |
| f-21 a correlation **matrix** | pairwise correlation is a scalar; nobody states a requirement over a whole matrix |
| f-22 a mutual-information **matrix** | same — pairwise, same shape as `Correlation` |

No new artifact types, no `Compare` redesign, no engine-wide change. Four new metrics
and a `PairColumnShape` that already existed.

```
implemented (a metric exists, rendered by >=1 engine) : 13 / 25
shape-covered (config + templates, no new rule)       :  9 / 25
reachable without structural change                   : 22 / 25
```

Executed against the encounter data: median length of stay **6** days (≤10, satisfied);
mode share of referring unit **26%** (≤40, satisfied); explicit-null rate **95%** —
violated, catching the 5% of records that write `"N/A"` instead of a null; correlation
between age and length of stay **0.834** (≥0.7, satisfied).

**What genuinely resists reduction is f-13.** Comparing a whole histogram against a
reference distribution needs that reference *as a structured operand* — which is B2
again, at a larger scale. One functionality out of twenty-five, and it is an operand
problem rather than a measured-value problem.

The lesson generalises past this project: **before adding a type system to an IR, check
whether the values you think you need are ever actually consumed.** Here they never
were — every one of them was on its way to a boolean.

## Proposed increment order

Ordered by unlock per unit of work, not by ambition:

| # | Axis | Unlocks | Touches | Rough size |
|---|---|---|---|---|
| ~~1~~ | ~~**B** row predicate + aggregate~~ | **done** — 9/25 → 12/25 | | |
| ~~2~~ | ~~**D** schema subject~~ | **done** — 12/25 → 14/25 | | |
| ~~3~~ | ~~**C** reference operands~~ | **done** — 14/25 → 16/25 | | |
| ~~4~~ | ~~**E** join~~ | **done** — 16/25 → 17/25, and closes B6 | | |
| ~~5~~ | ~~**A** typed outputs~~ | **dissolved** — 17/25 → 22/25 by summarising in the metric instead | | |

All five are settled: four built, one dissolved. Coverage went **9/25 → 22/25**, and the
last increment cost the least because it turned out to need no structure at all.

What is left is three functionalities and two reasons:

| Left | Why | Would need |
|---|---|---|
| f-13 histogram | the *reference distribution* is a structured operand | a way to carry a non-scalar right operand — B2 at a larger scale |
| f-08 ordering, f-12 interval overlap | the predicate is over an ordered *window*, not one record | `RowPredicate` extended with an ordering and a lag — call it B+ |

Neither is a gap in the measured-value model, which is what the axes were about. Engine
support diverges most sharply here too, so these are the best remaining source of
`NOT EXPRESSIBLE` results rather than a failure to implement.

## What this buys, as a claim

Right now: *"the IR is extensible."* Unfalsifiable.

After: *"of the 25 functionalities characterised by Papastergios et al. across seven tools,
our IR expresses N, refuses M as inexpressible for a given engine, and K fall outside a
scalar-over-relations substrate — and here is the harness that computes those numbers."*

The refusals are not a weakness in that sentence. A shared semantics that can only say *yes*
is a translation layer; one that can say *this engine cannot express this requirement* is a
characterisation of the landscape, and it is machine-checkable rather than a table in a paper.

## Scope honesty

The survey notes that the vast majority of these tools assess **relational data only** — not
streams or graphs. So a relational substrate is not a limitation peculiar to this design; it
is where the field is. Streaming (and the scheduling gap that goes with it) is a frontier
shared with everyone, and should be stated that way rather than apologised for.
