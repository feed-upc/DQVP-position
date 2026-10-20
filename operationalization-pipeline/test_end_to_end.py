"""Full end-to-end test of the framework, every stage, nothing mocked.

Walks the whole chain a data quality requirement travels:

  pattern catalogue -> DQR json -> DQVP policy -> EYE plan -> engine code
                    -> execution -> DQV provenance

and checks the properties that should hold at each hand-off. Requires the EYE
reasoner on PATH; every stage that can run without it is still checked if EYE is
missing, and the reasoner-dependent ones are reported as skipped.

Run:  python test_end_to_end.py
"""
import json
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from rdflib import Graph, Namespace, RDF

import codegen as C
import dqr_to_dqvp as D

REPO      = HERE.parent                       # the DQVP repo
CATALOGUE = REPO.parent / "ComputationalCatalogues"
DQV       = Namespace("http://www.w3.org/ns/dqv#")

results, stage_no = [], [0]


def stage(title):
    stage_no[0] += 1
    print(f"\n{'=' * 78}\nSTAGE {stage_no[0]}  {title}\n{'=' * 78}")


def check(label, got, want=True):
    ok = got == want
    results.append((ok, label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        expected {want!r}\n        got      {got!r}")
    return ok


def note(msg):
    print(f"        {msg}")


HAVE_EYE = shutil.which("eye") is not None


# ---------------------------------------------------------------- 1. catalogue
stage("Elicitation — the pattern catalogue the requirements instantiate")
if CATALOGUE.exists():
    pats = sorted(p.name for p in (CATALOGUE / "media").iterdir() if p.name.startswith("DQRP"))
    print(f"  patterns: {', '.join(pats)}")
    used = sorted({r["pattern"]["id"] for r in D.load_requirements()}
                  | {r["pattern"]["id"] for r in D.load_requirements(D.REQ_DIR / "heleade")})
    print(f"  instantiated by the requirements: {', '.join(used)}")
    check("catalogue exposes every pattern the requirements instantiate", set(used) <= set(pats))
else:
    note("SKIPPED - needs the pattern catalogue (ComputationalCatalogues) next to this repository")


# ------------------------------------------------------------- 2. requirements
stage("Requirements — instantiated DQRs reference a pattern and carry parameters")
reqs = D.load_requirements()
print(f"  {len(reqs)} requirements")
for r in reqs:
    print(f"    {r['id']:8s} pattern {r['pattern']['id']:6s} "
          f"{r['qualityDimension']['dimension']:14s} {r['statement'][:58]}...")
check("all requirements parse", len(reqs), 6)
check("every requirement names a pattern",
      all(r.get("pattern", {}).get("id") for r in reqs))
check("every requirement names its source entity",
      all(r.get("sourceEntity") for r in reqs))
known = set(D.PATTERN_SPEC)
check("every referenced pattern has a mapping spec",
      sorted({r["pattern"]["id"] for r in reqs} - known), [])


# --------------------------------------------------------- 3. DQR -> DQVP policy
stage("Mapping — DQR json compiles to a DQVP policy")
ttl = D.build_policy(reqs)
mapped = HERE / "artifacts" / "policy_from_dqr.ttl"
mapped.parent.mkdir(exist_ok=True)
mapped.write_text(ttl)
g = Graph().parse(mapped, format="turtle")
DQVP = C.DQVP
cons = list(g.subjects(RDF.type, DQVP.DQConstraint))
print(f"  generated {len(cons)} constraints -> {mapped.relative_to(REPO)}")
check("mapped policy is valid Turtle", len(g) > 0)
check("one constraint per requirement", len(cons), len(reqs))
check("the policy is a dqvp:QualityPolicy",
      len(list(g.subjects(RDF.type, DQVP.QualityPolicy))), 1)
check("B2 referenceStandard survives the mapping",
      len(list(g.subjects(DQVP.referenceStandard, None))) >= 1)
check("B3 RowFilter survives the mapping",
      len(list(g.subjects(RDF.type, DQVP.RowFilter))) >= 1)


# ------------------------------------------------------------------ 4. profile
stage("Profile — DQVP vocabulary is well formed and complete")
prof = Graph().parse(REPO / "dqvp.ttl", format="turtle")
print(f"  dqvp.ttl: {len(prof)} triples")
for name in ["targetAttribute", "referenceStandard", "filter",
             "comparedAttribute", "groupBy"]:
    check(f"property dqvp:{name} is defined",
          (DQVP[name], None, None) in prof)
metrics = sorted(C.local(m) for m in prof.subjects(RDF.type, DQV.Metric))
print(f"  metrics: {', '.join(metrics)}")
# A metric with no shape can never be planned, so this is the invariant that
# matters rather than a count that has to be edited every time one is added.
cfg = Graph().parse(HERE / "config.ttl", format="turtle")
shaped = {C.local(m) for m in cfg.subjects(C.PC.shape, None)}
check("every metric declares a shape in config.ttl",
      sorted(set(metrics) - shaped), [])
check("the row-predicate constructs are defined",
      all((DQVP[n], None, None) in prof for n in ["rowOperator", "rowExpression"]))


# ------------------------------------------------------- 5. planning (reasoner)
stage("Planning — EYE compiles the shipped policy to a Policy Checker")
if not HAVE_EYE:
    note("SKIPPED - eye not on PATH")
    plan = None
else:
    plan = C.build_plan("dataset_csv.ttl")
    PC = C.PC
    checkers = list(plan.subjects(RDF.type, PC.PolicyChecker))
    wfs = C.workflows(plan)
    print(f"  {len(checkers)} PolicyChecker, {len(wfs)} Workflow(s), "
          f"{len(C.checks(plan))} constraints")
    for wf, asset in wfs:
        loads = [o for o in plan.objects(wf, PC.hasOp) if (o, RDF.type, PC.Load) in plan]
        branch = {C.local(plan.value(o, PC.forCheck))
                  for o in plan.objects(wf, PC.hasOp) if plan.value(o, PC.forCheck)}
        print(f"    {C.local(asset):20s} {len(loads)} Load, "
              f"{len(branch)} checks: {', '.join(sorted(branch))}")
    check("exactly one PolicyChecker per policy", len(checkers), 1)
    check("one Workflow per asset", len(wfs), 2)
    # One Load per asset a workflow touches: its own, plus any it must look
    # values up in. The subject asset stays single, which is what keeps the
    # workflow deployable to one place.
    check("every workflow loads exactly the assets it touches",
          all(len(C.loads_of(plan, wf)) ==
              1 + len(list(plan.objects(wf, PC.referencesAsset)))
              for wf, _ in wfs))
    check("a cross-asset workflow declares what it reaches for",
          sorted(C.local(r) for wf, _ in wfs
                 for r in plan.objects(wf, PC.referencesAsset)), ["PatientSummary"])
    check("every workflow names exactly one asset",
          all(len(list(plan.objects(wf, PC.onAsset))) == 1 for wf, _ in wfs))
    check("the checker links to the policy",
          plan.value(checkers[0], PC.accordingTo) is not None)
    # duty-borne constraints must compile, not just permission-borne ones
    check("a duty-borne constraint reaches the plan",
          C.asset_of_check(plan, C.DQVP[""].defrag() and
                           next(c for c in C.checks(plan)
                                if C.local(c) == "c-enc-discharge")) is not None)
    check("every check routed to a workflow",
          all(C.workflow_of_check(plan, c) is not None for c in C.checks(plan)))


# ---------------------------------------------------------------- 6. code gen
stage("Code generation — one plan, four engines")
if plan is None:
    note("SKIPPED - needs a plan")
    code = None
else:
    code = C.generate(plan)
    for e in sorted(code):
        print(f"    {e:7s} {len(code[e])} checks")
    check("all four engines produced code", sorted(code),
          ["dbt", "deequ", "gx", "sodacl"])
    check("dbt covers every check", len(code["dbt"]), len(C.checks(plan)))
    # each engine must address the asset the check belongs to
    for cid, table in [("c-completeness", "patient_summary"),
                       ("c-enc-discharge", "encounter_summary")]:
        check(f"dbt addresses {table} for {cid}", table in code["dbt"][cid])
    check("the cross-asset check names both relations",
          all(t in code["dbt"]["c-enc-patient"]
              for t in ("encounter_summary", "patient_summary")))
    check("engines that cannot reach a second asset refuse",
          sorted(e for e in code if "NOT EXPRESSIBLE" in code[e]["c-enc-patient"]),
          ["deequ", "gx"])
    check("no template hardcodes a relation name",
          "patient_summary" not in (HERE / "templates.ttl").read_text())


# ---------------------------------------------------------------- 7. execution
stage("Execution — verdicts against the real data")
if plan is None:
    note("SKIPPED - needs a plan")
    res = None
else:
    res = C.execute(plan)
    print(f"  {'check':18s} {'measured':>10s}  verdict")
    print("  " + "-" * 44)
    for cid in sorted(res):
        v, ok = res[cid]
        print(f"  {cid:18s} {('—' if v is None else f'{v:10.4f}')}  "
              f"{'—' if ok is None else ('SATISFIED' if ok else 'VIOLATED')}")
    # A constraint bounded by its own history has nothing to say until there IS
    # a history. Reporting no verdict is the correct answer on a first run, not
    # a failure, so it is asserted rather than excused.
    baselined = {C.local(c) for c in C.checks(plan)
                 if plan.value(c, C.DQVP.baseline) is not None}
    check("every check without a baseline produced a verdict",
          all(ok is not None for cid, (_, ok) in res.items() if cid not in baselined))
    if baselined and not C.HISTORY.exists():
        check("a baseline check reports no verdict before any history exists",
              all(res[cid][1] is None for cid in baselined if cid in res))
    check("checks ran against their own asset (encounter 98%, not patient 98%)",
          res["c-enc-discharge"][0], 98.0)
    check("c-uniqueness SATISFIED", res["c-uniqueness"][1], True)
    check("referential integrity measures the seeded orphan rate",
          res["c-enc-patient"][0], 96.0)
    check("c-crossborder VIOLATED", res["c-crossborder"][1], False)
    # the group-quantification argument
    check("global completeness would pass 95%", res["c-completeness"][0] >= 95)
    check("...but the per-country check VIOLATES it", res["c-percountry"][1], False)


# --------------------------------------------------------------- 8. provenance
stage("Provenance — results recorded as DQV measurements")
if plan is None or res is None:
    note("SKIPPED - needs execution results")
else:
    meas = C.measurements_graph(plan, res)
    mvals = list(meas.subjects(RDF.type, DQV.QualityMeasurement))
    print(f"  {len(mvals)} dqv:QualityMeasurement nodes")
    measured = [cid for cid, (v, _) in res.items() if v is not None]
    check("one measurement per check that produced a value", len(mvals), len(measured))
    check("every measurement carries a value",
          all(meas.value(m, DQV.value) is not None for m in mvals))
    check("every measurement carries a verdict",
          all(meas.value(m, C.PC.verdict) is not None for m in mvals))
    check("every measurement names the metric it measured",
          all(meas.value(m, DQV.isMeasurementOf) is not None for m in mvals))


# ------------------------------------------------------------ 9. format-agnostic
stage("Data formats — the same plan over CSV, JSON and Parquet")
if not HAVE_EYE:
    note("SKIPPED - eye not on PATH")
else:
    per_format = {}
    for ds, fmt in [("dataset_csv.ttl", "csv"), ("dataset_json.ttl", "json"),
                    ("dataset_parquet.ttl", "parquet")]:
        p = C.build_plan(ds)
        per_format[fmt] = {k: v[1] for k, v in C.execute(p).items()}
        print(f"    {fmt:8s} {len(per_format[fmt])} checks executed")
    # Record-level metrics must agree across serializations: only the Load
    # differs, and the records are the same records.
    #
    # Schema metrics are a different case, and legitimately so. Their subject IS
    # the thing that differs between formats, so they can and should disagree -
    # here the parquet file declares dateOfBirth as VARCHAR while CSV and JSON
    # inference give DATE. That is the check doing its job on a real
    # inconsistency between three serializations of one dataset, not a defect.
    schema_checks = {"c-schema", "c-dtype"}
    base = per_format["csv"]
    for fmt in ("json", "parquet"):
        record_only = {k: v for k, v in per_format[fmt].items() if k not in schema_checks}
        check(f"{fmt} agrees with csv on every record-level metric",
              record_only, {k: v for k, v in base.items() if k not in schema_checks})
    note("only the Load operation changes; the check code is format-agnostic")
    differing = [k for k in schema_checks
                 if per_format["parquet"].get(k) != base.get(k)]
    check("a schema metric detects the type difference in the parquet file",
          differing, ["c-dtype"])
    note("schema metrics are format-sensitive by nature - the schema is exactly")
    note("what a serialization changes, so format-invariance cannot apply to them")


# ------------------------------------------------------------ 10. rule coverage
stage("Rule coverage — permissions, duties, and what is refused")
if not HAVE_EYE:
    note("SKIPPED - eye not on PATH")
else:
    probe = HERE / "probe_multi_asset.ttl"
    if not probe.exists():
        check("probe policy present", False)
    else:
        pplan = C.build_plan("dataset_csv.ttl", policy=probe)
        PC = C.PC
        branched = {C.local(c) for c in C.checks(pplan)
                    if C.workflow_of_check(pplan, c) is not None}
        flagged = {C.local(s) for s in pplan.subjects(PC.unsupportedRuleType, None)}
        print(f"    compiled: {', '.join(sorted(branched))}")
        print(f"    refused : {', '.join(sorted(flagged))}")
        check("permission-borne constraints compile", {"k1", "k2", "k3"} <= branched)
        check("duty-borne constraint compiles", "k4" in branched)
        check("prohibition-borne constraint is refused, not silently dropped",
              "k5" in flagged)
        check("refused constraints get no branch", "k5" not in branched)
        check("one checker across two assets",
              len(list(pplan.subjects(RDF.type, PC.PolicyChecker))), 1)
        check("two workflows", len(C.workflows(pplan)), 2)


# ------------------------------------------------- 11. cross-repo integration
stage("Cross-repo — can the validation workflow consume this profile?")
VF = REPO.parent.parent / "Connector" / "ValidationFramework"
vf_rule = VF / "planner" / "rules_n3_dqvp" / "NonNullRate.n3"
if not HAVE_EYE or not vf_rule.exists():
    note("SKIPPED - needs eye and the ValidationFramework submodule")
else:
    import re as _re
    import tempfile
    PROFILE_NS = "https://feed-upc.github.io/DQVP-position/dqvp.ttl#"
    rule_src = vf_rule.read_text()
    rule_ns = _re.search(r"@prefix dqvp:\s*<([^>]+)>", rule_src).group(1)
    print(f"    profile publishes : {PROFILE_NS}")
    print(f"    workflow expects  : {rule_ns}")

    stub = ("@prefix tb: <http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#> .\n"
            "@prefix ab: <http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#> .\n"
            "@prefix ex: <https://example.org/ehds/> .\n"
            "ab:pc1 a tb:PolicyChecker ; tb:accordingTo ex:ps-dq-offer-01 ; tb:nextStep ab:load1 .\n"
            "ab:load1 a tb:Operation ; tb:hasAbstract ab:LoadData .\n")

    def fire(rule_text):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "stub.ttl").write_text(stub)
            (d / "rule.n3").write_text(rule_text)
            out = C.eye([REPO / "dqvp.ttl", HERE / "policy.ttl", d / "stub.ttl"],
                        [d / "rule.n3"])
            return len(out)

    as_shipped = fire(rule_src)
    aligned = fire(rule_src.replace(rule_ns, PROFILE_NS))
    print(f"    as shipped        : {as_shipped} triples derived")
    print(f"    namespace aligned : {aligned} triples derived")

    check("as shipped, the workflow's DQVP rules derive nothing", as_shipped, 0)
    check("aligning the namespace makes them fire correctly", aligned > 0)
    if rule_ns != PROFILE_NS:
        note("KNOWN GAP: the two repositories disagree on the dqvp: IRI. N3 matches")
        note("on the full IRI, so a policy written against the published profile")
        note("makes no rule fire - silently, with an empty plan. The numbers above")
        note("show the IRI is the ONLY blocker: same rule, same policy, aligned")
        note("namespace, and it compiles. Settle on one IRI to close this.")


# ------------------------------------------------- 11b. the mapped EHDS DQRs
stage("Mapped DQRs — all six EHDS requirements run, against an oracle from the CSVs")
if not HAVE_EYE:
    note("SKIPPED - eye not on PATH")
else:
    import csv as _csv
    from collections import Counter as _Counter
    import duckdb as _duckdb
    import pycountry as _pycountry

    def _rows(name):
        with open(REPO / "data" / name, newline="") as f:
            return list(_csv.DictReader(f))
    ps, iso = _rows("Patient_Summary.csv"), _rows("AMR_Isolates.csv")
    codes = {c.alpha_2 for c in _pycountry.countries}
    pct = lambda n, d: round(100.0 * n / d, 4)
    countries = [r["countryOfAffiliation"] for r in ps if r["countryOfAffiliation"]]
    hospitals = [r["hospitalCountry"] for r in ps if r["hospitalCountry"]]
    males = [r for r in ps if r["gender"] == "male"]
    sexes = _Counter(r["gender"] for r in iso if r["gender"])
    oracle = {
        "c-completeness": pct(sum(bool(r["nationalHealthcarePatientID"]) for r in ps), len(ps)),
        "c-compliance":   pct(sum(c in codes for c in countries), len(countries)),
        "c-consistency":  pct(sum(not r["pregnancyHistory"] for r in males), len(males)),
        "c-dqr4eh":       float(len(iso)),
        "c-dqr5eh":       pct(max(sexes.values()) - min(sexes.values()), sum(sexes.values())),
        "c-dqr6eh":       pct(sum(c in codes for c in hospitals), len(hospitals)),
    }
    mplan = C.build_plan("dataset_csv.ttl", policy=mapped)
    got = C.execute(mplan)
    for cid in sorted(oracle):
        v, ok = got[cid]
        print(f"    {cid:15s} pipeline {v:10.4f} {'SAT' if ok else 'VIOLATED':8s} oracle {oracle[cid]:10.4f}")
    check("every mapped DQR executes", sorted(got), sorted(oracle))
    check("measured values equal the oracle", {k: v for k, (v, _) in got.items()}, oracle)
    check("verdicts: DQR3EH and DQR4EH satisfied, the other four violated",
          {k: ok for k, (_, ok) in got.items()},
          {"c-completeness": False, "c-compliance": False, "c-consistency": True,
           "c-dqr4eh": True, "c-dqr5eh": False, "c-dqr6eh": False})
    check("the policy has one permission per asset (PatientSummary, AMRIsolates)",
          sorted(C.local(a) for _, a in C.workflows(mplan)), ["AMRIsolates", "PatientSummary"])
    mcode = C.generate(mplan)
    db = _duckdb.connect()
    for w, a in C.workflows(mplan):
        db.execute(f'create table "{C.sql_table(a)}" as select * from {C.load_code(mplan, "duckdb", w, a)}')
    agree = {}
    for cid in oracle:
        sql = mcode["dbt"][cid]
        if "codes" in sql:          # reference-standard checks read a seeded code table
            continue
        for w, a in C.workflows(mplan):
            t = C.sql_table(a)
            sql = sql.replace("{{ source('ehds','" + t + "') }}", f'"{t}"')
        agree[cid] = (len(db.execute(sql).fetchall()) > 0) == (not got[cid][1])
    check("generated dbt tests agree with the reference executor", agree,
          {k: True for k in agree})
    check("GX and Deequ flag the share imbalance as not expressible",
          all("NOT EXPRESSIBLE" in mcode[e]["c-dqr5eh"] for e in ("gx", "deequ")))


# --------------------------------------------------------------- 12. the suites
stage("Regression suites")
for script in ["test_expressiveness.py", "coverage_survey.py", "test_heleade.py"]:
    p = subprocess.run([sys.executable, str(HERE / script)],
                       capture_output=True, text=True, cwd=HERE)
    tail = [l for l in p.stdout.splitlines() if "checks passed" in l]
    print(f"  {script}: {tail[-1].strip() if tail else 'no summary'}")
    check(f"{script} passes", p.returncode, 0)


# ------------------------------------------------------------------- summary
passed = sum(1 for ok, _ in results if ok)
print(f"\n{'=' * 78}")
print(f"{passed}/{len(results)} checks passed across {stage_no[0]} stages")
if passed != len(results):
    print("\nfailures:")
    for ok, label in results:
        if not ok:
            print(f"  - {label}")
print("=" * 78)
sys.exit(0 if passed == len(results) else 1)
