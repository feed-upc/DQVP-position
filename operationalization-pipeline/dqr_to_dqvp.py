"""DQR catalogue -> DQVP policy (the front of the end-to-end).

Each DQR JSON instantiates a pattern (DQRP*) and carries its parameters. One
pattern -> one DQVP DQConstraint shape; the parameters fill the slots. This mirrors
the pattern-based mapping in the Computational Catalogues prototype, retargeted to
the current DQVP model (dqvp:DQConstraint + dqvp:filter/RowFilter), so the output
feeds codegen.build_plan directly:

    DQR JSON --[this]--> DQVP policy --[plan.n3]--> Policy Checker --> code --> verdicts

The DQRs are not kept here. They come from the DQR catalogue,
DS-DataQualityRequirements, whose prototype/requirements folder this repository reads
when it is that catalogue's tools/dqvp submodule. DQVP_REQUIREMENTS overrides the
folder. A requirements folder may carry a dataspace.json naming the data space's
namespaces, its data-product descriptor, how its entities bind to assets, and which
column holds a data-model attribute whose name differs ("Hospital.country" ->
hospitalCountry). Without one, DEFAULT_SPACE applies.

Run:  python dqr_to_dqvp.py                    (the catalogue's EHDS DQRs)
      python dqr_to_dqvp.py <folder>           (e.g. the catalogue's requirements/heleade)
"""
import json
import os
import sys
from pathlib import Path

import codegen as C

HERE      = Path(__file__).parent
CATALOGUE = HERE.parent.parent.parent          # DS-DataQualityRequirements, when this is tools/dqvp
REQ_DIR   = Path(os.environ.get("DQVP_REQUIREMENTS") or CATALOGUE / "prototype" / "requirements")
ART     = HERE / "artifacts"; ART.mkdir(exist_ok=True)

# pattern id -> DQVP constraint shape (slot name = DQR parameter it binds).
# DQRP3 and DQRP4 also have a record-level shape, chosen per requirement in
# constraint(): a format standard has no code list, and a value set other than
# {NULL} is not a null rate.
PATTERN_SPEC = {
    "DQRP2": dict(metric="dqvp:NonNullRate", target="attributeName",
                  operatorParam="operator", right="percentageValue", rightType="xsd:decimal"),
    "DQRP3": dict(metric="dqvp:ReferenceConformance", target="attributeName",
                  referenceStandard="standardName", operator="odrl:gteq",
                  right="percentageValue", rightType="xsd:decimal"),
    "DQRP4": dict(metric="dqvp:NullRate", target="attributeName2",
                  filterAttr="attributeName1", filterVals="ValueSet1", filterOp="odrl:eq",
                  operator="odrl:eq", rightFixed="100", rightType="xsd:decimal"),
    "DQRP5": dict(metric="dqvp:RowCount", operatorParam="operator",
                  right="value", rightType="xsd:decimal"),
    "DQRP6": dict(metric="dqvp:ShareImbalance", target="attributeName",
                  operatorParam="operator", right="percValue", rightType="xsd:decimal"),
}
OP_WORDS      = {"at least": "odrl:gteq", "at most": "odrl:lteq", "not exceed": "odrl:lteq",
                 "exactly": "odrl:eq", "be exactly": "odrl:eq", "equal": "odrl:eq",
                 "contain": "odrl:eq"}
STANDARD_IRIS = {"ISO 3166":   "https://www.iso.org/iso-3166-country-codes.html",
                 "ISO 3166-1": "https://www.iso.org/iso-3166-country-codes.html"}
# Standards that fix how a value is written rather than which values are allowed.
# They are checked against the text of the value, so the descriptor must ask for
# the attribute to be read as written (pc:textAttribute). Like every
# dqvp:rowExpression, the check is DuckDB SQL.
FORMAT_STANDARDS = {
    "UTC":      r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$",
    "ISO 8601": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$",
}
DIM_CHECK     = {"Completeness": "c-completeness", "Compliance": "c-compliance",
                 "Consistency": "c-consistency"}

# The EHDS requirements predate dataspace.json; these are its settings.
DEFAULT_SPACE = {
    "namespaces": {"attribute": "https://w3id.org/ehds/patsum/attr/",
                   "asset":     "https://w3id.org/ehds/patsum/asset/",
                   "policy":    "https://example.org/ehds/"},
    "policyId":        "ps-dq-offer-01",
    "descriptor":      "dataset_csv.ttl",
    "policyFile":      "policy_from_dqr.ttl",
    "constraintNames": "dimension",
    "entities":        {},
}


def leaf(name):  # "A.B.attr" / "attr" -> "attr"
    return str(name).split(".")[-1]


def column(space, attr):
    """The column holding a data-model attribute: the data space's mapping for it,
    else the attribute's own name."""
    return space.get("attributes", {}).get(attr) or leaf(attr)


def value_set(s):  # "{a, b}" -> ["a", "b"]
    return [v.strip() for v in str(s).strip("{}").split(",") if v.strip()]


def turtle_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def value_predicate(col, operator, values):
    """SQL predicate for 'the attribute must <operator> values from the set <values>'.

    NULL in the set stands for the absent value, as in DQR3EH's {NULL}.
    """
    allows_null = any(v.upper() == "NULL" for v in values)
    listed = [v for v in values if v.upper() != "NULL"]
    inside = (f'"{col}" IN (' + ", ".join("'" + v.replace("'", "''") + "'" for v in listed) + ")"
              if listed else "FALSE")
    if operator == "contain":
        return f'({inside} OR "{col}" IS NULL)' if allows_null else inside
    if operator == "not contain":
        return (f'("{col}" IS NOT NULL AND NOT ({inside}))' if allows_null
                else f'("{col}" IS NULL OR NOT ({inside}))')
    raise ValueError(f"DQRP4 operator must be 'contain' or 'not contain', not {operator!r}")


def entity_binding(space, name):
    return space.get("entities", {}).get(name, {})


def target_asset(req, space=DEFAULT_SPACE):
    """The asset a requirement is checked on: its datasetName, else the asset the
    data space binds its entity to."""
    p = req["parameters"]
    if "datasetName" in p:
        return p["datasetName"].replace(".", "_")
    for key in ("entityName", "entityName1"):
        bound = entity_binding(space, p.get(key)).get("asset")
        if bound:
            return bound
    raise ValueError(f"{req['id']}: no datasetName and no asset bound to its entity in dataspace.json")


def check_names(reqs, space=DEFAULT_SPACE):
    """Constraint names: by requirement id, or by quality dimension where the data
    space asks for it. A second requirement of the same dimension takes its id."""
    if space.get("constraintNames") == "id":
        return ["c-" + r["id"].lower() for r in reqs]
    names = []
    for r in reqs:
        name = DIM_CHECK.get(r["qualityDimension"]["dimension"])
        names.append(name if name and name not in names else "c-" + r["id"].lower())
    return names


def _filter_line(attr, op, vals, space=DEFAULT_SPACE):
    inner = " ;\n                  ".join(
        ["a dqvp:RowFilter",
         f"dqvp:filterAttribute ab:{column(space, attr)}",
         f"dqvp:filterOperator  {op}",
         *[f'dqvp:filterValue     "{v}"' for v in vals]])
    return f"    dqvp:filter [ {inner} ] ;"


def constraint(req, space=DEFAULT_SPACE, name=None):
    """(local name, Turtle block) for one DQR's DQConstraint."""
    pid  = req["pattern"]["id"]
    spec = PATTERN_SPEC[pid]
    p    = req["parameters"]
    name = name or check_names([req], space)[0]
    head = [f'ex:{name} a dqvp:DQConstraint ; rdfs:label "{name}" ;']

    # DQRP3 against a format standard: the share of values written in that format.
    if pid == "DQRP3" and p.get("standardName") in FORMAT_STANDARDS:
        col  = column(space, p[spec["target"]])
        expr = f"regexp_matches(CAST(\"{col}\" AS VARCHAR), '{FORMAT_STANDARDS[p['standardName']]}')"
        return name, "\n".join(head + [
            "    odrl:leftOperand     dqvp:RowExpressionRate ;",
            f"    dqvp:targetAttribute ab:{col} ;",
            f"    dqvp:rowExpression   {turtle_string(expr)} ;",
            "    odrl:operator        odrl:gteq ;",
            f'    odrl:rightOperand    "{p.get("percentageValue", "100")}"^^xsd:decimal .'])

    # DQRP4 with a value set other than {NULL}: the share of filtered records
    # whose second attribute satisfies the set condition.
    vals2 = value_set(p.get("ValueSet2", "{NULL}"))
    if pid == "DQRP4" and not (p.get("operator", "contain") == "contain" and vals2 == ["NULL"]):
        col = column(space, p[spec["target"]])
        return name, "\n".join(head + [
            "    odrl:leftOperand     dqvp:RowExpressionRate ;",
            f"    dqvp:targetAttribute ab:{col} ;",
            _filter_line(p[spec["filterAttr"]], spec["filterOp"], value_set(p[spec["filterVals"]]), space),
            f"    dqvp:rowExpression   {turtle_string(value_predicate(col, p['operator'], vals2))} ;",
            "    odrl:operator        odrl:gteq ;",
            '    odrl:rightOperand    "100"^^xsd:decimal .'])

    # DQRP5 over an association ("Wetland.monitoringSite") counts per owner.
    if pid == "DQRP5":
        group = entity_binding(space, p.get("entityName")).get("groupBy")
        op    = OP_WORDS.get(p.get(spec["operatorParam"], ""), "odrl:gteq")
        return name, "\n".join(head + [
            f"    odrl:leftOperand     {spec['metric']} ;",
            *([f"    dqvp:groupBy         ab:{group} ;"] if group else []),
            f"    odrl:operator        {op} ;",
            f'    odrl:rightOperand    "{p[spec["right"]]}"^^{spec["rightType"]} .'])

    lines = head + [f"    odrl:leftOperand     {spec['metric']} ;"]
    if "target" in spec:
        lines.append(f"    dqvp:targetAttribute ab:{column(space, p[spec['target']])} ;")
    if "referenceStandard" in spec:
        std = p[spec["referenceStandard"]]
        iri = STANDARD_IRIS.get(std, f"https://www.iso.org/standard/{std.replace(' ', '_')}")
        lines.append(f"    dqvp:referenceStandard <{iri}> ;")
    if "filterAttr" in spec:
        lines.append(_filter_line(p[spec["filterAttr"]], spec["filterOp"],
                                  value_set(p[spec["filterVals"]]), space))

    op    = spec.get("operator") or OP_WORDS.get(p.get(spec.get("operatorParam"), ""), "odrl:gteq")
    rval  = spec.get("rightFixed", p.get(spec.get("right"), "100"))
    rtype = spec.get("rightType", "")
    ro    = f'"{rval}"^^{rtype}' if rtype else f'"{rval}"'
    lines += [f"    odrl:operator        {op} ;",
              f"    odrl:rightOperand    {ro} ."]
    return name, "\n".join(lines)


def build_policy(reqs, policy_id=None, space=None):
    """One odrl:permission per asset, each carrying that asset's constraints. Without
    a space, the one beside the DQRs (REQ_DIR/dataspace.json) applies."""
    space     = load_space() if space is None else space
    ns        = space["namespaces"]
    policy_id = policy_id or space["policyId"]
    assigner  = "".join(w.capitalize() for w in reqs[0]["sourceEntity"].split())
    by_asset  = {}
    for req, name in zip(reqs, check_names(reqs, space)):
        by_asset.setdefault(target_asset(req, space), []).append(constraint(req, space, name))

    permissions = [
        "    odrl:permission [\n"
        "        a            odrl:Permission ;\n"
        f"        odrl:target  dp:{asset} ;\n"
        "        odrl:action  odrl:use ;\n"
        f"        odrl:constraint {', '.join(f'ex:{n}' for n, _ in blocks)}\n"
        "    ]"
        for asset, blocks in by_asset.items()]
    return (
        "@prefix odrl: <http://www.w3.org/ns/odrl/2/> .\n"
        "@prefix dqvp: <https://feed-upc.github.io/DQVP-position/dqvp.ttl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .\n"
        f"@prefix ab:   <{ns['attribute']}> .\n"
        f"@prefix dp:   <{ns['asset']}> .\n"
        f"@prefix ex:   <{ns['policy']}> .\n\n"
        f"ex:{assigner} a odrl:Party .\n"
        + "".join(f"dp:{asset} a odrl:Asset .\n" for asset in by_asset) + "\n"
        f"ex:{policy_id}\n"
        "    a dqvp:QualityPolicy, odrl:Offer ;\n"
        "    odrl:profile  <https://feed-upc.github.io/DQVP-position/dqvp.ttl> ;\n"
        f"    odrl:assigner ex:{assigner} ;\n"
        + " ;\n".join(permissions) + " .\n\n"
        + "\n\n".join(b for blocks in by_asset.values() for _, b in blocks) + "\n")


def read_requirements(req_dir=REQ_DIR):
    """Every DQR record in a folder, whatever its pattern."""
    req_dir = Path(req_dir)
    if not req_dir.is_dir():
        raise FileNotFoundError(
            f"no DQR folder at {req_dir}. The DQRs live in DS-DataQualityRequirements "
            "(prototype/requirements); use this repository as its tools/dqvp submodule, "
            "or set DQVP_REQUIREMENTS to a folder of DQR*.json records.")
    return [json.loads(p.read_text()) for p in sorted(req_dir.glob("DQR*.json"))]


def load_requirements(req_dir=REQ_DIR):
    """The DQRs whose pattern has a DQVP mapping (PATTERN_SPEC)."""
    return [r for r in read_requirements(req_dir) if r["pattern"]["id"] in PATTERN_SPEC]


def load_space(req_dir=REQ_DIR):
    """The folder's dataspace.json over DEFAULT_SPACE, or DEFAULT_SPACE alone."""
    f = Path(req_dir) / "dataspace.json"
    return {**DEFAULT_SPACE, **json.loads(f.read_text())} if f.exists() else dict(DEFAULT_SPACE)


if __name__ == "__main__":
    req_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REQ_DIR
    space = load_space(req_dir)
    reqs = load_requirements(req_dir)
    print(f"mapped {len(reqs)} DQRs: " + ", ".join(f"{r['id']} ({r['pattern']['id']})" for r in reqs))
    skipped = [r for r in read_requirements(req_dir) if r["pattern"]["id"] not in PATTERN_SPEC]
    if skipped:
        print("not mapped (no DQVP shape for the pattern yet): "
              + ", ".join(f"{r['id']} ({r['pattern']['id']})" for r in skipped))
    ttl = build_policy(reqs, space=space)
    out = ART / space["policyFile"]; out.write_text(ttl)
    print("\n" + ttl)

    print("=" * 60, "\nrun the pipeline on the MAPPED policy:\n")
    plan = C.build_plan(space["descriptor"], policy=out)
    for cid, (v, ok) in sorted(C.execute(plan).items()):
        print(f"  {cid:16s} {('—' if v is None else f'{v:8.4f}')}  "
              f"{'SAT' if ok else 'VIOLATED'}")
