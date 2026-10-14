"""Synthetic AMR isolates for the EHDS scenario (DQR4EH and DQR5EH).

Each patient of the synthetic Patient Summary contributes three to six
bacterial isolates, each tested against one antimicrobial. Isolates inherit the
patient's gender, as in a surveillance extract, so the gender balance of the
study follows the patient population rather than being set by hand.

Run:  python generate_amr_isolates.py   (writes AMR_Isolates.csv next to this file)
"""
import csv
import random
from pathlib import Path

HERE = Path(__file__).parent
ORGANISMS = ["Escherichia coli", "Klebsiella pneumoniae", "Staphylococcus aureus",
             "Pseudomonas aeruginosa", "Enterococcus faecium"]
ANTIMICROBIALS = ["ciprofloxacin", "ceftriaxone", "meropenem", "vancomycin", "gentamicin"]


def generate(patients=HERE / "Patient_Summary.csv", out=HERE / "AMR_Isolates.csv", seed=11):
    rng = random.Random(seed)
    with open(patients, newline="") as f:
        people = list(csv.DictReader(f))
    rows = []
    for p in people:
        for _ in range(rng.randint(3, 6)):
            rows.append({
                "isolateId": f"ISO-{len(rows) + 1:05d}",
                "nationalHealthcarePatientID": p["nationalHealthcarePatientID"],
                "gender": p["gender"],
                "countryOfAffiliation": p["countryOfAffiliation"],
                "specimenDate": f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                "organism": rng.choice(ORGANISMS),
                "antimicrobial": rng.choice(ANTIMICROBIALS),
                "result": rng.choices(["S", "I", "R"], weights=[70, 8, 22])[0],
            })
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return len(rows)


if __name__ == "__main__":
    print(generate(), "isolates")
