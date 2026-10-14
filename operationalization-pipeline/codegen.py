"""Codegen + execution + DQV measurements.

ONE generic binder over the workflow graph + graph-stored Jinja templates.
Context = the parameters of the operations that carry pc:forCheck <check>.
The filter clause is built once per check (operator- and set-aware) and
injected as gxFilter/sqlFilter/sodaFilter. Reference standards are resolved by
IRI. Execution records dqv:QualityMeasurement on the MeasureValue.
"""
from __future__ import annotations

import functools
import re
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
JINJA.filters["pystr"] = repr      # a Python string literal, whatever quotes it holds

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

def workflows(plan):
    """(workflow, asset) pairs. One workflow per asset: a dataflow has a single
    Table, so it cannot span assets."""
    return sorted(((wf, plan.value(wf, PC.onAsset))
                   for wf in plan.subjects(RDF.type, PC.Workflow)),
                  key=lambda t: str(t[1]))


def workflow_of_check(plan, c):
    """The workflow whose Table feeds this check's branch."""
    for op in plan.subjects(PC.forCheck, c):
        for wf in plan.subjects(PC.hasOp, op):
            return wf
    return None


def asset_of_check(plan, c):
    wf = workflow_of_check(plan, c)
    return plan.value(wf, PC.onAsset) if wf else None


def loads_of(plan, wf):
    """(asset, format, locator) for every Load in a workflow.

    A workflow normally has one, over the asset it validates. A cross-asset
    constraint adds a second, over the asset it must look values up in.
    """
    out = []
    for op in plan.objects(wf, PC.hasOp):
        if (op, RDF.type, PC.Load) not in plan:
            continue
        ds = plan.value(op, PC.input)
        asset = plan.value(ds, PC.ofAsset)
        fmt, loc = plan.value(ds, PC["format"]), plan.value(ds, PC.locator)
        if fmt is None or loc is None:
            continue
        loc = str(loc)
        out.append((asset, str(fmt),
                    loc if loc.startswith("/") else str((HERE / loc).resolve())))
    return out


def dataset_of(plan, wf, asset=None):
    """(format, absolute locator) for a workflow's Load.

    With no asset given, the one over the workflow's own subject asset.
    """
    want = asset if asset is not None else plan.value(wf, PC.onAsset)
    for op in plan.objects(wf, PC.hasOp):
        if (op, RDF.type, PC.Load) not in plan:
            continue
        ds = plan.value(op, PC.input)
        if plan.value(ds, PC.ofAsset) != want:
            continue
        fmt, loc = plan.value(ds, PC["format"]), plan.value(ds, PC.locator)  # PC.format == str.format!
        if fmt is None or loc is None:
            return None, None
        loc = str(loc)
        return str(fmt), (loc if loc.startswith("/") else str((HERE / loc).resolve()))
    return None, None


def sql_table(asset):
    """The relation name an engine addresses this asset by."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", local(asset)).lower()


@functools.lru_cache(maxsize=1)
def _load_templates():
    g = Graph().parse(HERE / "templates.ttl", format="turtle")
    return {(str(g.value(t, TMPL.loadEngine)), str(g.value(t, TMPL.loadFormat))):
            str(g.value(t, TMPL.loadBody)) for t in g.subjects(TMPL.loadBody, None)}


def text_attributes(plan, asset):
    """Attributes the data-product descriptor asks to be read as written
    (pc:textAttribute). A format check needs the original text: type inference
    would turn '2026-04-01T08:00:00+02:00' into a timestamp and hide the offset."""
    return sorted(str(v) for v in plan.objects(asset, PC.textAttribute))


def dbt_source(plan, asset):
    """The dbt source an asset is declared under (pc:dbtSource), 'ehds' by default."""
    return str(plan.value(asset, PC.dbtSource) or "ehds")


def load_code(plan, engine, wf, asset=None):
    """Format-specific data-access code for one of a workflow's assets."""
    fmt, loc = dataset_of(plan, wf, asset)
    body = _load_templates().get((engine, fmt))
    if body is None:
        raise NotImplementedError(f"no load template for engine={engine} format={fmt}")
    subject = asset if asset is not None else plan.value(wf, PC.onAsset)
    return JINJA.from_string(body).render(locator=loc, format=fmt,
                                          textColumns=text_attributes(plan, subject))


# ---- workflow accessors ----------------------------------------------------

def checks(plan):
    return sorted(plan.subjects(RDF.type, DQVP.DQConstraint), key=str)


def measure_op(plan, c):
    """The operation carrying pc:metric.

    Keyed on the property rather than the class: a scalar shape puts the metric
    on its Measure, a row-predicate shape puts it on the Aggregate that reduces
    the mask. Both are "the operation that produced the measured value".
    """
    for op in plan.subjects(PC.forCheck, c):
        if plan.value(op, PC.metric) is not None:
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
        asset = asset_of_check(plan, c)
        if asset is None:                    # flagged as an unsupported rule type
            continue
        ctx["sourceTable"] = sql_table(asset)
        # dbt's own {{ source() }} is built here rather than guarded with
        # {% raw %} in every template, so the relation follows the asset.
        ctx["dbtSource"] = ("{{ source('" + dbt_source(plan, asset) + "','"
                            + ctx["sourceTable"] + "') }}")
        want = _expected(ctx)
        if want:
            ctx["gxColumnSet"] = "[" + ", ".join(f'"{a}"' for a in want) + "]"
            ctx["sqlColumnList"] = ", ".join(f"'{a}'" for a in want)
            ctx["sodaColumnList"] = "[" + ", ".join(want) + "]"
            ctx["expectedCount"] = len(want)
        enc = ctx.get("missingEncoding")
        if enc is not None:
            enc = enc if isinstance(enc, list) else [enc]
            ctx["sqlMissingList"] = ", ".join(f"'{e}'" for e in enc)
            ctx["sodaMissingList"] = "[" + ", ".join(enc) + "]"
        if ctx.get("referenceTable"):
            ctx["refTable"] = sql_table(ctx["referenceTable"])
            ctx["dbtRefSource"] = ("{{ source('" + dbt_source(plan, ctx["referenceTable"])
                                   + "','" + ctx["refTable"] + "') }}")
        if ctx.get("expectedDatatype"):
            ctx["sqlType"] = XSD_TO_SQL.get(ctx["expectedDatatype"], "")
        m = plan.value(measure_op(plan, c), PC.metric)
        # A constraint with a baseline is a drift requirement: what an engine has
        # to express is the comparison against history, not the base metric.
        b = plan.value(c, DQVP.baseline)
        if b is not None:
            m = plan.value(b, RDF.type) or m
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

class _NeedsOwnQuery(Exception):
    """Raised by a metric whose SQL is not an aggregate over the source relation
    and so cannot be slotted into the standard select-from shape."""


SCHEMA_METRICS = {"SchemaConformance", "AttributeTypeConformance"}

# XSD datatype -> a pattern matched against the engine's declared column type.
# The contract carries the XSD IRI; each engine maps it, and one with no
# corresponding type refuses rather than approximating.
XSD_TO_SQL = {
    "string": "VARCHAR", "date": "DATE", "dateTime": "TIMESTAMP",
    "integer": "INT", "int": "INT", "long": "BIGINT",
    "decimal": "DECIMAL", "double": "DOUBLE", "float": "FLOAT",
    "boolean": "BOOLEAN",
}


def _expected(ctx):
    v = ctx.get("expectedAttribute")
    return [] if v is None else (v if isinstance(v, list) else [v])


def _measure_source(plan, c, table):
    """The relation a metric is measured over - the records, or the schema."""
    m = local(plan.value(measure_op(plan, c), PC.metric))
    return f'(describe "{table}")' if m in SCHEMA_METRICS else f'"{table}"'


def _measure_expr(plan, c, ctx):
    """The metric's SQL expression, before any grouping is applied."""
    col = ctx.get("column")
    m = local(plan.value(measure_op(plan, c), PC.metric))
    if m == "NonNullRate":
        return f'100.0*count("{col}")/nullif(count(*),0)'
    if m == "NullRate":
        return f'100.0*(count(*)-count("{col}"))/nullif(count(*),0)'
    if m == "ReferenceConformance":
        codes = resolve_codes(ref_standard(plan, c))
        inlist = ", ".join(f"'{x}'" for x in codes)
        return (f'100.0*count(*) filter (where "{col}" in ({inlist}))'
                f'/nullif(count("{col}"),0)')
    if m == "UniqueRate":
        return f'100.0*count(distinct "{col}")/nullif(count("{col}"),0)'
    if m == "RowCount":
        return "count(*)"
    if m == "AttributeAgreementRate":
        col2 = ctx.get("column2")
        return (f'100.0*count(*) filter (where "{col}" = "{col2}")'
                f'/nullif(count(*) filter (where "{col}" is not null'
                f' and "{col2}" is not null),0)')
    if m == "RowComparison":
        # A record-level predicate reduced by the declared aggregation. The
        # comparison is a parameter, not frozen into the metric.
        col2, rop = ctx.get("column2"), ctx.get("rowSqlOp")
        pred = f'"{col}" {rop} "{col2}"'
        defined = f'"{col}" is not null and "{col2}" is not null'
        return _aggregate(ctx, pred, defined)
    if m == "RowExpressionRate":
        return _aggregate(ctx, ctx.get("rowExpression"), None)
    if m == "Quantile":
        q = float(ctx.get("quantile", 0.5))
        return f'quantile_cont("{col}", {q})'
    if m in ("ModeShare", "ShareImbalance"):
        raise _NeedsOwnQuery                      # see _measure_sql
    if m == "ExplicitNullRate":
        enc = ctx.get("missingEncoding")
        enc = [] if enc is None else (enc if isinstance(enc, list) else [enc])
        inlist = ", ".join(f"'{e}'" for e in enc)
        return (f'100.0*count(*) filter (where "{col}" is null'
                f' or "{col}" not in ({inlist}))/nullif(count(*),0)')
    if m == "Correlation":
        col2 = ctx.get("column2")
        return f'corr(try_cast("{col}" as double), try_cast("{col2}" as double))'
    if m == "ReferentialIntegrity":
        ref_t, ref_c = ctx.get("referenceTable"), ctx.get("referenceColumn")
        return (f'100.0*count(*) filter (where "{col}" in '
                f'(select "{ref_c}" from "{sql_table(ref_t)}"))'
                f'/nullif(count(*) filter (where "{col}" is not null),0)')
    if m == "SchemaConformance":
        want = _expected(ctx)
        inlist = ", ".join(f"'{a}'" for a in want)
        return f"100.0*count(*) filter (where column_name in ({inlist}))/{len(want)}"
    if m == "AttributeTypeConformance":
        dt = ctx.get("expectedDatatype")
        pat = XSD_TO_SQL.get(dt)
        if pat is None:
            raise NotImplementedError(
                f"no SQL type for xsd:{dt}; known: {', '.join(sorted(XSD_TO_SQL))}")
        return (f"100.0*count(*) filter (where column_name = '{col}'"
                f" and upper(column_type) like '%{pat}%')")
    raise NotImplementedError(m)


def _aggregate(ctx, predicate, defined):
    """Reduce a record-level predicate to a scalar, per pc:aggregation.

    'rate' counts over records where the predicate is defined, so nulls neither
    pass nor fail; the other reductions are over all records in scope.
    """
    agg = ctx.get("aggregation", "rate")
    over = f" filter (where {defined})" if defined else ""
    if agg == "rate":
        return (f"100.0*count(*) filter (where {predicate})"
                f"/nullif(count(*){over},0)")
    if agg == "count":
        return f"count(*) filter (where {predicate})"
    if agg == "all":
        return f"(count(*){over} = count(*) filter (where {predicate}))"
    if agg == "any":
        return f"(count(*) filter (where {predicate}) > 0)"
    raise NotImplementedError(
        f"pc:aggregation '{agg}': expected one of rate, count, all, any")


#  Universal quantification collapses to the worst partition: for a lower-bound
#  operator every group clears the threshold iff the minimum does, and likewise
#  the maximum for an upper bound. Operators with no worst case (eq, neq) cannot
#  be reduced this way and are refused rather than silently evaluated globally.
_GROUP_AGG = {"gteq": "min", "gt": "min", "lteq": "max", "lt": "max"}


def _measure_sql(plan, c, ctx, table):
    src = _measure_source(plan, c, table)
    metric = local(plan.value(measure_op(plan, c), PC.metric))
    if metric in ("ModeShare", "ShareImbalance"):
        # Group by value, then compare the group sizes: a nested query rather
        # than an aggregate over the source relation.
        col, where = ctx.get("column"), filter_clause("sql", ctx)
        top = "max(cnt)" if metric == "ModeShare" else "(max(cnt)-min(cnt))"
        return (f'select 100.0*{top}/nullif(sum(cnt),0) from '
                f'(select count(*) as cnt from {src}{where} '
                f'{"and" if where else "where"} "{col}" is not null group by "{col}")')
    # A row filter over a schema is meaningless, so it is not applied there.
    where = "" if src.startswith("(describe") else filter_clause("sql", ctx)
    expr = _measure_expr(plan, c, ctx)
    gcol = ctx.get("groupColumn")
    if not gcol:
        return f"select {expr} from {src}{where}"
    op = local(plan.value(c, ODRL.operator))
    agg = _GROUP_AGG.get(op)
    if agg is None:
        raise NotImplementedError(
            f"dqvp:groupBy with odrl:operator {op}: universal quantification over "
            f"partitions has no worst-case aggregate for this operator"
        )
    return (f"select {agg}(r) from (select {expr} as r from {src}{where} "
            f'group by "{gcol}")')


HISTORY = HERE / "artifacts" / "history.ttl"


def _history_graph():
    g = Graph()
    if HISTORY.exists():
        g.parse(HISTORY, format="turtle")
    return g


def _measure_key(plan, c):
    """What makes two measurements comparable over time.

    Not the constraint - two constraints may measure the same thing - and not
    the metric alone: c-completeness and c-percountry both measure NonNullRate
    of the same column on the same asset, but one is global and the other is the
    worst partition, so their histories must not mix.
    """
    ctx = context_of(plan, c)
    return "|".join([
        local(plan.value(measure_op(plan, c), PC.metric)),
        local(asset_of_check(plan, c)),
        str(ctx.get("column", "")),
        str(ctx.get("groupColumn", "")),
    ])


def recall_baseline(plan, c, hist=None):
    """The baseline for a check: the mean of its most recent recorded
    measurements of the same metric on the same asset.

    Returns None when there is no history to compare against - a drift
    requirement on a first run has nothing to say, and saying nothing is the
    honest answer.
    """
    ctx = context_of(plan, c)
    key = _measure_key(plan, c)
    window = int(float(ctx.get("window", 1)))
    hist = _history_graph() if hist is None else hist

    seen = []
    for obs in hist.subjects(RDF.type, DQV.QualityMeasurement):
        if str(hist.value(obs, PC.measureKey) or "") != key:
            continue
        at, val = hist.value(obs, PC.observedAt), hist.value(obs, DQV.value)
        if at is not None and val is not None:
            seen.append((str(at), float(val)))
    if not seen:
        return None
    recent = [v for _, v in sorted(seen)[-window:]]
    return sum(recent) / len(recent)


def deviation(measured, baseline, kind):
    """How far a measurement has moved from its baseline."""
    if baseline is None or measured is None:
        return None
    if kind == "RelativeDeviation":
        if baseline == 0:
            return None
        return abs(measured - baseline) / abs(baseline) * 100.0
    return abs(measured - baseline)          # AbsoluteDeviation, in the metric's units


def record_history(plan, results, path=None):
    """Append this run's measurements so later runs have something to recall."""
    import datetime
    path = HISTORY if path is None else path
    path.parent.mkdir(exist_ok=True)
    g = _history_graph()
    now = datetime.datetime.now().isoformat()
    for c in checks(plan):
        cid = local(c)
        if cid not in results or results[cid][0] is None:
            continue
        if plan.value(c, DQVP.baseline) is not None:
            continue                          # a deviation is not itself a measurement
        obs = URIRef(f"{c}/observation/{now}")
        g.add((obs, RDF.type, DQV.QualityMeasurement))
        g.add((obs, DQV.isMeasurementOf, plan.value(measure_op(plan, c), PC.metric)))
        g.add((obs, PC.onAsset, asset_of_check(plan, c)))
        g.add((obs, PC.measureKey, Literal(_measure_key(plan, c))))
        g.add((obs, PC.observedAt, Literal(now, datatype=XSD.dateTime)))
        g.add((obs, DQV.value, Literal(results[cid][0], datatype=XSD.decimal)))
    g.serialize(destination=str(path), format="turtle")
    return g


def execute(plan):
    """One relation per asset - each check runs against its own workflow's table."""
    import duckdb
    con = duckdb.connect()
    tables, made = {}, set()
    for wf, asset in workflows(plan):
        for a, _fmt, _loc in loads_of(plan, wf):
            t = sql_table(a)
            if t not in made:            # a reference asset may be loaded twice
                con.execute(
                    f'CREATE TABLE "{t}" AS SELECT * FROM {load_code(plan, "duckdb", wf, a)}')
                made.add(t)
        tables[wf] = sql_table(asset)
    out = {}
    for c in checks(plan):
        wf = workflow_of_check(plan, c)
        if wf is None:                      # e.g. flagged as an unsupported rule type
            continue
        ctx = context_of(plan, c)
        v = con.execute(_measure_sql(plan, c, ctx, tables[wf])).fetchone()[0]
        v = None if v is None else round(float(v), 4)

        # A constraint with a baseline is bounded by how far the measurement has
        # moved, not by the measurement itself.
        if plan.value(c, DQVP.baseline) is not None:
            base = recall_baseline(plan, c)
            v = deviation(v, base, ctx.get("deviationKind", "AbsoluteDeviation"))
            v = None if v is None else round(v, 4)

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

    # The generated expectations use the fluent Validator API
    # (validator.expect_column_values_to_*), which Great Expectations removed in
    # 1.0 along with context.sources. Fail here with something actionable rather
    # than an AttributeError from inside the harness.
    if not hasattr(gx.get_context(), "sources"):
        raise NotImplementedError(
            f"great_expectations {gx.__version__} is installed, but the generated "
            f"expectations and this harness target the 0.18 fluent API. Either pin "
            f"'great_expectations<1.0', or port the gx templates in templates.ttl to "
            f"the 1.x gx.expectations.* classes and update this function."
        )

    id2c = {local(c): c for c in checks(plan)}
    out = {}
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        ctxt = gx.get_context()
        validators = {}
        for wf, asset in workflows(plan):
            df = eval(load_code(plan, "gx", wf), {"pd": pd})   # generated load code
            validators[wf] = (ctxt.sources.add_pandas(local(asset))
                                  .read_dataframe(df, asset_name=local(asset)))
        for cid, expr in code_gx.items():
            if expr.lstrip().startswith("#"):   # an explicit refusal: nothing to run
                continue
            c = id2c[cid]
            wf = workflow_of_check(plan, c)
            if wf is None:
                continue
            # A ModeShare constraint bounds the share of the most common value and
            # names no value, so the set this approximation needs does not exist.
            if "ALLOWED_MODE" in expr:
                continue
            std = ref_standard(plan, c)
            enc = context_of(plan, c).get("missingEncoding")
            ns = {"validator": validators[wf],
                  "CODE_LIST": list(resolve_codes(std)) if std else [],
                  "MISSING_ENCODINGS": [] if enc is None else (enc if isinstance(enc, list) else [enc])}
            out[cid] = bool(eval(expr, ns).success)
    return out


def assemble_sodacl(frags, plan):
    """SodaCL groups checks under the dataset they apply to, so emit one block
    per asset rather than assuming a single relation."""
    by_table = {}
    for c in checks(plan):
        cid = local(c)
        if cid not in frags:
            continue
        asset = asset_of_check(plan, c)
        if asset is None:
            continue
        by_table.setdefault(sql_table(asset), []).append(frags[cid])
    return "\n".join(f"checks for {t}:\n" + "\n".join(v)
                     for t, v in sorted(by_table.items())) + "\n"
