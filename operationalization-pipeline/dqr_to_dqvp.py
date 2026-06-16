"""DQR catalogue -> DQVP policy (the front of the end-to-end).

Each DQR JSON instantiates a pattern (DQRP*) and carries its parameters. One
pattern -> one DQVP DQConstraint shape; the parameters fill the slots. This mirrors
the pattern-based mapping in the Computational Catalogues prototype, retargeted to
the current DQVP model (dqvp:DQConstraint + dqvp:filter/RowFilter), so the output
feeds codegen.build_plan directly:

    DQR JSON --[this]--> DQVP policy --[plan.n3]--> Policy Checker --> code --> verdicts

Run:  python dqr_to_dqvp.py     (maps requirements/*.json, then runs the pipeline)
"""
import json
from pathlib import Path

import codegen as C

HERE    = Path(__file__).parent
REQ_DIR = HERE.parent / "requirements"
ART     = HERE / "artifacts"; ART.mkdir(exist_ok=True)

# pattern id -> DQVP constraint shape (slot name = DQR parameter it binds)
PATTERN_SPEC = {
    "DQRP2": dict(metric="dqvp:NonNullRate", target="attributeName",
                  operatorParam="operator", right="percentageValue", rightType="xsd:decimal"),
    "DQRP3": dict(metric="dqvp:ReferenceConformance", target="attributeName",
                  referenceStandard="standardName", operator="odrl:gteq",
                  right="percentageValue", rightType="xsd:decimal"),
    "DQRP4": dict(metric="dqvp:NullRate", target="attributeName2",
                  filterAttr="attributeName1", filterVals="ValueSet1", filterOp="odrl:eq",
                  operator="odrl:eq", rightFixed="100", rightType="xsd:decimal"),
}
OP_WORDS      = {"at least": "odrl:gteq", "at most": "odrl:lteq",
                 "exactly": "odrl:eq", "equal": "odrl:eq", "contain": "odrl:eq"}
STANDARD_IRIS = {"ISO 3166":   "https://www.iso.org/iso-3166-country-codes.html",
                 "ISO 3166-1": "https://www.iso.org/iso-3166-country-codes.html"}
DIM_CHECK     = {"Completeness": "c-completeness", "Compliance": "c-compliance",
                 "Consistency": "c-consistency"}


def leaf(name):  # "A.B.attr" / "attr" -> "attr"
    return str(name).split(".")[-1]


def constraint(req):
    """(local name, Turtle block) for one DQR's DQConstraint."""
    spec = PATTERN_SPEC[req["pattern"]["id"]]
    p    = req["parameters"]
    name = DIM_CHECK.get(req["qualityDimension"]["dimension"], "c-" + req["id"].lower())

    lines = [f'ex:{name} a dqvp:DQConstraint ; rdfs:label "{name}" ;',
             f"    odrl:leftOperand     {spec['metric']} ;"]
    if "target" in spec:
        lines.append(f"    dqvp:targetAttribute ab:{leaf(p[spec['target']])} ;")
    if "referenceStandard" in spec:
        std = p[spec["referenceStandard"]]
        iri = STANDARD_IRIS.get(std, f"https://www.iso.org/standard/{std.replace(' ', '_')}")
        lines.append(f"    dqvp:referenceStandard <{iri}> ;")
    if "filterAttr" in spec:
        vals  = [v.strip() for v in str(p[spec["filterVals"]]).strip("{}").split(",") if v.strip()]
        inner = " ;\n                  ".join(
            ["a dqvp:RowFilter",
             f"dqvp:filterAttribute ab:{leaf(p[spec['filterAttr']])}",
             f"dqvp:filterOperator  {spec['filterOp']}",
             *[f'dqvp:filterValue     "{v}"' for v in vals]])
        lines.append(f"    dqvp:filter [ {inner} ] ;")

    op    = spec.get("operator") or OP_WORDS.get(p.get(spec.get("operatorParam"), ""), "odrl:gteq")
    rval  = spec.get("rightFixed", p.get(spec.get("right"), ""))
    rtype = spec.get("rightType", "")
    ro    = f'"{rval}"^^{rtype}' if rtype else f'"{rval}"'
    lines += [f"    odrl:operator        {op} ;",
              f"    odrl:rightOperand    {ro} ."]
    return name, "\n".join(lines)


def build_policy(reqs, policy_id="ps-dq-offer-01"):
    dataset  = reqs[0]["parameters"].get("datasetName", "PatientSummary").replace(".", "_")
    assigner = "".join(w.capitalize() for w in reqs[0]["sourceEntity"].split())
    blocks   = [constraint(r) for r in reqs]
    names    = ", ".join(f"ex:{n}" for n, _ in blocks)
    return (
        "@prefix odrl: <http://www.w3.org/ns/odrl/2/> .\n"
        "@prefix dqvp: <https://feed-upc.github.io/DQVP-position/dqvp.ttl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .\n"
        "@prefix ab:   <https://w3id.org/ehds/patsum/attr/> .\n"
        "@prefix dp:   <https://w3id.org/ehds/patsum/asset/> .\n"
        "@prefix ex:   <https://example.org/ehds/> .\n\n"
        f"ex:{assigner} a odrl:Party .\n"
        f"dp:{dataset} a odrl:Asset .\n\n"
        f"ex:{policy_id}\n"
        "    a dqvp:QualityPolicy, odrl:Offer ;\n"
        "    odrl:profile  <https://feed-upc.github.io/DQVP-position/dqvp.ttl> ;\n"
        f"    odrl:assigner ex:{assigner} ;\n"
        "    odrl:permission [\n"
        "        a            odrl:Permission ;\n"
        f"        odrl:target  dp:{dataset} ;\n"
        "        odrl:action  odrl:use ;\n"
        f"        odrl:constraint {names}\n"
        "    ] .\n\n"
        + "\n\n".join(b for _, b in blocks) + "\n")


def load_requirements(req_dir=REQ_DIR):
    return [json.loads(p.read_text()) for p in sorted(req_dir.glob("DQR*.json"))]


if __name__ == "__main__":
    reqs = load_requirements()
    print(f"mapped {len(reqs)} DQRs: " + ", ".join(f"{r['id']} ({r['pattern']['id']})" for r in reqs))
    ttl = build_policy(reqs)
    out = ART / "policy_from_dqr.ttl"; out.write_text(ttl)
    print("\n" + ttl)

    print("=" * 60, "\nrun the pipeline on the MAPPED policy:\n")
    plan = C.build_plan("dataset_csv.ttl", policy=out)
    for cid, (v, ok) in sorted(C.execute(plan).items()):
        print(f"  {cid:16s} {('—' if v is None else f'{v:8.4f}')}  "
              f"{'SAT' if ok else 'VIOLATED'}")
