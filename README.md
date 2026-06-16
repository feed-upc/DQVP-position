# DQVP — ODRL Data Quality Validation Profile

A minimal ODRL 2.2 profile that lets data-quality requirements be expressed as contractual
constraints inside a policy, plus a pipeline that compiles such a policy to executable
validation code. Companion artifact to the position paper *Bringing Data Quality to ODRL:
From Policy to Validation*.

## Profile

[`dqvp.ttl`](dqvp.ttl) — namespace `https://feed-upc.github.io/DQVP-position/dqvp.ttl#`.

Three constructs added on top of an ODRL constraint:

| Construct | Purpose |
|---|---|
| `dqvp:targetAttribute` | the column a metric applies to |
| `dqvp:referenceStandard` | an external standard to validate against |
| `dqvp:filter` / `dqvp:RowFilter` | a row-selection predicate |

Metrics (`odrl:leftOperand`, co-typed `dqv:Metric`, grounded in ISO/IEC 25012 dimensions)
are an open class.

## Pipeline

End to end: a data-quality requirement, expressed once, drives everything downstream.

```
DQR JSON  --[dqr_to_dqvp.py]-->  DQVP policy  --[plan.n3]-->  Policy Checker
          --[templates + binder]-->  check code (GX, dbt, SodaCL, PyDeequ)  -->  verdicts + DQV
```

- `requirements/*.json` — the DQR catalogue: each requirement instantiates a pattern (`DQRP*`)
  and fills its parameters. `operationalization-pipeline/dqr_to_dqvp.py` maps them to a DQVP
  policy (the same shape as `policy.ttl`) and runs the rest of the pipeline.
- `operationalization-pipeline/` turns a DQVP policy into a technology-agnostic Policy Checker
  and then into validation code, parametrised by engine (Great Expectations, dbt, SodaCL,
  PyDeequ) and data format (CSV, JSON, Parquet). Design notes in
  [`operationalization-pipeline/MODEL.md`](operationalization-pipeline/MODEL.md).
- [`operationalization.ipynb`](operationalization.ipynb) — the example, run end to end.
- `operationalization-pipeline/run.py` — the policy→validation flow as a script;
  `dqr_to_dqvp.py` — the DQR→policy front stage.

### Run

Needs Python 3 with `rdflib jinja2 great_expectations pandas duckdb pycountry jupyter`, plus
the [EYE reasoner](https://github.com/eyereasoner/eye) on `PATH` (used for the N3 rules).

```
cd operationalization-pipeline
python run.py
```

## Data

`data/Patient_Summary.{csv,json,parquet}` is synthetic example data.

## Namespace hosting

The profile IRI resolves once GitHub Pages is enabled for this repo
(Settings → Pages → branch `main`, folder `/`); `dqvp.ttl` is then served at the namespace
IRI above.
