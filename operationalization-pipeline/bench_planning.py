"""How does planning scale with the size of the policy?

Compiles synthetic policies of increasing constraint count and times the stages.
The question this answers: is compilation linear in the number of constraints, or
does it degrade the way a full re-derivation over a growing graph would?

Run:  python bench_planning.py [max_constraints]
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import codegen as C

ART = HERE / "artifacts"; ART.mkdir(exist_ok=True)

COLUMNS = ["nationalHealthcarePatientID", "familyName", "givenName", "dateOfBirth",
           "gender", "countryOfAffiliation", "hospitalCode", "hospitalCountry",
           "lastUpdated", "pregnancyHistory"]
METRICS = ["dqvp:NonNullRate", "dqvp:UniqueRate", "dqvp:NullRate"]

HEADER = """@prefix odrl: <http://www.w3.org/ns/odrl/2/> .
@prefix dqvp: <https://feed-upc.github.io/DQVP-position/dqvp.ttl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix ab:   <https://w3id.org/ehds/patsum/attr/> .
@prefix dp:   <https://w3id.org/ehds/patsum/asset/> .
@prefix ex:   <https://example.org/bench/> .

ex:Provider a odrl:Party .
dp:PatientSummary a odrl:Asset .
"""


def synth_policy(n):
    """A policy with n scalar-column constraints over one asset."""
    names = [f"b{i}" for i in range(n)]
    body = [HEADER,
            "ex:bench-policy",
            "    a dqvp:QualityPolicy, odrl:Offer ;",
            "    odrl:profile  <https://feed-upc.github.io/DQVP-position/dqvp.ttl> ;",
            "    odrl:assigner ex:Provider ;",
            "    odrl:permission [",
            "        a odrl:Permission ; odrl:target dp:PatientSummary ; odrl:action odrl:use ;",
            "        odrl:constraint " + ", ".join(f"ex:{x}" for x in names),
            "    ] .",
            ""]
    for i, nm in enumerate(names):
        body.append(
            f'ex:{nm} a dqvp:DQConstraint ; rdfs:label "{nm}" ;\n'
            f"    odrl:leftOperand     {METRICS[i % len(METRICS)]} ;\n"
            f"    dqvp:targetAttribute ab:{COLUMNS[i % len(COLUMNS)]} ;\n"
            f"    odrl:operator        odrl:gteq ;\n"
            f'    odrl:rightOperand    "50"^^xsd:decimal .')
    return "\n".join(body) + "\n"


def bench(n):
    pol = ART / f"bench_{n}.ttl"
    pol.write_text(synth_policy(n))
    t0 = time.perf_counter()
    plan = C.build_plan("dataset_csv.ttl", policy=pol)
    t_plan = time.perf_counter() - t0

    t0 = time.perf_counter()
    C.generate(plan)
    t_gen = time.perf_counter() - t0

    ops = len(list(plan.subjects(C.RDF.type, C.PC.Operation)))
    # Operation is only asserted via its subclasses, so count the typed ops
    ops = sum(len(list(plan.subjects(C.RDF.type, t)))
              for t in (C.PC.Load, C.PC.Filter, C.PC.Measure, C.PC.Compare,
                        C.PC.Report, C.PC.Resolve))
    pol.unlink()
    return t_plan, t_gen, ops, len(C.checks(plan))


if __name__ == "__main__":
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 128
    sizes = [n for n in (1, 2, 4, 8, 16, 32, 64, 128, 256) if n <= cap]

    print(f"{'checks':>7} {'ops':>6} {'plan s':>9} {'ms/check':>10} "
          f"{'codegen s':>10} {'ratio':>7}")
    print("-" * 56)
    rows, prev = [], None
    for n in sizes:
        t_plan, t_gen, ops, got = bench(n)
        ratio = "" if prev is None else f"{t_plan / prev:6.2f}x"
        print(f"{got:>7} {ops:>6} {t_plan:>9.3f} {1000 * t_plan / got:>10.2f} "
              f"{t_gen:>10.3f} {ratio:>7}")
        rows.append((got, ops, t_plan, t_gen))
        prev = t_plan
        if t_plan > 120:
            print("  (stopping: planning exceeded 2 minutes)")
            break

    print()
    # Doubling the input doubles a linear cost and quadruples a quadratic one.
    doublings = [rows[i][2] / rows[i - 1][2] for i in range(1, len(rows))
                 if rows[i][0] == 2 * rows[i - 1][0] and rows[i - 1][2] > 0.05]
    if doublings:
        avg = sum(doublings) / len(doublings)
        print(f"  mean cost ratio per doubling of constraint count: {avg:.2f}x")
        print(f"  linear would be ~2.0x, quadratic ~4.0x  ->  "
              f"{'LINEAR' if avg < 2.6 else 'SUPERLINEAR' if avg < 3.4 else 'QUADRATIC'}")
    else:
        print("  (too fast to characterise reliably at these sizes)")
    print(f"  operations per constraint: "
          f"{(rows[-1][1] - rows[0][1]) / (rows[-1][0] - rows[0][0]):.1f}")
