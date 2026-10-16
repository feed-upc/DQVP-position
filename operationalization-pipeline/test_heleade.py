"""End-to-end test of the HELEADE requirements (IST paper, Section 6.2).

The second data space through the same chain as the EHDS one:

  DQR json (DQRP2-5) -> DQVP policy -> EYE plan -> engine code -> execution

Every measured value is compared with an oracle computed directly from the CSV
files, and every check is also run on a clean and on a fully defective variant
of the data, so each one is seen both passing and failing.

Run:  python test_heleade.py
"""
import csv
import importlib.util
import sys
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import duckdb
from rdflib import Graph, RDF

import codegen as C
import dqr_to_dqvp as D

REPO    = HERE.parent
REQS    = D.REQ_DIR / "heleade"           # in the DQR catalogue
DATA    = REPO / "data" / "heleade"
ART     = HERE / "artifacts"
VARIANT = ART / "heleade_variants"

_spec = importlib.util.spec_from_file_location("heleade_data", DATA / "generate.py")
G = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(G)

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


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def is_utc(value):
    """Independent of the regex: ISO 8601 date-time with the Z designator."""
    if "T" not in value or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
        return True
    except ValueError:
        return False


def oracle(data_dir):
    """The four measurements, computed straight from the CSV files."""
    sites, obs = rows(data_dir / "monitoring_site.csv"), rows(data_dir / "observation.csv")
    not_detected = [r for r in obs if r["detectionStatus"] == "notDetected"]
    return {
        "c-dqr1hl": round(100.0 * sum(bool(r["location"]) for r in sites) / len(sites), 4),
        "c-dqr2hl": round(100.0 * sum(is_utc(r["observedAt"]) for r in obs) / len(obs), 4),
        "c-dqr3hl": round(100.0 * sum(r["breedingEvidence"] not in G.BREEDING for r in not_detected)
                          / len(not_detected), 4),
        "c-dqr4hl": float(min(Counter(r["wetlandId"] for r in sites).values())),
    }


def descriptor(data_dir, text=True):
    """A data-product descriptor over data_dir, optionally without pc:textAttribute."""
    name = f"dataset_{data_dir.name}{'' if text else '_typed'}.ttl"
    path = VARIANT / name
    path.write_text(
        "@prefix dp: <https://w3id.org/heleade/asset/> .\n"
        "@prefix pc: <https://feed-upc.github.io/DQVP-position/pc#> .\n"
        f'dp:MonitoringSite pc:format "csv" ; pc:locator "{data_dir / "monitoring_site.csv"}" .\n'
        f'dp:Observation    pc:format "csv" ; pc:locator "{data_dir / "observation.csv"}"'
        + (' ; pc:textAttribute "observedAt" .\n' if text else " .\n"))
    return path


# ------------------------------------------------------------- 1. requirements
stage("Requirements — the HELEADE DQRs instantiate DQRP2 to DQRP5")
space = D.load_space(REQS)
reqs = D.load_requirements(REQS)
for r in reqs:
    print(f"    {r['id']:7s} {r['pattern']['id']:6s} {r['statement'][:62]}...")
check("four requirements parse", len(reqs), 4)
check("they use patterns DQRP2 to DQRP5", sorted(r["pattern"]["id"] for r in reqs),
      ["DQRP2", "DQRP3", "DQRP4", "DQRP5"])
check("every referenced pattern has a mapping spec",
      sorted({r["pattern"]["id"] for r in reqs} - set(D.PATTERN_SPEC)), [])
check("every requirement names the HELEADE Governance Authority",
      {r["sourceEntity"] for r in reqs}, {"HELEADE Governance Authority"})
check("dataspace.json is read", space["name"], "HELEADE")
check("every requirement resolves to an asset",
      [D.target_asset(r, space) for r in reqs],
      ["MonitoringSite", "Observation", "Observation", "MonitoringSite"])
check("the EHDS folder still holds exactly its six DQRs", len(D.load_requirements()), 6)


# ------------------------------------------------------- 2. DQR -> DQVP policy
stage("Mapping — the DQRs compile to one policy with a permission per asset")
ART.mkdir(exist_ok=True)
VARIANT.mkdir(exist_ok=True)
policy = ART / space["policyFile"]
policy.write_text(D.build_policy(reqs, space=space))
g = Graph().parse(policy, format="turtle")
DQVP, ODRL = C.DQVP, C.ODRL
cons = {C.local(c): c for c in g.subjects(RDF.type, DQVP.DQConstraint)}
check("mapped policy is valid Turtle", len(g) > 0)
check("one constraint per requirement", sorted(cons), ["c-dqr1hl", "c-dqr2hl", "c-dqr3hl", "c-dqr4hl"])
targets = sorted(C.local(t) for p in g.objects(None, ODRL.permission) for t in g.objects(p, ODRL.target))
check("one permission per asset", targets, ["MonitoringSite", "Observation"])
metric = lambda c: C.local(g.value(cons[c], ODRL.leftOperand))
check("DQRP2 -> NonNullRate", metric("c-dqr1hl"), "NonNullRate")
check("DQRP3 with a format standard -> RowExpressionRate", metric("c-dqr2hl"), "RowExpressionRate")
check("DQRP4 'not contain' -> RowExpressionRate under a RowFilter",
      (metric("c-dqr3hl"), g.value(cons["c-dqr3hl"], DQVP["filter"]) is not None),
      ("RowExpressionRate", True))
check("DQRP5 over Wetland.monitoringSite -> RowCount grouped by wetlandId",
      (metric("c-dqr4hl"), C.local(g.value(cons["c-dqr4hl"], DQVP.groupBy))),
      ("RowCount", "wetlandId"))

con = duckdb.connect()
con.execute("create table t as select * from (values (NULL), ('nesting'), ('other')) v(x)")
truth = lambda pred: [r[0] for r in con.execute(f"select {pred} from t").fetchall()]
check("value set: 'not contain' {nesting} admits NULL and 'other'",
      truth(D.value_predicate("x", "not contain", ["nesting"])), [True, False, True])
check("value set: 'contain' {nesting} admits only 'nesting'",
      [bool(v) for v in truth(D.value_predicate("x", "contain", ["nesting"]))], [False, True, False])
check("value set: 'contain' {NULL, nesting} admits NULL and 'nesting'",
      truth(D.value_predicate("x", "contain", ["NULL", "nesting"])), [True, True, False])
check("value set: 'not contain' {NULL} admits every present value",
      truth(D.value_predicate("x", "not contain", ["NULL"])), [False, True, True])


# ------------------------------------------------------------------ 3. plan
stage("Plan — EYE derives a policy checker with one workflow per asset")
plan = C.build_plan(space["descriptor"], policy=policy)
check("four checks in the plan", sorted(C.local(c) for c in C.checks(plan)), sorted(cons))
check("two workflows", sorted(C.local(a) for _, a in C.workflows(plan)), ["MonitoringSite", "Observation"])


# ------------------------------------------------------- 4. shipped data
stage("Execution — the shipped dataset, against an oracle computed from the CSVs")
want = oracle(DATA)
got = C.execute(plan)
for cid in sorted(want):
    v, ok = got[cid]
    print(f"    {cid}  pipeline {v:9.4f} {'SAT' if ok else 'VIOLATED':8s}  oracle {want[cid]:9.4f}")
check("measured values equal the oracle", {k: v for k, (v, _) in got.items()}, want)
check("verdicts: DQR1HL and DQR3HL satisfied, DQR2HL and DQR4HL violated",
      {k: ok for k, (_, ok) in got.items()},
      {"c-dqr1hl": True, "c-dqr2hl": False, "c-dqr3hl": True, "c-dqr4hl": False})


# ------------------------------------------------------- 5. both outcomes
stage("Execution — every check passes on clean data and fails with every defect")
for label, defects, verdict in [("clean", set(), True), ("all-defects", G.ALL, False)]:
    out = VARIANT / label
    G.generate(out, defects)
    vplan = C.build_plan(descriptor(out), policy=policy)
    res = C.execute(vplan)
    check(f"{label}: values equal the oracle", {k: v for k, (v, _) in res.items()}, oracle(out))
    check(f"{label}: every check {'satisfied' if verdict else 'violated'}",
          {k: ok for k, (_, ok) in res.items()}, {k: verdict for k in want})


# ------------------------------------------------------- 6. lexical reading
stage("Load — a format check needs the attribute as written")
typed = C.execute(C.build_plan(descriptor(DATA, text=False), policy=policy))["c-dqr2hl"][0]
print(f"    c-dqr2hl read as text {got['c-dqr2hl'][0]:.4f}, read with type inference {typed:.4f}")
check("with type inference the timestamps are rewritten and the measurement is wrong",
      typed != want["c-dqr2hl"])
wf = [w for w, a in C.workflows(plan) if C.local(a) == "Observation"][0]
check("the DuckDB load reads observedAt as VARCHAR",
      "types={ 'observedAt': 'VARCHAR' }" in C.load_code(plan, "duckdb", wf))
check("the pandas load reads observedAt as str",
      "dtype={ 'observedAt': str }" in C.load_code(plan, "gx", wf))


# ------------------------------------------------------- 7. generated code
stage("Code generation — grouping and filters are kept or flagged, never dropped")
code = C.generate(plan)
check("dbt groups DQR4HL by wetlandId", 'group by "wetlandId"' in code["dbt"]["c-dqr4hl"])
for eng in ("gx", "sodacl", "deequ"):
    check(f"{eng} flags DQR4HL's grouping as not expressible",
          "NOT EXPRESSIBLE: per-group evaluation over wetlandId" in code[eng]["c-dqr4hl"])
check("SodaCL applies DQR3HL's row filter",
      "where \"detectionStatus\" in ('notDetected')" in code["sodacl"]["c-dqr3hl"])
check("Deequ notes that DQR3HL's row filter is not applied",
      "row filter detectionStatus" in code["deequ"]["c-dqr3hl"])
for cid in ("c-dqr2hl", "c-dqr3hl"):
    line = code["deequ"][cid].split("  #")[0].strip()
    try:
        compile("checks" + line, "<deequ>", "eval"); valid = True
    except SyntaxError:
        valid = False
    check(f"Deequ code for {cid} is valid Python", valid)
check("GX flags the row expressions as not expressible",
      all("NOT EXPRESSIBLE" in code["gx"][c] for c in ("c-dqr2hl", "c-dqr3hl")))
check("dbt sources are declared under 'heleade'",
      all("source('heleade'," in code["dbt"][c] for c in cons))

# The dbt tests are SQL that returns the failing rows: run them on DuckDB and
# require that they fail exactly when the reference executor says VIOLATED.
db = duckdb.connect()
for w, a in C.workflows(plan):
    db.execute(f'create table "{C.sql_table(a)}" as select * from {C.load_code(plan, "duckdb", w, a)}')
agree = {}
for cid in sorted(cons):
    sql = code["dbt"][cid]
    for t in ("monitoring_site", "observation"):
        sql = sql.replace("{{ source('heleade','" + t + "') }}", f'"{t}"')
    agree[cid] = (len(db.execute(sql).fetchall()) > 0) == (not got[cid][1])
check("generated dbt tests agree with the reference executor on every check", agree,
      {c: True for c in cons})


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
