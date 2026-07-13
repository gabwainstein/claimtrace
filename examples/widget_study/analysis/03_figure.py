"""Render the scatter + fit line as a standalone SVG. (stdlib only — no matplotlib)"""
import csv
import json
import os

xs, ys = [], []
with open("data/clean.csv") as f:
    for r in csv.DictReader(f):
        xs.append(float(r["polish_minutes"]))
        ys.append(float(r["shininess"]))
fit = json.load(open("results/fit.json"))
slope, intercept = fit["slope"], fit["intercept"]

W, H, P = 400, 300, 40
xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)


def sx(x):
    return P + (x - xmin) / (xmax - xmin) * (W - 2 * P)


def sy(y):
    return H - P - (y - ymin) / (ymax - ymin) * (H - 2 * P)


dots = "".join(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3" fill="#2171b5"/>'
               for x, y in zip(xs, ys))
y0, y1 = slope * xmin + intercept, slope * xmax + intercept
line = (f'<line x1="{sx(xmin):.1f}" y1="{sy(y0):.1f}" x2="{sx(xmax):.1f}" y2="{sy(y1):.1f}" '
        f'stroke="#cb181d" stroke-width="2"/>')
label = f'<text x="{P}" y="22" font-family="sans-serif" font-size="13">shininess ~ {slope:.2f}*polish + {intercept:.1f}</text>'
svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">{dots}{line}{label}</svg>'

os.makedirs("figures", exist_ok=True)
with open("figures/fit.svg", "w") as f:
    f.write(svg)
print("wrote figures/fit.svg")
