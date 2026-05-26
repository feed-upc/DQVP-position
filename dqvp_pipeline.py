"""Standalone DQVP operationalisation module."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from rdflib import BNode, Graph, Literal, Namespace, RDF, URIRef
from rdflib.collection import Collection

logger = logging.getLogger(__name__)

TB   = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#")
AB   = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#")
ODRL = Namespace("http://www.w3.org/ns/odrl/2/")
DQVP = Namespace("https://w3id.org/odrl/profile/dataquality#")

_ODRL_OP_MAP = {
    "at least": "odrl:gteq", "≥": "odrl:gteq", ">=": "odrl:gteq",
    "at most":  "odrl:lteq", "≤": "odrl:lteq", "<=": "odrl:lteq",
    "equal":    "odrl:eq",   "=":  "odrl:eq",
    "contain":  "odrl:isIncludedIn",
}

_DQVP_PATTERN_MAP = {
    "DQRP2": {
        "leftOperand":    "dqvp:NonNullRate",
        "targetAttribute": "attributeName",
        "rightOperand":   "percentageValue",
        "rightType":      "xsd:decimal",
    },
    "DQRP3": {
        "leftOperand":        "dqvp:ReferenceConformance",
        "targetAttribute":    "attributeName",
        "referenceStandard":  "standardName",
        "operator":           "odrl:gteq",
        "rightOperand":       "percentageValue",
        "rightType":          "xsd:decimal",
    },
    "DQRP4": {
        "leftOperand":        "dqvp:NullRate",
        "targetAttribute":    "attributeName2",
        "conditionAttribute": "attributeName1",
        "conditionValues":    "ValueSet1",
        "operator":           "odrl:gteq",
        "rightOperand_fixed": "100",
        "rightType":          "xsd:decimal",
    },
}

_STANDARD_IRIS = {
    "ISO 3166":   "<https://www.iso.org/iso-3166-country-codes.html>",
    "ISO 3166-1": "<https://www.iso.org/iso-3166-country-codes.html>",
    "ICD-10":     "<https://www.who.int/classifications/icd/en/>",
    "SNOMED CT":  "<http://snomed.info/sct>",
}


def build_dqvp_turtle(requirement: dict, template: dict = {}) -> str:
    """Map a DQR requirement JSON to a DQVP-conformant Turtle policy string."""
    pid  = requirement.get("pattern", {}).get("id", "")
    spec = _DQVP_PATTERN_MAP.get(pid)
    if not spec:
        raise ValueError(f"No DQVP mapping for pattern '{pid}' "
                         f"(supported: {list(_DQVP_PATTERN_MAP)})")

    params   = requirement.get("parameters", {})
    req_id   = requirement["id"]
    dataset  = params.get("datasetName", params.get("entityName", "UnknownEntity")).replace(".", "_")

    odrl_op = spec.get("operator") or _ODRL_OP_MAP.get(params.get("operator", ""), "odrl:gteq")

    if "rightOperand_fixed" in spec:
        right_val  = spec["rightOperand_fixed"]
        right_type = spec.get("rightType", "")
    else:
        right_val  = params.get(spec["rightOperand"], "")
        right_type = spec.get("rightType", "")

    right_turtle = f'"{right_val}"^^{right_type}' if right_type else f'"{right_val}"'

    lines = [
        f"            [ a             dqvp:DQConstraint, odrl:Constraint ;",
        f"              odrl:leftOperand  {spec['leftOperand']} ;",
    ]

    if "targetAttribute" in spec and spec["targetAttribute"] in params:
        attr = params[spec["targetAttribute"]].split(".")[-1]
        lines.append(f'              dqvp:targetAttribute ab:{attr} ;')

    if "referenceStandard" in spec:
        std_name = params.get(spec["referenceStandard"], "")
        std_iri  = _STANDARD_IRIS.get(std_name,
                   f"<https://www.iso.org/standard/{std_name.replace(' ', '_')}>")
        lines.append(f"              dqvp:referenceStandard {std_iri} ;")

    if "conditionAttribute" in spec and spec["conditionAttribute"] in params:
        cond_attr = params[spec["conditionAttribute"]].split(".")[-1]
        lines.append(f'              dqvp:conditionAttribute ab:{cond_attr} ;')

    if "conditionValues" in spec and spec["conditionValues"] in params:
        raw_vals = params[spec["conditionValues"]].strip("{}")
        lines.append(f'              dqvp:conditionValues    "{raw_vals}" ;')

    lines += [
        f"              odrl:operator     {odrl_op} ;",
        f"              odrl:rightOperand {right_turtle} ]",
    ]

    constraint_block = "\n".join(lines)

    return f"""\
@prefix odrl:    <http://www.w3.org/ns/odrl/2/> .
@prefix dqvp:    <https://w3id.org/odrl/profile/dataquality#> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .
@prefix ab:      <https://w3id.org/ehds/patsum/attr/> .
@prefix dp:      <https://w3id.org/ehds/patsum/asset/> .
@prefix ex:      <https://example.org/ehds/> .

ex:EHDSAuthority  a odrl:Party .
dp:{dataset}      a odrl:Asset .

ex:{req_id}-offer
    a dqvp:QualityPolicy, odrl:Offer ;
    odrl:uid      ex:{req_id}-offer ;
    odrl:profile  <https://w3id.org/odrl/profile/dataquality> ;
    odrl:assigner ex:EHDSAuthority ;
    odrl:obligation [
        a             dqvp:QualityDuty, odrl:Duty ;
        odrl:target   dp:{dataset} ;
        odrl:action   dqvp:validate ;
        dqvp:allChecks (
{constraint_block}
        )
    ] .
"""


def build_combined_dqvp_turtle(requirements: list, policy_id: str = "ps-dq-offer-01") -> str:
    """Build a single dqvp:QualityPolicy with all DQRs in one dqvp:allChecks group."""
    all_constraint_blocks = []
    dataset = "PatientSummary"

    for requirement in requirements:
        pid  = requirement.get("pattern", {}).get("id", "")
        spec = _DQVP_PATTERN_MAP.get(pid)
        if not spec:
            continue
        params = requirement.get("parameters", {})

        odrl_op = spec.get("operator") or _ODRL_OP_MAP.get(params.get("operator", ""), "odrl:gteq")

        if "rightOperand_fixed" in spec:
            right_val  = spec["rightOperand_fixed"]
            right_type = spec.get("rightType", "")
        else:
            right_val  = params.get(spec["rightOperand"], "")
            right_type = spec.get("rightType", "")

        right_turtle = f'"{right_val}"^^{right_type}' if right_type else f'"{right_val}"'

        lines = [
            f"          [ a             dqvp:DQConstraint, odrl:Constraint ;",
            f"            odrl:leftOperand  {spec['leftOperand']} ;",
        ]

        if "targetAttribute" in spec and spec["targetAttribute"] in params:
            attr = params[spec["targetAttribute"]].split(".")[-1]
            lines.append(f'            dqvp:targetAttribute ab:{attr} ;')

        if "referenceStandard" in spec:
            std_name = params.get(spec["referenceStandard"], "")
            std_iri  = _STANDARD_IRIS.get(std_name,
                       f"<https://www.iso.org/standard/{std_name.replace(' ', '_')}>")
            lines.append(f"            dqvp:referenceStandard {std_iri} ;")

        if "conditionAttribute" in spec and spec["conditionAttribute"] in params:
            cond_attr = params[spec["conditionAttribute"]].split(".")[-1]
            lines.append(f'            dqvp:conditionAttribute ab:{cond_attr} ;')

        if "conditionValues" in spec and spec["conditionValues"] in params:
            raw_vals = params[spec["conditionValues"]].strip("{}")
            lines.append(f'            dqvp:conditionValues    "{raw_vals}" ;')

        lines += [
            f"            odrl:operator     {odrl_op} ;",
            f"            odrl:rightOperand {right_turtle} ]",
        ]
        all_constraint_blocks.append("\n".join(lines))

    constraints = "\n".join(all_constraint_blocks)

    return f"""\
@prefix odrl:    <http://www.w3.org/ns/odrl/2/> .
@prefix dqvp:    <https://w3id.org/odrl/profile/dataquality#> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .
@prefix ab:      <https://w3id.org/ehds/patsum/attr/> .
@prefix dp:      <https://w3id.org/ehds/patsum/asset/> .
@prefix ex:      <https://example.org/ehds/> .

ex:EHDSAuthority  a odrl:Party .
dp:{dataset}      a odrl:Asset .

ex:{policy_id}
    a dqvp:QualityPolicy, odrl:Offer ;
    odrl:uid      ex:{policy_id} ;
    odrl:profile  <https://w3id.org/odrl/profile/dataquality> ;
    odrl:assigner ex:EHDSAuthority ;
    odrl:obligation [
        a             dqvp:QualityDuty, odrl:Duty ;
        odrl:target   dp:{dataset} ;
        odrl:action   dqvp:validate ;
        dqvp:allChecks (
{constraints}
        )
    ] .
"""


def _skolemize(graph: Graph) -> Graph:
    """Replace blank nodes with stable hash-based URIs."""
    bnode_map: dict[BNode, URIRef] = {}

    def _uri(bn: BNode) -> URIRef:
        if bn not in bnode_map:
            h = hashlib.md5(str(bn).encode()).hexdigest()[:8]
            bnode_map[bn] = AB[f"genid-{h}"]
        return bnode_map[bn]

    out = Graph()
    for prefix, ns in graph.namespaces():
        out.bind(prefix, ns)
    for s, p, o in graph:
        s2 = _uri(s) if isinstance(s, BNode) else s
        o2 = _uri(o) if isinstance(o, BNode) else o
        out.add((s2, p, o2))
    return out


def plan_dqvp(ttl_path: str, data_path: str,
              rules_dir: Optional[str] = None,
              eye_cmd: str = "eye") -> Graph:
    """
    Compile a DQVP Turtle policy into a tbox:Operation plan graph.

    Flattens dqvp:allChecks RDF lists, bootstraps one PolicyChecker + LoadData
    per dqvp:QualityPolicy, then runs EYE with the N3 rules in rules_dir.
    Returns a skolemized graph with tbox:Operation nodes.
    """
    if rules_dir is None:
        rules_dir = Path(__file__).parent / "rules_n3_dqvp"
    rules_dir = Path(rules_dir)
    if not rules_dir.exists():
        raise FileNotFoundError(f"rules_dir not found: {rules_dir}")

    rule_files = sorted(rules_dir.glob("*.n3"))
    if not rule_files:
        raise FileNotFoundError(f"No .n3 files found in {rules_dir}")

    policy = Graph()
    policy.parse(ttl_path, format="turtle")

    for policy_uri in policy.subjects(RDF.type, DQVP.QualityPolicy):
        for obligation in policy.objects(policy_uri, ODRL.obligation):
            head = policy.value(obligation, DQVP.allChecks)
            if head:
                for constraint in Collection(policy, head):
                    policy.add((obligation, ODRL.constraint, constraint))

    bootstrap = Graph()
    data_lit  = Literal(data_path)
    for policy_uri in policy.subjects(RDF.type, DQVP.QualityPolicy):
        pc   = BNode(); load = BNode(); inp = BNode()
        bootstrap.add((pc,   RDF.type,        TB.PolicyChecker))
        bootstrap.add((pc,   TB.accordingTo,  policy_uri))
        bootstrap.add((pc,   TB.nextStep,     load))
        bootstrap.add((load, RDF.type,        TB.Operation))
        bootstrap.add((load, TB.hasAbstract,  AB.LoadData))
        bootstrap.add((load, TB.hasInput,     inp))
        bootstrap.add((inp,  TB.value,        data_lit))
        bootstrap.add((inp,  TB.role,         Literal("filePath")))
        bootstrap.add((load, TB.hasOutput,    AB.data))

    combined = Graph()
    for prefix, ns in policy.namespaces():
        combined.bind(prefix, ns)
    for t in policy:    combined.add(t)
    for t in bootstrap: combined.add(t)

    with tempfile.TemporaryDirectory() as tmp:
        data_file  = Path(tmp) / "data.n3"
        query_file = Path(tmp) / "query.n3"
        combined.serialize(destination=str(data_file), format="n3")
        query_file.write_text("{ ?s ?p ?o } => { ?s ?p ?o } .\n")

        cmd = [eye_cmd, str(data_file)] + [str(r) for r in rule_files] \
              + ["--query", str(query_file), "--nope"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"EYE failed:\n{proc.stderr}")

        result = Graph()
        result.parse(data=proc.stdout, format="n3")

    return _skolemize(result)


def generate_gx_code(plan: Graph, code_metadata_path: Optional[str] = None) -> str:
    """
    Walk the plan graph and emit a run_gx_validation(data_path) function.

    Emits version-adaptive code: GX 1.x fluent API when available, GX 0.18
    from_pandas API otherwise.
    """
    if code_metadata_path is None:
        code_metadata_path = Path(__file__).parent / "code_metadata.json"
    meta = json.loads(Path(code_metadata_path).read_text())

    impl_to_gx: dict[str, str] = {}
    for node in meta.get("@graph", []):
        impl_id = node.get("@id", "")
        gx_type = node.get("tb:gxExpectationType", {})
        if isinstance(gx_type, dict):
            gx_type = gx_type.get("@value", "")
        if impl_id and gx_type:
            impl_to_gx[impl_id] = gx_type

    def _short(uri) -> str:
        return str(uri).split("#")[-1].split("/")[-1]

    def _role_inputs(op_uri) -> dict:
        out = {}
        for inp in plan.objects(op_uri, TB.hasInput):
            role  = _short(plan.value(inp, TB.role) or "")
            value = plan.value(inp, TB.value)
            if role and value is not None:
                out[role] = value.toPython() if hasattr(value, "toPython") else str(value)
        return out

    pre_lines: list[str] = []
    exp_1x:    list[str] = []
    exp_0x:    list[str] = []

    for pc in plan.subjects(RDF.type, TB.PolicyChecker):
        load_op  = plan.value(pc, TB.nextStep)
        branches = list(plan.objects(load_op, TB.nextStep)) if load_op else []
        for branch in branches:
            abstract = plan.value(branch, TB.hasAbstract)
            ab_name  = _short(abstract) if abstract else ""
            if not ab_name:
                continue
            inputs   = _role_inputs(branch)
            col      = _short(inputs.get("targetAttribute", ""))

            terminal  = plan.value(branch, TB.nextStep)
            t_inputs  = _role_inputs(terminal) if terminal else {}
            threshold = t_inputs.get("threshold")
            mostly    = float(threshold) / 100.0 if threshold is not None else None
            mostly_s  = f", mostly={mostly}" if mostly is not None else ""

            if ab_name == "CheckCompleteness":
                exp_1x.append(f"suite.add_expectation(gx.expectations.ExpectColumnValuesToNotBeNull(column='{col}'{mostly_s}))")
                exp_0x.append(f"_results.append(_gdf.expect_column_values_to_not_be_null(column='{col}'{mostly_s}))")

            elif ab_name == "CheckValidity":
                pre_lines.append("import pycountry as _pc")
                pre_lines.append("_iso3166 = [c.alpha_2 for c in _pc.countries]")
                exp_1x.append(f"suite.add_expectation(gx.expectations.ExpectColumnValuesToBeInSet(column='{col}', value_set=_iso3166{mostly_s}))")
                exp_0x.append(f"_results.append(_gdf.expect_column_values_to_be_in_set(column='{col}', value_set=_iso3166{mostly_s}))")

            elif ab_name == "CheckConsistency":
                cond_col = _short(inputs.get("conditionAttribute", ""))
                cond_val = inputs.get("conditionValues", "")
                target   = _short(inputs.get("targetAttribute", ""))
                exp_val  = inputs.get("expectedValue", "NULL")
                pre_lines.append(f"_df = _df[_df['{cond_col}'] == '{cond_val}'].copy()")
                if exp_val == "NULL":
                    exp_1x.append(f"suite.add_expectation(gx.expectations.ExpectColumnValuesToBeNull(column='{target}'))")
                    exp_0x.append(f"_results.append(_gdf.expect_column_values_to_be_null(column='{target}'))")
                else:
                    exp_1x.append(f"suite.add_expectation(gx.expectations.ExpectColumnValuesToBeInSet(column='{target}', value_set=['{exp_val}']))")
                    exp_0x.append(f"_results.append(_gdf.expect_column_values_to_be_in_set(column='{target}', value_set=['{exp_val}']))")

    lines = [
        "import great_expectations as gx",
        "import pandas as pd",
        "",
        "_GX1 = int(gx.__version__.split('.')[0]) >= 1",
        "",
        "def run_gx_validation(data_path: str) -> bool:",
        "    _df = pd.read_csv(data_path)",
    ]
    for line in pre_lines:
        lines.append(f"    {line}")
    if pre_lines:
        lines.append("")
    lines += [
        "    if _GX1:",
        "        context = gx.get_context(mode='ephemeral')",
        "        suite   = context.suites.add(gx.ExpectationSuite(name='dqvp_suite'))",
    ]
    for exp in exp_1x:
        lines.append(f"        {exp}")
    lines += [
        "        batch = context.data_sources.add_pandas('src').add_dataframe_asset('asset')",
        "        batch = batch.add_batch_definition_whole_dataframe('bd').get_batch(batch_parameters={'dataframe': _df})",
        "        return bool(batch.validate(suite)['success'])",
        "    else:",
        "        _gdf = gx.from_pandas(_df)",
        "        _results = []",
    ]
    for exp in exp_0x:
        lines.append(f"        {exp}")
    lines.append("        return all(r.success for r in _results)")

    return "\n".join(lines)
