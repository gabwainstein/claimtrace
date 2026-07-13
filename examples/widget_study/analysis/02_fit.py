"""Ordinary-least-squares fit of shininess ~ polish_minutes. (stdlib only)"""
import csv
import json
import os

xs, ys = [], []
with open("data/clean.csv") as f:
    for r in csv.DictReader(f):
        xs.append(float(r["polish_minutes"]))
        ys.append(float(r["shininess"]))

n = len(xs)
xb, yb = sum(xs) / n, sum(ys) / n
sxx = sum((x - xb) ** 2 for x in xs)
sxy = sum((x - xb) * (y - yb) for x, y in zip(xs, ys))
syy = sum((y - yb) ** 2 for y in ys)
slope = sxy / sxx
intercept = yb - slope * xb
r2 = (slope * sxy) / syy

os.makedirs("results", exist_ok=True)
with open("results/fit.json", "w") as f:
    json.dump({"slope": round(slope, 4), "intercept": round(intercept, 4),
               "r2": round(r2, 4), "n": n}, f, indent=2)
print(f"slope={slope:.4f} intercept={intercept:.4f} r2={r2:.4f} -> results/fit.json")
