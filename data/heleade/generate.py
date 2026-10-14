"""Synthetic data for the HELEADE wetlands scenario (IST paper, Section 6.2).

Follows the excerpt of the HELEADE data model: Wetland, MonitoringSite and
Observation, where water-level, weather and biodiversity observations share one
table and are told apart by observationType. The three wetlands are the real
ones of southern Alicante; everything else is generated.

Each quality defect is a switch, so the same generator gives the shipped dataset,
a clean one and one with every defect:

  missing_location   one monitoring site has no location            (DQR1HL)
  non_utc            five observation timestamps are not UTC        (DQR2HL)
  breeding_conflict  three 'notDetected' records report breeding    (DQR3HL)
  thin_wetland       one wetland has only four monitoring sites     (DQR4HL)

The shipped dataset turns on non_utc and thin_wetland, so DQR2HL and DQR4HL are
violated and DQR1HL and DQR3HL are satisfied.

Run:  python generate.py [out_dir] [--clean | --all-defects]
"""
import csv
import random
import sys
from pathlib import Path

WETLANDS = [("WL-HONDO", "El Hondo"),
            ("WL-SPOLA", "Salinas de Santa Pola"),
            ("WL-MATA", "Lagunas de La Mata y Torrevieja")]
CENTRE = {"WL-HONDO": (38.27, -0.70), "WL-SPOLA": (38.18, -0.62), "WL-MATA": (38.03, -0.70)}
TAXA = ["Marmaronetta angustirostris", "Phoenicopterus roseus", "Himantopus himantopus"]
BREEDING = ["courtship", "nesting", "chicks"]
OBSERVATIONS_PER_SITE = 30

SHIPPED = {"non_utc", "thin_wetland"}
ALL = {"missing_location", "non_utc", "breeding_conflict", "thin_wetland"}


def generate(out_dir, defects=SHIPPED, seed=7):
    rng = random.Random(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with open(out / "wetland.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["wetlandId", "name"])
        w.writerows(WETLANDS)

    per_wetland = {"WL-HONDO": 7, "WL-SPOLA": 6,
                   "WL-MATA": 4 if "thin_wetland" in defects else 5}
    sites = []
    for wid, n in per_wetland.items():
        lat, lon = CENTRE[wid]
        for i in range(1, n + 1):
            point = f"POINT({lon + rng.uniform(-.03, .03):.5f} {lat + rng.uniform(-.03, .03):.5f})"
            sites.append([f"{wid}-S{i}", f"{wid} site {i}", point, wid])
    if "missing_location" in defects:
        sites[3][2] = ""
    with open(out / "monitoring_site.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["siteId", "name", "location", "wetlandId"])
        w.writerows(sites)

    rows = []
    for site in sites:
        for k in range(OBSERVATIONS_PER_SITE):
            kind = ("waterLevel", "weather", "biodiversity")[k % 3]
            observed = f"2026-04-{1 + k % 28:02d}T{(6 + k) % 24:02d}:00:00Z"
            level = status = breeding = taxon = ""
            if kind == "waterLevel":
                level = f"{rng.uniform(0.2, 1.4):.2f}"
            if kind == "biodiversity":
                status = rng.choice(["detected", "notDetected"])
                breeding = rng.choice(BREEDING + [""]) if status == "detected" else ""
                taxon = rng.choice(TAXA)
            rows.append([f"OBS-{len(rows) + 1:05d}", site[0], observed, kind,
                         level, status, breeding, taxon])
    if "non_utc" in defects:
        for i in (10, 200, 333, 410):                 # local time with an offset
            rows[i][2] = rows[i][2].replace("Z", "+02:00")
        rows[500][2] = rows[500][2].replace("T", " ").replace("Z", "")   # no zone
    if "breeding_conflict" in defects:
        conflicting = [r for r in rows if r[5] == "notDetected"][:3]
        for r, evidence in zip(conflicting, BREEDING):
            r[6] = evidence
    with open(out / "observation.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["observationId", "siteId", "observedAt", "observationType",
                    "waterLevel", "detectionStatus", "breedingEvidence", "taxon"])
        w.writerows(rows)
    return {"wetlands": len(WETLANDS), "sites": len(sites), "observations": len(rows)}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    defects = set() if "--clean" in sys.argv else ALL if "--all-defects" in sys.argv else SHIPPED
    print(generate(args[0] if args else Path(__file__).parent, defects))
