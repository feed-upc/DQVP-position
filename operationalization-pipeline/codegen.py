"""Codegen + execution + DQV measurements.

ONE generic binder over the workflow graph + graph-stored Jinja templates.
Context = the parameters of the operations that carry pc:forCheck <check>.
The filter clause is built once per check (operator- and set-aware) and
injected as gxFilter/sqlFilter/sodaFilter. Reference standards are resolved by
IRI. Execution records dqv:QualityMeasurement on the MeasureValue.
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

from jinja2 import Environment
from rdflib import Graph, Literal, Namespace, RDF, URIRef
from rdflib.namespace import XSD

HERE = Path(__file__).parent
PC   = Namespace("https://feed-upc.github.io/DQVP-position/pc#")
TMPL = Namespace("https://feed-upc.github.io/DQVP-position/tmpl#")
DQVP = Namespace("https://feed-upc.github.io/DQVP-position/dqvp.ttl#")
DQV  = Namespace("http://www.w3.org/ns/dqv#")
ODRL = Namespace("http://www.w3.org/ns/odrl/2/")
VOCAB_INPUTS  = [HERE.parent / "dqvp.ttl", HERE / "pc-vocab.ttl", HERE / "config.ttl"]
DEFAULT_POLICY = HERE / "policy.ttl"
JINJA = Environment()

# operator local name -> (sql symbol, python symbol)
OPSYM = {"gteq": (">=", ">="), "lteq": ("<=", "<="), "eq": ("=", "=="),
         "gt": (">", ">"), "lt": ("<", "<"), "neq": ("<>", "!=")}


def local(t) -> str:
    s = str(t)
    for sep in ("#", "/"):
        if sep in s: s = s.rsplit(sep, 1)[1] or s
    return s


def eye(inputs, rules) -> Graph:
    p = subprocess.run(["eye", "--quiet", "--nope", "--pass-only-new",
                        *map(str, inputs), *map(str, rules)],
                       capture_output=True, text=True, timeout=120)
    g = Graph(); g.parse(data=p.stdout, format="n3"); return g


# ---- reference-standard resolution -----------------------------------------

@functools.lru_cache(maxsize=8)
def resolve_codes(iri) -> tuple:
    """Resolve a reference-standard IRI to a code list. Keyed by IRI; raises on
    an unknown standard instead of silently returning the wrong codes."""
    s = str(iri)
    if "iso-3166" in s or "authority/country" in s:
        import pycountry
        return tuple(sorted(c.alpha_2 for c in pycountry.countries))
    raise NotImplementedError(f"no code-list resolver for reference standard <{s}>")


def build_plan(dataset="dataset_csv.ttl", policy=None) -> Graph:
    """Compile the policy to a Policy Checker for a given data-product descriptor
    (which fixes the data FORMAT + locator of the Load operation). `policy`
    overrides the default hand-written policy.ttl (e.g. one mapped from a DQR)."""
    inputs = VOCAB_INPUTS + [Path(policy) if policy else DEFAULT_POLICY, HERE / dataset]
    derived = eye(inputs, [HERE / "rules" / "plan.n3"])
    g = Graph()
    for src in inputs: g.parse(src, format="turtle")
    for t in derived: g.add(t)
    return g


# ---- data-format dimension: Load's Dataset input -> format + locator -------

def dataset_of(plan):
    """(format, absolute locator) from the workflow's Load -> Dataset input."""
    for op in plan.subjects(RDF.type, PC.Load):
        ds = plan.value(op, PC.input)
        fmt, loc = plan.value(ds, PC["format"]), plan.value(ds, PC.locator)  # PC.format == str.format!
        if fmt is None or loc is None:
            return None, None
        loc = str(loc)
        return str(fmt), (loc if loc.startswith("/") else str((HERE / loc).resolve()))
    return None, None


@functools.lru_cache(maxsize=1)
def _load_templates():
    g = Graph().parse(HERE / "templates.ttl", format="turtle")
    return {(str(g.value(t, TMPL.loadEngine)), str(g.value(t, TMPL.loadFormat))):
            str(g.value(t, TMPL.loadBody)) for t in g.subjects(TMPL.loadBody, None)}


def load_code(plan, engine):
    """Format-specific data-access code for an engine (rendered from a graph template)."""
    fmt, loc = dataset_of(plan)
    body = _load_templates().get((engine, fmt))
    if body is None:
        raise NotImplementedError(f"no load template for engine={engine} format={fmt}")
    return JINJA.from_string(body).render(locator=loc, format=fmt)


# ---- workflow accessors ----------------------------------------------------

def checks(plan):
    return sorted(plan.subjects(RDF.type, DQVP.DQConstraint), key=str)


def measure_op(plan, c):
    for op in plan.subjects(PC.forCheck, c):
        if (op, RDF.type, PC.Measure) in plan:
            return op
    return None


def ref_standard(plan, c):
    for op in plan.subjects(PC.forCheck, c):
        if (op, RDF.type, PC.Resolve) in plan:
            for p in plan.objects(op, PC.parameter):
                if str(plan.value(p, PC.name)) == "referenceStandard":
                    return plan.value(p, PC.value)
    return None


def context_of(plan, c) -> dict:
    """Flatten parameters of the check's operations; repeated names -> list."""
    ctx = {"checkId": local(c)}
    for op in plan.subjects(PC.forCheck, c):
        for p in plan.objects(op, PC.parameter):
            n, v = str(plan.value(p, PC.name)), str(plan.value(p, PC.value))
            if n in ctx:
                ctx[n] = ctx[n] + [v] if isinstance(ctx[n], list) else [ctx[n], v]
            else:
                ctx[n] = v
    return ctx


# ---- filter clause: operator- and set-aware, one place ---------------------

def _vals(ctx):
    v = ctx.get("filterValue")
    return [] if v is None else (v if isinstance(v, list) else [v])


def filter_clause(engine, ctx) -> str:
    col = ctx.get("filterColumn")
    if not col:
        return ""
    vals, op = _vals(ctx), ctx.get("filterOp", "eq")
    if op in ("eq", "isAnyOf"):                       # set membership
        if engine == "gx":
            pred = f'{col}=="{vals[0]}"' if len(vals) == 1 else f"{col} in {vals!r}"
            return f", row_condition='{pred}', condition_parser=\"pandas\""
        inlist = ", ".join(f"'{v}'" for v in vals)
        if engine == "sql":  return f' where "{col}" in ({inlist})'
        if engine == "soda": return f"\n      filter: {col} IN ({inlist})"
    else:                                             # scalar comparison
        ssym, psym = OPSYM[op]; v = vals[0]
        if engine == "gx":   return f", row_condition='{col}{psym}\"{v}\"', condition_parser=\"pandas\""
        if engine == "sql":  return f" where \"{col}\" {ssym} '{v}'"
        if engine == "soda": return f"\n      filter: {col} {ssym} '{v}'"
    return ""


def deequ_note(ctx) -> str:
    if ctx.get("filterColumn"):
        return (f"  -- NOTE: row filter {ctx['filterColumn']} {ctx.get('filterOp','eq')} "
                f"{_vals(ctx)} not applied (no native PyDeequ row filter)")
    return ""


# ---- the generic binder ----------------------------------------------------

@functools.lru_cache(maxsize=1)
def _templates():
    g = Graph().parse(HERE / "templates.ttl", format="turtle")
    return {(str(g.value(t, TMPL.engine)), g.value(t, TMPL.metric)): str(g.value(t, TMPL.body))
            for t in g.subjects(TMPL.body, None)}


def generate(plan):
    tpl = _templates()
    engines = sorted({e for e, _ in tpl})
    code = {e: {} for e in engines}
    for c in checks(plan):
        ctx = context_of(plan, c)
        ctx["gxFilter"]   = filter_clause("gx", ctx)
        ctx["sqlFilter"]  = filter_clause("sql", ctx)
        ctx["sodaFilter"] = filter_clause("soda", ctx)
        ctx["deequNote"]  = deequ_note(ctx)
        m = plan.value(measure_op(plan, c), PC.metric)
        for e in engines:
            body = tpl.get((e, m))
            if not body:
                continue
            out = JINJA.from_string(body).render(**ctx)
            if "__CODES__" in out:                       # inline the resolved list
                codes = resolve_codes(ref_standard(plan, c))
                out = out.replace("__CODES__", "[" + ", ".join(f"'{x}'" for x in codes) + "]")
            code[e][local(c)] = out
    return code


# ---- reference executor (mechanical) — value SQL reuses filter_clause -------

def _measure_sql(plan, c, ctx):
    col = ctx.get("column"); where = filter_clause("sql", ctx)
    m = local(plan.value(measure_op(plan, c), PC.metric))
    if m == "NonNullRate":
        return f'select 100.0*count("{col}")/nullif(count(*),0) from data{where}'
    if m == "NullRate":
        return f'select 100.0*(count(*)-count("{col}"))/nullif(count(*),0) from data{where}'
    if m == "ReferenceConformance":
        codes = resolve_codes(ref_standard(plan, c))
        inlist = ", ".join(f"'{x}'" for x in codes)
        return (f'select 100.0*count(*) filter (where "{col}" in ({inlist}))'
                f'/nullif(count("{col}"),0) from data{where}')
    raise NotImplementedError(m)


def execute(plan):
    import duckdb
    con = duckdb.connect()
    con.execute(f"CREATE TABLE data AS SELECT * FROM {load_code(plan, 'duckdb')}")  # format-aware
    out = {}
    for c in checks(plan):
        ctx = context_of(plan, c)
        v = con.execute(_measure_sql(plan, c, ctx)).fetchone()[0]
        v = None if v is None else round(float(v), 4)
        op = local(plan.value(c, ODRL.operator))
        ok = None if v is None else eval(f"{v} {OPSYM[op][1]} {float(ctx['threshold'])}")
        out[local(c)] = (v, ok)
    return out


def measurements_graph(plan, results):
    g = Graph(); g.bind("dqv", DQV); g.bind("pc", PC)
    for c in checks(plan):
        v, ok = results[local(c)]
        if v is None: continue
        mval = plan.value(measure_op(plan, c), PC.output)
        g.add((mval, RDF.type, DQV.QualityMeasurement))
        g.add((mval, DQV.isMeasurementOf, plan.value(measure_op(plan, c), PC.metric)))
        g.add((mval, DQV.value, Literal(str(v), datatype=XSD.decimal)))
        g.add((mval, PC.verdict, Literal(bool(ok))))
    return g


# ---- GX live execution; CODE_LIST resolved per check -----------------------

def run_gx(code_gx, plan):
    import contextlib, io
    import great_expectations as gx
    import pandas as pd
    df = eval(load_code(plan, "gx"), {"pd": pd})        # run the generated load code
    id2c = {local(c): c for c in checks(plan)}
    out = {}
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        v = gx.get_context().sources.add_pandas("s").read_dataframe(df, asset_name="a")
        for cid, expr in code_gx.items():
            std = ref_standard(plan, id2c[cid])
            ns = {"validator": v, "CODE_LIST": list(resolve_codes(std)) if std else []}
            out[cid] = bool(eval(expr, ns).success)
    return out


def assemble_sodacl(frags):
    return ("checks for patient_summary:\n"
            + "\n".join(frags[k] for k in sorted(frags)) + "\n")
