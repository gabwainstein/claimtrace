"""Fetch the three pinned PhysioNet EEGBCI EDF files and verify SHA-256."""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path


DATASET = {
    "id": "physionet-eegmmidb-1.0.0",
    "doi": "10.13026/C28G6P",
    "license": "Open Data Commons Attribution License v1.0",
    "license_url": "https://physionet.org/content/eegmmidb/view-license/1.0.0/",
    "sha256s_url": "https://physionet.org/files/eegmmidb/1.0.0/SHA256SUMS.txt",
}
FILES = {
    "S001R06.edf": {
        "url": "https://physionet.org/files/eegmmidb/1.0.0/S001/S001R06.edf",
        "sha256": "5369364f2c4e81ca141679d6dd2ba6ece61c7eb53d7fae31241b308876e1b6b3",
    },
    "S001R10.edf": {
        "url": "https://physionet.org/files/eegmmidb/1.0.0/S001/S001R10.edf",
        "sha256": "20de1c7746c2349d16bda5e9f1b0ac7b7ad1581102a2e30dd2ac422696f62fb1",
    },
    "S001R14.edf": {
        "url": "https://physionet.org/files/eegmmidb/1.0.0/S001/S001R14.edf",
        "sha256": "2110c48e3106898e3dbca47e39b330637afd3d3b8bc2da3ba1e44f4ac1118137",
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_one(name: str, spec: dict[str, str], data_dir: Path) -> dict[str, object]:
    destination = data_dir / name
    expected = spec["sha256"]
    if destination.exists() and sha256(destination) == expected:
        print(f"verified existing {destination}")
    else:
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.unlink(missing_ok=True)
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "claimtrace-eegbci-demo/1"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
            observed = sha256(temporary)
            if observed != expected:
                raise RuntimeError(f"SHA-256 mismatch for {name}: expected {expected}, observed {observed}")
            os.replace(temporary, destination)
            print(f"downloaded and verified {destination}")
        finally:
            temporary.unlink(missing_ok=True)
    observed = sha256(destination)
    if observed != expected:
        raise RuntimeError(f"SHA-256 mismatch for {name}: expected {expected}, observed {observed}")
    return {
        "path": destination.as_posix(),
        "url": spec["url"],
        "sha256": observed,
        "size_bytes": destination.stat().st_size,
    }


def main() -> None:
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    records = [fetch_one(name, spec, data_dir) for name, spec in FILES.items()]
    manifest = {**DATASET, "files": records}
    output = data_dir / "source_manifest.json"
    output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
