# DQVP - A Minimum ODRL Data Quality Validation Profile

A minimal ODRL 2.2 profile that lets data-quality requirements be expressed as contractual
constraints inside a policy, plus a pipeline that compiles such a policy to executable
validation code.

## Profile

[`dqvp.ttl`](dqvp.ttl) — namespace `https://feed-upc.github.io/DQVP-position/dqvp.ttl#`.

Needs Python 3 with `rdflib jinja2 great_expectations pandas duckdb pycountry jupyter`, plus
the [EYE reasoner](https://github.com/eyereasoner/eye) on `PATH` (used for the N3 rules).

## Data

`data/Patient_Summary.{csv,json,parquet}` is synthetic example data. `data/AMR_Isolates.csv` holds synthetic AMR
isolates derived from the same patients; `data/generate_amr_isolates.py` regenerates it.
`data/heleade/` is synthetic data for the HELEADE wetlands scenario; `data/heleade/generate.py` regenerates it, with or without its quality defects.

## Reproduce the JSON-mapped EHDS DQRs

From this directory, run:

```
cd operationalization-pipeline
python dqr_to_dqvp.py
```

This reads `requirements/DQR1EH.json` through `DQR6EH.json`, maps them to a DQVP
policy, and executes the resulting checks against the Patient Summary and AMR isolates CSVs using
DuckDB. The measurements and verdicts are printed to the terminal, and the mapped
policy is written to `operationalization-pipeline/artifacts/policy_from_dqr.ttl`.

## Reproduce the HELEADE DQRs

The second data space of the paper (HELEADE, Section 6.2) runs through the same chain:

```
cd operationalization-pipeline
python dqr_to_dqvp.py ../requirements/heleade
```

This reads `requirements/heleade/DQR1HL.json` through `DQR4HL.json`, which instantiate
patterns DQRP2 to DQRP5. `requirements/heleade/dataspace.json` gives the data space's
namespaces and binds its entities to assets; for example, `Wetland.monitoringSite` is
counted per `wetlandId`. The command maps the DQRs to a DQVP policy with one permission
per asset (`artifacts/policy_from_dqr_heleade.ttl`) and executes it with DuckDB against
`data/heleade/`, using the descriptor `dataset_heleade_csv.ttl`.

The descriptor reads `observedAt` as text (`pc:textAttribute`). DQR2HL checks how the
timestamp is written, and type inference would otherwise rewrite it before the check sees it.

`python test_heleade.py` checks every measured value against an oracle computed from the
CSV files, and runs each check on a clean and a fully defective variant of the data.

## Measurements reported in the paper

From `operationalization-pipeline`:

- `python bench_planning.py 256` times planning and code generation for growing policies.
- `python bench_execution.py 5000000 5` times execution as the data and the number of checks grow.
- `python automation_report.py` counts, for each DQR, what is authored and what is generated.

## Generate the saved measurement artifact

From `operationalization-pipeline`, run:

```
python run.py
```

This executes the broader checked-in `policy.ttl` workflow and writes the plan,
generated validation artifacts, and DQV measurements to the `artifacts/` directory,
including `artifacts/measurements.ttl`.

