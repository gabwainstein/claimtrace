"""Fetch the two official palmerpenguins v0.1.0 CSV files with hash gates."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen


SOURCES = {
    "penguins_raw.csv": {
        "url": "https://raw.githubusercontent.com/allisonhorst/palmerpenguins/v0.1.0/inst/extdata/penguins_raw.csv",
        "bytes": 53098,
        "sha256": "144f623143c9360fd77322a4f86acb06dc198814dbd2669724c63e6457b907bd",
    },
    "penguins.csv": {
        "url": "https://raw.githubusercontent.com/allisonhorst/palmerpenguins/v0.1.0/inst/extdata/penguins.csv",
        "bytes": 15241,
        "sha256": "f204db2c753b0937caac3cb35258562c14f073e4bbc76be24b4c51ce22767a93",
    },
}


def fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "claimtrace-penguin-demo/1.0"})
    with urlopen(request, timeout=30) as response:
        return response.read()


def main() -> None:
    downloaded = {}
    for filename, expected in SOURCES.items():
        payload = fetch(expected["url"])
        actual_hash = hashlib.sha256(payload).hexdigest()
        if len(payload) != expected["bytes"]:
            raise RuntimeError(
                f"{filename}: expected {expected['bytes']} bytes, received {len(payload)}"
            )
        if actual_hash != expected["sha256"]:
            raise RuntimeError(
                f"{filename}: expected SHA-256 {expected['sha256']}, received {actual_hash}"
            )
        downloaded[filename] = payload

    source_dir = Path("data/source")
    source_dir.mkdir(parents=True, exist_ok=True)
    for filename, payload in downloaded.items():
        (source_dir / filename).write_bytes(payload)

    manifest = {
        "schema_version": "claimtrace.penguin-source-manifest/1",
        "dataset_version": "palmerpenguins-v0.1.0",
        "license": "CC0-1.0",
        "license_url": "https://allisonhorst.github.io/palmerpenguins/LICENSE.html",
        "package_url": "https://allisonhorst.github.io/palmerpenguins/",
        "package_doi": "10.5281/zenodo.3960218",
        "package_citation": (
            "Horst AM, Hill AP, Gorman KB (2020). palmerpenguins: Palmer "
            "Archipelago (Antarctica) penguin data. R package version 0.1.0."
        ),
        "original_study_doi": "10.1371/journal.pone.0090081",
        "sources": {
            filename: {
                "url": expected["url"],
                "bytes": expected["bytes"],
                "sha256": expected["sha256"],
            }
            for filename, expected in SOURCES.items()
        },
    }
    Path("results").mkdir(exist_ok=True)
    with Path("results/source_manifest.json").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print("fetched and verified palmerpenguins v0.1.0 raw and curated CSV files")


if __name__ == "__main__":
    main()
