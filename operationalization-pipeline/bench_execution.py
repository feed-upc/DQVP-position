"""How does executing the generated checks scale with data volume and check count?

Planning depends only on the policy (see bench_planning.py). Data size enters at
execution, where each check is one aggregate query run by the engine. This
replicates the 500-record synthetic Patient Summary to larger sizes and times
loading and checking separately: first for the three mapped EHDS DQRs at
increasing row counts, then for synthetic policies with more checks at the
largest size. Every figure is the median of several runs.

Replication keeps every proportion intact, so the verdicts must not change with
size; the script asserts that as it goes.

Run:  python bench_execution.py [max_rows] [repeats]
"""
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import duckdb

import codegen as C
import dqr_to_dqvp as M
from bench_planning import synth_policy

SOURCE = (HERE.parent / "data" / "Patient_Summary.csv").resolve()
BENCH = HERE / "artifacts" / "bench"; BENCH.mkdir(parents=True, exist_ok=True)
BASE_ROWS = 500


def make_csv(n):
    """The source CSV repeated n / 500 times."""
    out = BENCH / f"patient_summary_{n}.csv"
    if not out.exists():
        duckdb.sql(f"COPY (SELECT s.* FROM read_csv('{SOURCE}') s, range({n // BASE_ROWS})) "
                   f"TO '{out}' (HEADER)")
    return out


def descriptor(csv):
    ttl = BENCH / f"{csv.stem}.ttl"
    ttl.write_text("@prefix dp: <https://w3id.org/ehds/patsum/asset/> .\n"
                   "@prefix pc: <https://feed-upc.github.io/DQVP-position/pc#> .\n"
                   f'dp:PatientSummary pc:format "csv" ; pc:locator "{csv}" .\n')
    return ttl


def load(plan):
    """A fresh connection with the plan's assets loaded, as C.execute does it."""
    con = duckdb.connect()
    t0 = time.perf_counter()
    tables = {}
    for wf, asset in C.workflows(plan):
        for a, _fmt, _loc in C.loads_of(plan, wf):
            con.execute(f'CREATE OR REPLACE TABLE "{C.sql_table(a)}" AS '
                        f'SELECT * FROM {C.load_code(plan, "duckdb", wf, a)}')
        tables[wf] = C.sql_table(asset)
    return con, tables, time.perf_counter() - t0


def run_checks(plan, con, tables):
    """The generated metric query for every check, as C.execute issues them."""
    t0 = time.perf_counter()
    values = {}
    for c in C.checks(plan):
        sql = C._measure_sql(plan, c, C.context_of(plan, c), tables[C.workflow_of_check(plan, c)])
        values[C.local(c)] = round(float(con.execute(sql).fetchone()[0]), 4)
    return time.perf_counter() - t0, values


def median_load(plan, repeats):
    times = []
    for _ in range(repeats):
        con, _tables, t = load(plan)
        con.close()
        times.append(t)
    return statistics.median(times)


def median_checks(plan, con, tables, repeats):
    runs = [run_checks(plan, con, tables) for _ in range(repeats)]
    return statistics.median(t for t, _ in runs), runs[0][1]


def by_rows(sizes, repeats):
    policy = M.ART / "policy_from_dqr.ttl"
    # The first three EHDS DQRs, the set the published timings were measured on.
    reqs = [r for r in M.load_requirements() if r["id"] in ("DQR1EH", "DQR2EH", "DQR3EH")]
    policy.write_text(M.build_policy(reqs))
    print(f"Three mapped EHDS DQRs, increasing data volume (median of {repeats})")
    print(f"{'rows':>10} {'load s':>8} {'checks s':>9} {'ms/check':>9} {'x per 10x rows':>15}")
    print("-" * 55)
    reference, prev = None, None
    for n in sizes:
        plan = C.build_plan(descriptor(make_csv(n)), policy=policy)
        t_load = median_load(plan, repeats)
        con, tables, _ = load(plan)
        t_checks, values = median_checks(plan, con, tables, repeats)
        con.close()
        assert reference is None or values == reference, (n, values, reference)
        reference = values
        ratio = "" if prev is None else f"{t_checks / prev:.2f}x"
        print(f"{n:>10,} {t_load:>8.3f} {t_checks:>9.4f} "
              f"{1000 * t_checks / len(values):>9.2f} {ratio:>15}")
        prev = t_checks
    print(f"  measurements identical at every size: {reference}\n")


def by_checks(n, counts, repeats):
    csv = descriptor(make_csv(n))
    con = tables = None
    print(f"Synthetic policies over {n:,} rows, increasing check count (median of {repeats})")
    print(f"{'checks':>7} {'checks s':>9} {'ms/check':>9}")
    print("-" * 27)
    for k in counts:
        pol = BENCH / f"policy_{k}.ttl"
        pol.write_text(synth_policy(k))
        plan = C.build_plan(csv, policy=pol)
        if con is None:                     # every policy targets the same asset
            con, tables, _ = load(plan)
        t_checks, values = median_checks(plan, con, tables, repeats)
        print(f"{len(values):>7} {t_checks:>9.4f} {1000 * t_checks / len(values):>9.2f}")
        pol.unlink()
    con.close()


if __name__ == "__main__":
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    sizes = [n for n in (500, 5_000, 50_000, 500_000, 5_000_000) if n <= cap]
    by_rows(sizes, repeats)
    by_checks(sizes[-1], (3, 12, 48, 96), repeats)
