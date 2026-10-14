"""What is authored by hand, and what is generated, for each DQR.

For every executed requirement (the EHDS and HELEADE DQRs) this counts what the
governance authority supplies when it instantiates a pattern, and what the
pipeline then generates without further input: the DQVP constraint, the policy
checker operations, executable checks for each engine, and the verdict.

Only executable code is counted: a line that is a comment, including an engine's
NOT EXPRESSIBLE note, does not count as generated code. An engine whose code runs
but carries a note that part of the requirement is not checked (a grouping or a
row filter it cannot express) is counted as partial, not as faithful.

Run:  python automation_report.py
"""
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from rdflib import BNode, Graph

import codegen as C
import dqr_to_dqvp as D

ENGINES = ("dbt", "sodacl", "gx", "deequ")
DEPLOYMENT = {"datasetName"}      # binds a DQR to a table; not part of the pattern


def executable_lines(code):
    return sum(1 for line in str(code).splitlines()
               if line.strip() and not line.strip().startswith(("#", "--")))


PARTIAL = ("NOT EXPRESSIBLE", "not applied", "is DuckDB SQL")


def engine_status(code):
    if not executable_lines(code):
        return "none"
    return "partial" if any(m in str(code) for m in PARTIAL) else "faithful"


def triples_of(g, node):
    """Triples describing a constraint, including its blank-node filters."""
    out = list(g.triples((node, None, None)))
    for _, _, o in list(out):
        if isinstance(o, BNode):
            out += triples_of(g, o)
    return out


def report(req_dir):
    space = D.load_space(req_dir)
    reqs = D.load_requirements(req_dir)
    policy = D.ART / f"automation_{space['policyFile']}"
    policy.write_text(D.build_policy(reqs, space=space))
    g = Graph().parse(policy, format="turtle")
    plan = C.build_plan(space["descriptor"], policy=policy)
    code = C.generate(plan)
    sql = {C.local(c): c for c in C.checks(plan)}
    verdicts = C.execute(plan)
    rows = []
    for req, name in zip(reqs, D.check_names(reqs, space)):
        c = sql[name]
        ops = sum(1 for _ in plan.subjects(C.PC.forCheck, c))
        per_engine = {e: executable_lines(code[e].get(name, "")) for e in ENGINES}
        reference = C._measure_sql(plan, c, C.context_of(plan, c),
                                   C.sql_table(C.asset_of_check(plan, c)))
        rows.append({
            "dqr": req["id"], "pattern": req["pattern"]["id"],
            "authored_values": len(set(req["parameters"]) - DEPLOYMENT),
            "constraint_triples": len(triples_of(g, c)),
            "plan_operations": ops,
            "engine_code_lines": per_engine,
            "engine_status": {e: engine_status(code[e].get(name, "")) for e in ENGINES},
            "engines_faithful": sum(engine_status(code[e].get(name, "")) == "faithful" for e in ENGINES),
            "reference_sql_lines": executable_lines(reference),
            "verdict": "satisfied" if verdicts[name][1] else "violated",
        })
    return space.get("name", "EHDS"), rows


if __name__ == "__main__":
    all_rows = []
    for folder in (D.REQ_DIR, D.REQ_DIR / "heleade"):
        space, rows = report(folder)
        all_rows += [{"space": space, **r} for r in rows]
    print(f"{'DQR':8s} {'pattern':7s} {'authored':>8s} {'triples':>7s} {'ops':>4s} "
          f"{'dbt':>4s} {'soda':>4s} {'gx':>4s} {'deequ':>5s} {'faithful':>8s}  verdict")
    for r in all_rows:
        e = r["engine_code_lines"]
        print(f"{r['dqr']:8s} {r['pattern']:7s} {r['authored_values']:8d} {r['constraint_triples']:7d} "
              f"{r['plan_operations']:4d} {e['dbt']:4d} {e['sodacl']:4d} {e['gx']:4d} {e['deequ']:5d} "
              f"{r['engines_faithful']:5d}/4   {r['verdict']}")
    n = len(all_rows)
    authored = sum(r["authored_values"] for r in all_rows)
    generated = sum(r["constraint_triples"] + r["plan_operations"] + sum(r["engine_code_lines"].values())
                    + r["reference_sql_lines"] for r in all_rows)
    print(f"\n{n} DQRs: {authored} authored parameter values in total "
          f"({min(r['authored_values'] for r in all_rows)}-{max(r['authored_values'] for r in all_rows)} per DQR); "
          f"{generated} generated items (triples, operations and lines of executable code).")
    dbt_ok = sum(r["engine_status"]["dbt"] == "faithful" for r in all_rows)
    print(f"Every DQR runs on the reference executor; dbt code is faithful for {dbt_ok} of {n}; "
          f"all four engines are faithful for {sum(r['engines_faithful'] == 4 for r in all_rows)} of {n}.")
    (D.ART / "automation.json").write_text(json.dumps(all_rows, indent=2))
