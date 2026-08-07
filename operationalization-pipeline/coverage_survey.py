"""Coverage of the IR against an external functionality taxonomy.

Target: the 25 low-level data quality functionalities catalogued by Papastergios,
Ehrlinger and Gounaris, "Unfolding Data Quality Dimensions in Practice: A Survey"
(ACM JDIQ 18.1, 2026; arXiv:2507.17507), across seven open-source tools - four of
which (dbt Core, Deequ, GX, Soda Core) this pipeline targets.

Using their taxonomy rather than one of our own is the point: it is external and
peer-reviewed, so the resulting number is not one we get to define.

Each functionality carries a status:

  implemented  a metric exists, and templates render it for at least one engine
  shape        the substrate covers it; adding it is a config row plus template
               rows, with no new reasoning rule
  axis:X       needs a named structural generalization (see IR-GENERALIZATION.md)
  out          outside a relational, scalar-over-relations substrate

Claims of `implemented` are verified against the profile and the templates rather
than trusted, so this file cannot drift from the code without failing.

Run:  python3 coverage_survey.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from rdflib import Graph

import codegen as C

# id, short name, status, metric (if any), note
FUNCTIONALITIES = [
    ("f-01", "values fall within range or set", "shape", "ReferenceConformance",
     "set variant is ReferenceConformance; the range variant needs a structured "
     "right operand (min+max), which is B2 in a new guise"),
    ("f-02", "string values are of right length", "shape", None,
     "scalar rate over one column"),
    ("f-03", "values comply with regex", "shape", None,
     "scalar rate over one column"),
    ("f-04", "timestamp values are recent", "shape", None,
     "scalar over one column; the freshness metric exists in the workflow repo"),
    ("f-05", "row values satisfy comparison", "implemented", "RowComparison",
     "axis B: record-level operator, then aggregation"),
    ("f-06", "row satisfies SQL expression", "implemented", "RowExpressionRate",
     "axis B, with an engine-syntax predicate"),
    ("f-07", "simple descriptive statistic checks", "shape", None,
     "scalar over one column (mean, min, max, stddev)"),
    ("f-08", "values satisfy ordering", "axis:B+", None,
     "record-level, but over an ordered window rather than a single record"),
    ("f-09", "columns match schema", "implemented", "SchemaConformance",
     "axis D: Describe derives a Schema from the Table, and the metric measures it"),
    ("f-10", "data types match schema", "implemented", "AttributeTypeConformance",
     "axis D: expected datatype carried as an XSD IRI, mapped per engine"),
    ("f-11", "matching between tables", "axis:E", None,
     "two assets and a join; this is also B6"),
    ("f-12", "adjacent intervals do not overlap", "axis:B+", None,
     "record-level over an ordered window"),
    ("f-13", "histogram checks", "axis:A", None,
     "measured value is a distribution, not a scalar"),
    ("f-14", "quantiles checks", "axis:A", None,
     "measured value is a vector"),
    ("f-15", "checks on the number of elements", "implemented", "RowCount",
     "table-level scalar, absolute threshold"),
    ("f-16", "distinct elements checks", "shape", None,
     "scalar over one column"),
    ("f-17", "unique elements checks", "implemented", "UniqueRate",
     "scalar over one column"),
    ("f-18", "most common elements checks", "axis:A", None,
     "measured value is a ranked set"),
    ("f-19", "missing elements checks", "implemented", "NonNullRate",
     "NullRate is the same measurement under the Consistency dimension"),
    ("f-20", "checks on representation for missing", "axis:A", None,
     "measured value is a set of encodings"),
    ("f-21", "correlation checks", "axis:A", None,
     "measured value is a matrix, or a scalar for one pair"),
    ("f-22", "mutual information checks", "axis:A", None,
     "measured value is a matrix"),
    ("f-23", "anomaly detection", "axis:C", None,
     "needs a baseline drawn from prior measurements"),
    ("f-24", "target labels/values do not conflict", "shape", None,
     "expressible per case as a row predicate; not a single reusable metric"),
    ("f-25", "data drift detection", "axis:C", None,
     "needs a reference distribution, declared or historical"),
]

# The four engines this pipeline targets, all within the survey's seven.
ENGINES = ["dbt", "deequ", "gx", "sodacl"]


def verify_implemented():
    """An `implemented` claim must name a metric that the profile defines and
    that at least one engine template can render."""
    prof = Graph().parse(HERE.parent / "dqvp.ttl", format="turtle")
    tmpl = Graph().parse(HERE / "templates.ttl", format="turtle")
    defined = {C.local(m) for m in prof.subjects(None, None)}
    rendered = {C.local(m) for m in tmpl.objects(None, C.TMPL.metric)}

    problems = []
    for fid, name, status, metric, _ in FUNCTIONALITIES:
        if status != "implemented":
            continue
        if metric is None:
            problems.append(f"{fid}: marked implemented but names no metric")
        elif metric not in defined:
            problems.append(f"{fid}: dqvp:{metric} is not defined in the profile")
        elif metric not in rendered:
            problems.append(f"{fid}: dqvp:{metric} has no engine template")
    return problems


def engine_support(metric):
    """Which of the four engines have a template for this metric."""
    tmpl = Graph().parse(HERE / "templates.ttl", format="turtle")
    have = set()
    for t in tmpl.subjects(C.TMPL.body, None):
        m = tmpl.value(t, C.TMPL.metric)
        if m is not None and C.local(m) == metric:
            have.add(str(tmpl.value(t, C.TMPL.engine)))
    return sorted(have)


def main():
    print("=" * 92)
    print("IR coverage against Papastergios et al. (ACM JDIQ 18.1, 2026) — 25 functionalities")
    print("=" * 92)
    print(f"  {'id':6s} {'functionality':40s} {'status':11s} metric / needs")
    print("  " + "-" * 88)

    counts = {}
    for fid, name, status, metric, note in FUNCTIONALITIES:
        counts[status] = counts.get(status, 0) + 1
        detail = f"dqvp:{metric}" if metric else note[:40]
        print(f"  {fid:6s} {name[:40]:40s} {status:11s} {detail}")

    impl = counts.get("implemented", 0)
    shape = counts.get("shape", 0)
    axes = {k: v for k, v in counts.items() if k.startswith("axis")}

    print("\n  " + "-" * 88)
    print(f"  implemented (a metric exists, rendered by >=1 engine) : {impl:2d} / 25")
    print(f"  shape-covered (config + templates, no new rule)       : {shape:2d} / 25")
    print(f"  reachable without structural change                  : {impl + shape:2d} / 25")
    for a in sorted(axes):
        print(f"  needs {a:<15s}                                : {axes[a]:2d} / 25")

    print("\n  engine coverage of the implemented metrics:")
    for fid, name, status, metric, _ in FUNCTIONALITIES:
        if status == "implemented":
            have = engine_support(metric)
            missing = [e for e in ENGINES if e not in have]
            flag = "" if not missing else f"   (no template: {', '.join(missing)})"
            print(f"    {fid}  dqvp:{metric:22s} {len(have)}/4 engines{flag}")

    print("\n  Note: an engine template may still refuse a particular case at render")
    print("  time — GX has no column-pair expectation for neq, for instance. Those")
    print("  refusals are visible in the generated code, not in this table.")

    problems = verify_implemented()
    print("\n" + "=" * 92)
    if problems:
        print("VERIFICATION FAILED — this table disagrees with the code:")
        for p in problems:
            print(f"  - {p}")
        print("=" * 92)
        return 1
    print(f"verified: every `implemented` claim resolves to a defined, rendered metric")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
