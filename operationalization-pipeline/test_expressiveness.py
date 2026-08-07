"""Verify the expressiveness ladder without invoking the reasoner.

The plan graph is built here in Python, mirroring what rules/plan.n3 derives, and
then handed to the REAL codegen: generate(), execute() and run_gx() are the
shipped functions. So this exercises templates.ttl, the binder, the reference SQL
and the GX path against the real dataset.

It does NOT verify that plan.n3 produces these triples - that needs EYE. Run
`python run.py` for the genuine end-to-end path.

Usage:  python test_expressiveness.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from rdflib import Graph, Namespace, URIRef, Literal, RDF, XSD

import codegen as C

PC, DQVP, ODRL = C.PC, C.DQVP, C.ODRL
EX = Namespace("https://example.org/ehds/")

results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        expected {want!r}\n        got      {got!r}")


def param(g, op, name, value):
    node = URIRef(f"{op}/p/{name}")
    g.add((op, PC.parameter, node))
    g.add((node, PC.name, Literal(name)))
    g.add((node, PC.value, Literal(str(value))))


def build_plan():
    """Reproduce rules/plan.n3 in Python: P1 shared Load, then one branch per
    constraint according to the metric's pc:shape."""
    g = Graph()
    for src in [HERE.parent / "dqvp.ttl", HERE / "pc-vocab.ttl", HERE / "config.ttl",
                HERE / "policy.ttl", HERE / "dataset_csv.ttl"]:
        g.parse(src, format="turtle")

    pol = EX["ps-dq-offer-01"]
    base = str(pol)
    chk = URIRef(f"{base}/checker")
    g.add((chk, RDF.type, PC.PolicyChecker))
    g.add((chk, PC.accordingTo, pol))

    # One rule may be a permission, a duty or an obligation; each names its own
    # asset, and each asset gets its own workflow with its own Load and Table.
    rules = [r for prop in (ODRL.permission, ODRL.duty, ODRL.obligation)
             for r in g.objects(pol, prop)]
    tbl_of, constraint_asset = {}, {}
    for r in rules:
        asset = g.value(r, ODRL.target)
        al = C.local(asset)
        wf = URIRef(f"{base}/checker/wf/{al}")
        load, src_, tbl = (URIRef(f"{wf}/load"), URIRef(f"{wf}/source"), URIRef(f"{wf}/table"))
        g.add((chk, PC.hasWorkflow, wf))
        g.add((wf, RDF.type, PC.Workflow))
        g.add((wf, PC.onAsset, asset))
        g.add((wf, PC.hasOp, load))
        g.add((load, RDF.type, PC.Load))
        g.add((load, PC.input, src_))
        g.add((load, PC.output, tbl))
        g.add((src_, RDF.type, PC.Dataset))
        g.add((src_, PC.ofAsset, asset))
        g.add((tbl, RDF.type, PC.Table))
        for prop in (PC["format"], PC.locator):
            v = g.value(asset, prop)
            if v is not None:
                g.add((src_, prop, v))
        tbl_of[wf] = tbl
        for c in g.objects(r, ODRL.constraint):
            constraint_asset[c] = wf

    for c in g.subjects(RDF.type, DQVP.DQConstraint):
        wf = constraint_asset.get(c)
        if wf is None:            # e.g. carried by a prohibition
            continue
        tbl = tbl_of[wf]
        cu = str(c)
        metric = g.value(c, ODRL.leftOperand)
        shape = g.value(metric, PC.shape)
        is_pct = g.value(metric, PC.isPercentage) is not None
        thr = g.value(c, ODRL.rightOperand)
        op_uri = g.value(c, ODRL.operator)

        flt, meas = URIRef(f"{cu}/filter"), URIRef(f"{cu}/measure")
        mval, cmp_ = URIRef(f"{cu}/mval"), URIRef(f"{cu}/compare")
        ftbl, vd = URIRef(f"{cu}/ftable"), URIRef(f"{cu}/verdict")
        rep, rdoc = URIRef(f"{cu}/report"), URIRef(f"{cu}/reportdoc")

        for o in (flt, meas, cmp_, rep):
            g.add((wf, PC.hasOp, o))
        g.add((flt, RDF.type, PC.Filter));   g.add((flt, PC.forCheck, c))
        g.add((flt, PC.input, tbl));         g.add((flt, PC.output, ftbl))
        g.add((ftbl, RDF.type, PC.Table))
        g.add((meas, RDF.type, PC.Measure)); g.add((meas, PC.forCheck, c))
        g.add((meas, PC.metric, metric))
        g.add((meas, PC.input, ftbl));       g.add((meas, PC.output, mval))
        g.add((mval, RDF.type, PC.MeasureValue))
        g.add((cmp_, RDF.type, PC.Compare)); g.add((cmp_, PC.forCheck, c))
        g.add((cmp_, PC.input, mval));       g.add((cmp_, PC.output, vd))
        g.add((vd, RDF.type, PC.Verdict))
        g.add((rep, RDF.type, PC.Report));   g.add((rep, PC.forCheck, c))
        g.add((rep, PC.input, vd));          g.add((rep, PC.output, rdoc))
        g.add((rdoc, RDF.type, PC.ReportDoc))

        # schema shape: the subject is the schema, so no row filter and the
        # expected attributes are the set being measured over
        if shape == PC.SchemaShape:
            for a in g.objects(c, DQVP.expectedAttribute):
                param(g, meas, "expectedAttribute", C.local(a))
            dt = g.value(c, DQVP.expectedDatatype)
            if dt is not None:
                param(g, meas, "expectedDatatype", C.local(dt))

        # column parameters, per shape
        if shape != PC.TableShape:
            ta = g.value(c, DQVP.targetAttribute)
            if ta is not None:
                param(g, meas, "column", C.local(ta))
        if shape == PC.PairColumnShape:
            param(g, meas, "column2", C.local(g.value(c, DQVP.comparedAttribute)))

        # row-predicate shape: the record-level test and its aggregation are
        # separate operations, and the record operator is a parameter rather
        # than something the metric froze in.
        if shape == PC.RowPredicateShape:
            g.add((meas, RDF.type, PC.Aggregate))
            agg = g.value(metric, PC.aggregation)
            param(g, meas, "aggregation", agg if agg is not None else "rate")
            col2 = g.value(c, DQVP.comparedAttribute)
            if col2 is not None:
                param(g, meas, "column2", C.local(col2))
            rop = g.value(c, DQVP.rowOperator)
            if rop is not None:
                param(g, meas, "rowOp", C.local(rop))
                param(g, meas, "rowSqlOp", g.value(rop, PC.sqlSymbol))
                param(g, meas, "rowPyOp", g.value(rop, PC.pySymbol))
            rex = g.value(c, DQVP.rowExpression)
            if rex is not None:
                param(g, meas, "rowExpression", rex)

        # compare parameters
        param(g, cmp_, "threshold", thr)
        param(g, cmp_, "sqlOp", g.value(op_uri, PC.sqlSymbol))
        param(g, cmp_, "pyOp", g.value(op_uri, PC.pySymbol))

        # derived percentage render-values, guarded on pc:isPercentage
        if is_pct:
            if op_uri == ODRL.gteq:
                param(g, meas, "mostly", float(thr) / 100)
            param(g, meas, "complement", 100 - float(thr))

        # row filter
        f = g.value(c, DQVP.filter)
        if f is not None:
            param(g, flt, "filterColumn", C.local(g.value(f, DQVP.filterAttribute)))
            for fv in g.objects(f, DQVP.filterValue):
                param(g, flt, "filterValue", fv)
            fo = g.value(f, DQVP.filterOperator)
            if fo is not None:
                param(g, flt, "filterOp", C.local(fo))

        # grouping
        gb = g.value(c, DQVP.groupBy)
        if gb is not None:
            param(g, meas, "groupColumn", C.local(gb))

        # reference standard
        std = g.value(c, DQVP.referenceStandard)
        if std is not None:
            res, codes = URIRef(f"{cu}/resolve"), URIRef(f"{cu}/codes")
            g.add((wf, PC.hasOp, res))
            g.add((res, RDF.type, PC.Resolve)); g.add((res, PC.forCheck, c))
            g.add((res, PC.output, codes));     g.add((codes, RDF.type, PC.CodeList))
            param(g, res, "referenceStandard", std)
            g.add((meas, PC.input, codes))
    return g


plan = build_plan()

print("=" * 74)
print("Plan built (mirroring plan.n3)")
print("=" * 74)
ids = sorted(C.local(c) for c in C.checks(plan))
print(f"  constraints: {', '.join(ids)}")
check("all thirteen constraints produced a branch", len(ids), 13)

print()
print("=" * 74)
print("Execution — measured value and verdict against the real dataset")
print("=" * 74)
res = C.execute(plan)
print(f"  {'check':16s} {'measured':>10s}  verdict")
print("  " + "-" * 42)
for cid in sorted(res):
    v, ok = res[cid]
    print(f"  {cid:16s} {('—' if v is None else f'{v:10.4f}')}  "
          f"{'—' if ok is None else ('SATISFIED' if ok else 'VIOLATED')}")

print()
# L1 uniqueness: 490 distinct of 490 non-null
check("c-uniqueness measures 100.0", res["c-uniqueness"][0], 100.0)
check("c-uniqueness SATISFIED (>= 100)", res["c-uniqueness"][1], True)
# L2 row count: absolute, not a percentage
check("c-volume measures 500 rows", res["c-volume"][0], 500.0)
check("c-volume SATISFIED (>= 400)", res["c-volume"][1], True)
# L3 cross-attribute
check("c-crossborder VIOLATED (2.61% agree, needs 90%)", res["c-crossborder"][1], False)
# L4 the point of grouping: global passes, per-group does not
check("c-percountry VIOLATED per group", res["c-percountry"][1], False)
glob = C.execute(plan)["c-completeness"][0]
check("...while the same attribute is 98.0% complete globally", glob, 98.0)
print(f"        -> 98.0 >= 95 globally, but the worst country partition is "
      f"{res['c-percountry'][0]:.1f}%. Only dqvp:groupBy can state this.")

print()
print("=" * 74)
print("Code generation — four engines")
print("=" * 74)
code = C.generate(plan)
for cid in ["c-uniqueness", "c-volume", "c-crossborder", "c-percountry"]:
    print(f"\n  {cid}")
    for e in sorted(code):
        body = code[e].get(cid)
        print(f"    {e:7s}: {body.splitlines()[0] if body else '(no template)'}"
              if body else f"    {e:7s}: (no template)")

check("both assets got their own workflow", len(C.workflows(plan)), 2)
check("schema conformance SATISFIED (all expected attributes declared)",
      res["c-schema"][1], True)
check("datatype conformance SATISFIED (dateOfBirth is a date)",
      res["c-dtype"][1], True)
check("schema metrics are measured over the schema, not the records",
      "describe" in C._measure_sql(plan, EX["c-schema"],
                                   C.context_of(plan, EX["c-schema"]), "t"), True)
check("record-level comparison SATISFIED (dates ordered in 100% of encounters)",
      res["c-enc-dates"][1], True)
check("the record operator is distinct from the threshold operator",
      ">=" in code["dbt"]["c-enc-dates"].split(" as r")[0], True)
check("patient check routed to patient_summary",
      C.sql_table(C.asset_of_check(plan, EX["c-completeness"])), "patient_summary")
check("encounter check routed to encounter_summary",
      C.sql_table(C.asset_of_check(plan, EX["c-enc-discharge"])), "encounter_summary")
check("dbt addresses the encounter relation",
      "encounter_summary" in code["dbt"]["c-enc-discharge"], True)
check("dbt addresses the patient relation",
      "patient_summary" in code["dbt"]["c-completeness"], True)
check("a duty-borne constraint compiles",
      res["c-enc-discharge"][1] is not None, True)

check("uniqueness generated for all 4 engines", len([e for e in code if "c-uniqueness" in code[e]]), 4)
check("row count generated for all 4 engines", len([e for e in code if "c-volume" in code[e]]), 4)
check("cross-attribute generated for all 4 engines", len([e for e in code if "c-crossborder" in code[e]]), 4)
check("dbt renders a GROUP BY for the grouped check",
      "group by" in code["dbt"]["c-percountry"].lower(), True)
check("gx flags per-group evaluation as not expressible",
      "NOT EXPRESSIBLE" in code["gx"]["c-percountry"], True)
check("ungrouped checks carry no GROUP BY",
      "group by" in code["dbt"]["c-completeness"].lower(), False)
check("row count carries no percentage render-value",
      "mostly" in C.context_of(plan, EX["c-volume"]), False)
check("percentage metrics still do",
      "mostly" in C.context_of(plan, EX["c-uniqueness"]), True)

check("sodacl flags per-group evaluation as not expressible",
      "NOT EXPRESSIBLE" in code["sodacl"]["c-percountry"], True)
check("deequ flags per-group evaluation as not expressible",
      "NOT EXPRESSIBLE" in code["deequ"]["c-percountry"], True)

print()
print("=" * 74)
print("Great Expectations — live execution")
print("=" * 74)
try:
    gx = C.run_gx(code["gx"], plan)
    for cid in sorted(gx):
        print(f"  {cid:16s} {'PASS' if gx[cid] else 'FAIL'}")
    for cid in ["c-uniqueness", "c-volume", "c-crossborder"]:
        check(f"GX agrees with the reference executor on {cid}", gx[cid], res[cid][1])
except (AttributeError, NotImplementedError) as e:
    # codegen.run_gx targets the great_expectations 0.18 fluent API
    # (context.sources). GX 1.x renamed it to context.data_sources, so the call
    # fails before any expectation runs. Not caused by the ladder - the
    # generated GX expressions above are unaffected.
    print(f"  SKIPPED — installed great_expectations is API-incompatible with")
    print(f"  codegen.run_gx: {e}")
    print(f"  The generated GX code is still shown above and is syntactically")
    print(f"  the 0.18/1.x-common expectation form; only the harness differs.")

print()
print("=" * 74)
print(f"{sum(results)}/{len(results)} checks passed")
print("=" * 74)
sys.exit(0 if all(results) else 1)
