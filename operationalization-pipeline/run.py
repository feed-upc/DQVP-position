"""v5 end-to-end: distinct PolicyChecker, shared-load workflow, shape-driven,
neutral plan, DQV measurements. Implements analysis items 1-6."""
import warnings; warnings.filterwarnings("ignore")
from pathlib import Path

from rdflib import RDF

import codegen as C

HERE = Path(__file__).parent
ART  = HERE / "artifacts"; ART.mkdir(exist_ok=True)
PC   = C.PC
def bar(t): print("\n" + "=" * 82 + f"\n{t}\n" + "=" * 82)


bar("STAGE 1  policy --[EYE plan.n3 + vocab + config]--> Policy Checker (one workflow)")
plan = C.build_plan()
plan.serialize(destination=str(ART / "plan.ttl"), format="turtle")
chk = next(plan.subjects(RDF.type, PC.PolicyChecker))
wf  = plan.value(chk, PC.hasWorkflow)
print(f"  PolicyChecker {C.local(chk)}  accordingTo {C.local(plan.value(chk, PC.accordingTo))}")
print(f"  shared Load -> Table, then per-check branches:\n")
def _branch(op):
    c = plan.value(op, PC.forCheck)
    return C.local(c) if c else "·shared"
def _opkind(op):
    return C.local(next(x for x in plan.objects(op, RDF.type) if x != PC.Operation))
ops = sorted(plan.objects(wf, PC.hasOp),
             key=lambda o: (_branch(o), int(plan.value(o, PC.order))))
for op in ops:
    cons = [C.local(x) for x in plan.objects(op, PC.input)]
    prod = [C.local(x) for x in plan.objects(op, PC.output)]
    print(f"    {_branch(op):16s} {_opkind(op):8s} {cons} -> {prod}")
print("\n  -> artifacts/plan.ttl")

bar("STAGE 2  workflow + templates.ttl --[1 generic Jinja binder]--> code (4 engines)")
code = C.generate(plan)
for e in sorted(code): print(f"  {e:7s}: {sorted(code[e])}")
print("\n  c-consistency (B3 filter rendered by template, not in plan):")
print("    gx :", code["gx"]["c-consistency"])

# write artifacts
import contextlib
(ART / "checks_sodacl.yml").write_text(C.assemble_sodacl(code["sodacl"]))
for cid, sql in code["dbt"].items(): (ART / f"dbt_{cid}.sql").write_text(sql + "\n")

bar("STAGE 3  execute + record dqv:QualityMeasurement (provenance, #5)")
results = C.execute(plan)
meas = C.measurements_graph(plan, results)
meas.serialize(destination=str(ART / "measurements.ttl"), format="turtle")
gx_res = C.run_gx(code["gx"], plan)
print(f"  {'check':16s} {'measured':>10s}  {'verdict':8s} {'GX':6s} agree?")
print("  " + "-" * 52)
for cid in sorted(results):
    v, ok = results[cid]
    verd = "—" if ok is None else ("SAT" if ok else "VIOL")
    g = "PASS" if gx_res[cid] else "FAIL"
    agree = "yes" if (ok is None or ok == gx_res[cid]) else "NO!"
    mv = "—" if v is None else f"{v:10.4f}"
    print(f"  {cid:16s} {mv}  {verd:8s} {g:6s} {agree}")
print("\n  measurements -> artifacts/measurements.ttl")
print("  artifacts:", ", ".join(p.name for p in sorted(ART.iterdir())))
print("done.")
