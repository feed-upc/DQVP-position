"""Generate v5_end_to_end.ipynb."""
import nbformat as nbf

nb = nbf.v4.new_notebook()
C = []
def md(s): C.append(nbf.v4.new_markdown_cell(s.strip("\n")))
def code(s): C.append(nbf.v4.new_code_cell(s.strip("\n")))

md(r"""
# DQVP — operationalization pipeline: from policy to validated data, across engines *and* data formats

```
policy.ttl + data-product descriptor
   ──[EYE rules/plan.n3 + config + vocab]──► Policy Checker (workflow of OP: input→output)
   ──[graph templates + 1 Jinja binder]────► check code (GX·dbt·SodaCL·PyDeequ)
                                          └─► LOAD code per (engine, FORMAT)   ← csv / json / parquet
   ──[execute on the data]──────────────────► verdicts + dqv:QualityMeasurement
```

* **Contract:** the provider offers the asset for use *under* quality conditions
  (`odrl:constraint`s on an `odrl:Permission`; no "validate" action).
* **Plan:** typed operations with an explicit `input → output` shape (after Hmimou et al.).
* **Code:** the *check* code is format-agnostic; only the **Load** operation's code depends
  on the data format — so the same plan validates CSV, JSON or Parquet.
""")

md("## 0. The policy (contract)")
code(r"""
import sys, warnings; warnings.filterwarnings("ignore")
from pathlib import Path
PIPE = "operationalization-pipeline"          # supporting code + ontologies live here
sys.path.insert(0, PIPE)
from rdflib import RDF, Graph, Namespace
import codegen as C

EX = Namespace("https://example.org/ehds/")
print(Path(PIPE, "policy.ttl").read_text())
""")

md(r"""
## 1. Compile to the Policy Checker (intermediate metadata)

One `pc:PolicyChecker` `pc:accordingTo` the policy: a shared `Load` (with a `Dataset`
**input** and a `Table` **output**) feeds parallel per-check branches
(`Filter → Measure → Compare → Report`, plus `Resolve → CodeList` for the reference check).
Every operation has the `input → output` shape; parameters are added on top.
""")
code(r"""
plan = C.build_plan("dataset_csv.ttl")
Path(PIPE, "artifacts").mkdir(exist_ok=True)
plan.serialize(destination=f"{PIPE}/artifacts/plan.ttl", format="turtle")
PC = C.PC

chk = next(plan.subjects(RDF.type, PC.PolicyChecker))
wf  = plan.value(chk, PC.hasWorkflow)

def branch(op):                                   # which check this op belongs to ("·shared" Load)
    c = plan.value(op, PC.forCheck)
    return C.local(c) if c else "·shared"
def opkind(op):                                   # Load / Filter / Measure / …
    return C.local(next(x for x in plan.objects(op, RDF.type) if x != PC.Operation))

print(f"pc:PolicyChecker {C.local(chk)}  accordingTo {C.local(plan.value(chk, PC.accordingTo))}\n")
print(f"  {'branch':16s} {'op':8s} input -> output")
for op in sorted(plan.objects(wf, PC.hasOp), key=lambda o: (branch(o), int(plan.value(o, PC.order)))):
    ins = [C.local(x) for x in plan.objects(op, PC.input)]
    out = [C.local(x) for x in plan.objects(op, PC.output)]
    ps  = {str(plan.value(p, PC.name)): str(plan.value(p, PC.value)) for p in plan.objects(op, PC.parameter)}
    print(f"  {branch(op):16s} {opkind(op):8s} {ins} -> {out}" + (f"   {ps}" if ps else ""))
""")

md("### The Load operation's `Dataset` input carries the data format")
code(r"""
src = plan.value(next(plan.subjects(RDF.type, PC.Load)), PC.input)
g = Graph(); g.bind("pc", PC)
for tr in plan.triples((src, None, None)): g.add(tr)
print(g.serialize(format="turtle"))
""")

md(r"""
## 2. Generate the check code (graph templates + one generic binder)

Four engines, from the *same* workflow. This code is **format-agnostic** — it runs over the
loaded table.
""")
code(r"""
def show(s, n=96):
    return "\n".join((l[:n] + " …]") if len(l) > n else l for l in s.splitlines())

code_by_engine = C.generate(plan)
for cid in ["c-completeness", "c-compliance", "c-consistency"]:
    print(f"=== {cid} ===")
    for e in sorted(code_by_engine):
        print(f"  [{e:6s}] {show(code_by_engine[e][cid])}")
    print()
""")

md(r"""
## 3. The data-format dimension

Only the **Load** operation's code depends on the format. The descriptor (`dataset_*.ttl`)
sets `pc:format` + `pc:locator` on the asset; the binder renders the matching load template.
""")
code(r"""
plan_csv     = C.build_plan("dataset_csv.ttl")
plan_json    = C.build_plan("dataset_json.ttl")
plan_parquet = C.build_plan("dataset_parquet.ttl")

for p in (plan_csv, plan_json, plan_parquet):
    fmt, _ = C.dataset_of(p)
    print(f"format = {fmt}")
    for eng in ["gx", "duckdb", "dbt"]:
        print(f"   load[{eng:6s}] {C.load_code(p, eng)}")
    print()

print("the CHECK code is identical across formats — e.g. GX c-completeness:")
print("  ", C.generate(plan_csv)["gx"]["c-completeness"])
""")

md(r"""
## 4. Run the generated code on the data — CSV, JSON and Parquet

GX expectations are executed live; the generated dbt singular tests are run as SQL via DuckDB
(format-aware scan); a DuckDB measure-and-compare gives the reference value + verdict. Same
plan, different formats, identical verdicts.
""")
code(r"""
import duckdb

def run_format(plan):
    code = C.generate(plan)
    gx   = C.run_gx(code["gx"], plan)          # generated GX, live
    ref  = C.execute(plan)                      # reference measure-and-compare (format-aware)
    con  = duckdb.connect()
    con.execute(f"CREATE TABLE data AS SELECT * FROM {C.load_code(plan, 'duckdb')}")
    con.execute("CREATE TABLE codes(code VARCHAR)")
    std = C.ref_standard(plan, EX["c-compliance"])
    con.executemany("INSERT INTO codes VALUES (?)", [(x,) for x in C.resolve_codes(std)])
    dbt = {}
    for cid, sql in code["dbt"].items():
        body = "\n".join(l for l in sql.splitlines() if not l.startswith("--"))
        body = body.replace("{{ source('ehds','patient_summary') }}", "data")
        dbt[cid] = not con.execute(body).fetchall()
    return ref, gx, dbt

for label, plan in [("CSV", plan_csv), ("JSON", plan_json), ("PARQUET", plan_parquet)]:
    ref, gx, dbt = run_format(plan)
    print(f"--- {label} ({C.dataset_of(plan)[0]}) ---")
    print(f"  {'check':16s} {'measured':>10s}  {'verdict':8s} {'GX':6s} {'dbt':6s}")
    for cid in sorted(ref):
        v, ok = ref[cid]
        print(f"  {cid:16s} {('—' if v is None else f'{v:10.4f}')}  "
              f"{('SAT' if ok else 'VIOL'):8s} {('PASS' if gx[cid] else 'FAIL'):6s} "
              f"{('PASS' if dbt[cid] else 'FAIL'):6s}")
    print()
""")

md(r"""
## 5. Provenance of results (DQV)

Each measured value is recorded as a `dqv:QualityMeasurement` on the workflow's `MeasureValue`.
""")
code(r"""
meas = C.measurements_graph(plan_csv, C.execute(plan_csv))
meas.serialize(destination=f"{PIPE}/artifacts/measurements.ttl", format="turtle")
print(meas.serialize(format="turtle"))
""")

md(r"""
### Takeaway

One DQVP policy → one technology-agnostic Policy Checker (operations with `input→output`) →
code for four engines, with the **Load** code selected per data **format**. GX and dbt run that
code on CSV, JSON and Parquet with identical verdicts, recorded back in DQV. Adding a format = a load
template row; an engine = check + load template rows; a metric shape = one rule.
""")

nb["cells"] = C
nb["metadata"] = {"kernelspec": {"name": "python3", "display_name": "Python 3"},
                  "language_info": {"name": "python"}}
nbf.write(nb, "../operationalization.ipynb")
print("wrote ../operationalization.ipynb with", len(C), "cells")
