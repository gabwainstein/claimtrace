"""Render pooled and within-species fits as a standalone SVG (stdlib only)."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path


COLORS = {"Adelie": "#ef7b45", "Chinstrap": "#5b8ff9", "Gentoo": "#55b981"}


def load_rows() -> list[tuple[str, float, float]]:
    rows = []
    with Path("data/penguins_clean.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["bill_length_mm"] == "NA" or row["bill_depth_mm"] == "NA":
                continue
            rows.append(
                (row["species"], float(row["bill_length_mm"]), float(row["bill_depth_mm"]))
            )
    return rows


def panel(
    rows: list[tuple[str, float, float]],
    fit: dict[str, float | int],
    title: str,
    x0: float,
    y0: float,
    width: float,
    height: float,
) -> list[str]:
    xs = [row[1] for row in rows]
    ys = [row[2] for row in rows]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    xpad = max((xmax - xmin) * 0.06, 0.5)
    ypad = max((ymax - ymin) * 0.10, 0.3)
    xmin, xmax = xmin - xpad, xmax + xpad
    ymin, ymax = ymin - ypad, ymax + ypad

    def sx(value: float) -> float:
        return x0 + (value - xmin) / (xmax - xmin) * width

    def sy(value: float) -> float:
        return y0 + height - (value - ymin) / (ymax - ymin) * height

    elements = [
        f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{width:.1f}" height="{height:.1f}" rx="8" fill="#f8fafc" stroke="#cbd5e1"/>',
        f'<text x="{x0:.1f}" y="{y0 - 14:.1f}" class="panel-title">{title}</text>',
    ]
    for species, bill_length, bill_depth in rows:
        elements.append(
            f'<circle cx="{sx(bill_length):.2f}" cy="{sy(bill_depth):.2f}" r="2.4" fill="{COLORS[species]}" fill-opacity="0.62"/>'
        )
    line_y0 = float(fit["slope"]) * xmin + float(fit["intercept"])
    line_y1 = float(fit["slope"]) * xmax + float(fit["intercept"])
    elements.append(
        f'<line x1="{sx(xmin):.2f}" y1="{sy(line_y0):.2f}" x2="{sx(xmax):.2f}" y2="{sy(line_y1):.2f}" stroke="#172554" stroke-width="2.5"/>'
    )
    elements.extend(
        [
            f'<text x="{x0 + 8:.1f}" y="{y0 + 18:.1f}" class="fit">slope = {float(fit["slope"]):.3f}; r = {float(fit["pearson_r"]):.3f}</text>',
            f'<text x="{x0:.1f}" y="{y0 + height + 17:.1f}" class="tick">{min(xs):.1f}</text>',
            f'<text x="{x0 + width:.1f}" y="{y0 + height + 17:.1f}" text-anchor="end" class="tick">{max(xs):.1f}</text>',
            f'<text x="{x0 - 7:.1f}" y="{y0 + height:.1f}" text-anchor="end" class="tick">{min(ys):.1f}</text>',
            f'<text x="{x0 - 7:.1f}" y="{y0 + 8:.1f}" text-anchor="end" class="tick">{max(ys):.1f}</text>',
        ]
    )
    return elements


def main() -> None:
    rows = load_rows()
    with Path("results/slopes.json").open(encoding="utf-8") as handle:
        result = json.load(handle)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(row)

    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="620" viewBox="0 0 960 620">',
        "<title>Palmer Penguins pooled and species-specific bill associations</title>",
        "<style>",
        "text { font-family: ui-sans-serif, system-ui, sans-serif; fill: #172554; }",
        ".title { font-size: 23px; font-weight: 700; }",
        ".subtitle { font-size: 13px; fill: #475569; }",
        ".panel-title { font-size: 15px; font-weight: 700; }",
        ".fit { font-size: 12px; font-weight: 600; }",
        ".tick { font-size: 10px; fill: #64748b; }",
        "</style>",
        '<rect width="960" height="620" fill="#ffffff"/>',
        '<text x="45" y="38" class="title">One pooled slope, three within-species slopes</text>',
        '<text x="45" y="61" class="subtitle">bill depth (mm) versus bill length (mm); complete cases only</text>',
    ]
    svg.extend(panel(rows, result["pooled"], f"Pooled (n={len(rows)})", 72, 104, 816, 200))
    for index, species in enumerate(("Adelie", "Chinstrap", "Gentoo")):
        svg.extend(
            panel(
                grouped[species],
                result["by_species"][species],
                f"{species} (n={len(grouped[species])})",
                62 + index * 302,
                382,
                252,
                160,
            )
        )
    svg.extend(
        [
            '<text x="480" y="574" text-anchor="middle" class="panel-title">Bill length (mm)</text>',
            '<text x="18" y="330" text-anchor="middle" class="panel-title" transform="rotate(-90 18 330)">Bill depth (mm)</text>',
            '<text x="480" y="606" text-anchor="middle" class="subtitle">Pooled sign reversal is descriptive and does not identify a causal species effect.</text>',
            "</svg>",
            "",
        ]
    )
    Path("figures").mkdir(exist_ok=True)
    Path("figures/bill_slopes.svg").write_text("\n".join(svg), encoding="utf-8", newline="\n")
    print("wrote figures/bill_slopes.svg")


if __name__ == "__main__":
    main()
