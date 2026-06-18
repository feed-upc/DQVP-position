# DQVP - A Minimum ODRL Data Quality Validation Profile

A minimal ODRL 2.2 profile that lets data-quality requirements be expressed as contractual
constraints inside a policy, plus a pipeline that compiles such a policy to executable
validation code.

## Profile

[`dqvp.ttl`](dqvp.ttl) — namespace `https://feed-upc.github.io/DQVP-position/dqvp.ttl#`.

Needs Python 3 with `rdflib jinja2 great_expectations pandas duckdb pycountry jupyter`, plus
the [EYE reasoner](https://github.com/eyereasoner/eye) on `PATH` (used for the N3 rules).

```
cd operationalization-pipeline
python run.py
```

## Data

`data/Patient_Summary.{csv,json,parquet}` is synthetic example data.

