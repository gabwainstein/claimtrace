"""Clean the raw widget measurements: drop blank rows. (stdlib only)"""
import csv
import os

os.makedirs("data", exist_ok=True)
rows = []
with open("data/raw_measurements.csv") as f:
    for r in csv.DictReader(f):
        if r["polish_minutes"].strip() == "" or r["shininess"].strip() == "":
            continue
        rows.append((float(r["polish_minutes"]), float(r["shininess"])))

with open("data/clean.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["polish_minutes", "shininess"])
    w.writerows(rows)
print(f"cleaned {len(rows)} rows -> data/clean.csv")
