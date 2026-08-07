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

## Proposed increment order

Ordered by unlock per unit of work, not by ambition:

| # | Axis | Unlocks | Touches | Rough size |
|---|---|---|---|---|
| ~~1~~ | ~~**B** row predicate + aggregate~~ | **done** — 9/25 → 12/25 | | |
| 2 | **D** schema subject | 2 | 1 rule, 1 artifact type, 4 templates | small |
| 3 | **C** reference operands | 2 | vocabulary + Resolve generalization + executor reads back measurements | medium |
| 4 | **E** join | 1 (and B6) | plan skeleton, executor multi-table | medium-large |
| 5 | **A** typed outputs | 6 | Compare dispatch across every engine | large |

Doing 1–4 takes coverage from **9/25 to 19/25**. Axis A is the long tail and the one where
engine support genuinely diverges — which makes it the best source of `NOT EXPRESSIBLE`
results rather than a failure to implement.

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
