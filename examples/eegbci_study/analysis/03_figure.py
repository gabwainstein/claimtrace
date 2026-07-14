"""Render observed fold scores and the permutation null as a standalone SVG."""
from __future__ import annotations

import html
import json
from pathlib import Path


def main() -> None:
    result = json.loads(Path("results/decoding.json").read_text(encoding="utf-8"))
    fold_scores = [float(fold["balanced_accuracy"]) for fold in result["folds"]]
    fold_runs = [int(fold["test_run"]) for fold in result["folds"]]
    null = [float(value) for value in result["permutation"]["scores"]]
    observed = float(result["observed_mean_balanced_accuracy"])
    null_95 = float(result["permutation"]["null_95th_percentile"])
    criterion = bool(result["criterion_met"])

    width, height = 820, 425
    left, top, plot_width, plot_height = 70, 55, 700, 235
    minimum, maximum = 0.0, 1.0

    def x(value: float) -> float:
        return left + (value - minimum) / (maximum - minimum) * plot_width

    bins = [0] * 20
    for value in null:
        index = min(len(bins) - 1, max(0, int(value * len(bins))))
        bins[index] += 1
    max_count = max(bins) if bins else 1
    bars = []
    for index, count in enumerate(bins):
        x0 = x(index / len(bins))
        x1 = x((index + 1) / len(bins))
        bar_height = (count / max_count) * plot_height
        bars.append(
            f'<rect x="{x0:.2f}" y="{top + plot_height - bar_height:.2f}" '
            f'width="{max(1.0, x1 - x0 - 1):.2f}" height="{bar_height:.2f}" fill="#91a7c0"/>'
        )

    fold_y = top + plot_height + 36
    fold_marks = []
    for index, (run, score) in enumerate(zip(fold_runs, fold_scores)):
        y = fold_y + index * 17
        fold_marks.append(
            f'<circle cx="{x(score):.2f}" cy="{y}" r="5" fill="#845ef7"/>'
            f'<text x="{left - 8}" y="{y + 4}" text-anchor="end" class="small">run {run:02d}</text>'
        )

    status = "criterion met" if criterion else "criterion not met"
    status_color = "#2b8a3e" if criterion else "#c92a2a"
    title = "S001 hands-vs-feet motor-imagery decoding"
    subtitle = (
        f"mean balanced accuracy {observed:.3f}; run-constrained null 95th percentile "
        f"{null_95:.3f}; {status}"
    )
    attribution = (
        "Contains information from PhysioNet EEG Motor Movement/Imagery Dataset v1.0.0, "
        "available under ODC Attribution License v1.0."
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<metadata>Source: https://physionet.org/content/eegmmidb/1.0.0/; license: https://physionet.org/content/eegmmidb/view-license/1.0.0/; DOI: 10.13026/C28G6P</metadata>
<desc>{html.escape(attribution)}</desc>
<style>text{{font-family:system-ui,sans-serif;fill:#202936}}.small{{font-size:11px}}.axis{{stroke:#495057;stroke-width:1}}</style>
<rect width="100%" height="100%" fill="#f8fafc"/>
<text x="{left}" y="25" font-size="18" font-weight="700">{html.escape(title)}</text>
<text x="{left}" y="44" font-size="12" fill="{status_color}">{html.escape(subtitle)}</text>
{''.join(bars)}
<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" class="axis"/>
<line x1="{x(0.5):.2f}" y1="{top}" x2="{x(0.5):.2f}" y2="{top + plot_height}" stroke="#adb5bd" stroke-dasharray="4 4"/>
<line x1="{x(null_95):.2f}" y1="{top}" x2="{x(null_95):.2f}" y2="{top + plot_height}" stroke="#e67700" stroke-width="2"/>
<line x1="{x(observed):.2f}" y1="{top}" x2="{x(observed):.2f}" y2="{top + plot_height}" stroke="#1971c2" stroke-width="3"/>
<text x="{x(null_95) + 5:.2f}" y="{top + 14}" class="small">null 95%</text>
<text x="{x(observed) + 5:.2f}" y="{top + 30}" class="small">observed mean</text>
{''.join(fold_marks)}
<text x="{left + plot_width / 2}" y="382" text-anchor="middle" font-size="12">balanced accuracy</text>
<text x="{left}" y="405" font-size="9">{html.escape(attribution)}</text>
<text x="{left}" y="417" font-size="9">Source and license URLs are embedded in this SVG's metadata.</text>
</svg>
'''
    output = Path("figures/decoding.svg")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8", newline="\n")
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
