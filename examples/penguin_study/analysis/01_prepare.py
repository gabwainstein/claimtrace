"""Reproduce the documented palmerpenguins raw-to-curated transformation."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path


COLUMNS = [
    "species",
    "island",
    "bill_length_mm",
    "bill_depth_mm",
    "flipper_length_mm",
    "body_mass_g",
    "sex",
    "year",
]


def normalized(value: str) -> str:
    value = value.strip()
    return "NA" if value in {"", "NA", "."} else value


def transform(row: dict[str, str]) -> dict[str, str]:
    date_egg = normalized(row["Date Egg"])
    year = "NA" if date_egg == "NA" else date_egg[:4]
    sex = normalized(row["Sex"])
    return {
        "species": normalized(row["Species"]).split(" ", 1)[0],
        "island": normalized(row["Island"]),
        "bill_length_mm": normalized(row["Culmen Length (mm)"]),
        "bill_depth_mm": normalized(row["Culmen Depth (mm)"]),
        "flipper_length_mm": normalized(row["Flipper Length (mm)"]),
        "body_mass_g": normalized(row["Body Mass (g)"]),
        "sex": "NA" if sex == "NA" else sex.lower(),
        "year": year,
    }


def main() -> None:
    raw_path = Path("data/source/penguins_raw.csv")
    official_path = Path("data/source/penguins.csv")
    with raw_path.open(encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.DictReader(handle))

    transformed = [transform(row) for row in raw_rows]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(transformed)
    generated = buffer.getvalue().encode("utf-8")
    official = official_path.read_bytes()

    if generated != official:
        generated_lines = generated.decode("utf-8").splitlines()
        official_lines = official.decode("utf-8").splitlines()
        mismatch = next(
            (
                index
                for index, (left, right) in enumerate(
                    zip(generated_lines, official_lines), start=1
                )
                if left != right
            ),
            min(len(generated_lines), len(official_lines)) + 1,
        )
        raise RuntimeError(
            "local raw-to-curated transformation does not match the official v0.1.0 "
            f"CSV; first differing line is {mismatch}"
        )

    clean_path = Path("data/penguins_clean.csv")
    clean_path.parent.mkdir(exist_ok=True)
    clean_path.write_bytes(generated)

    report = {
        "schema_version": "claimtrace.penguin-preprocess/1",
        "dataset_version": "palmerpenguins-v0.1.0",
        "source_rows": len(raw_rows),
        "output_rows": len(transformed),
        "columns": COLUMNS,
        "byte_identical_to_official_curated": True,
        "official_curated_sha256": hashlib.sha256(official).hexdigest(),
        "generated_sha256": hashlib.sha256(generated).hexdigest(),
    }
    Path("results").mkdir(exist_ok=True)
    with Path("results/preprocess.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"transformed {len(transformed)} rows; byte-identical to official curated CSV")


if __name__ == "__main__":
    main()
