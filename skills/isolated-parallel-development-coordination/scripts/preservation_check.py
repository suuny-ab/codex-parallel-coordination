#!/usr/bin/env python3
"""Verify an explicitly inventoried backup before cleanup. Never copies or deletes."""
import argparse
import json
from pathlib import Path
import sys

from coordination import digest, load, require


def verify_bundle(manifest):
    root = Path(manifest["backup_root"]).resolve()
    managed = [Path(x).resolve() for x in manifest["managed_roots"]]
    require(managed and all(Path(x).is_absolute() for x in manifest["managed_roots"]),
            "all managed roots must be explicitly declared as absolute paths")
    require(Path(manifest["backup_root"]).is_absolute() and root.is_dir() and
            not any(root.is_relative_to(x) for x in managed), "backup is inside a managed root")
    require(manifest.get("base") and manifest.get("scope"), "base and coverage scope required")
    require(manifest.get("credential_review") == "completed", "credential review not recorded")
    kinds = set()
    for item in manifest["files"]:
        relative = Path(item["copy"])
        target = (root / relative).resolve()
        require(not relative.is_absolute() and target.is_relative_to(root) and
                not any(target.is_relative_to(x) for x in managed), "unsafe backup file path")
        require(target.is_file() and digest(target) == item["sha256"], "backup missing/hash mismatch")
        source = Path(item["source"])
        require(source.is_absolute() and source.is_file() and digest(source) == item["sha256"],
                "source unavailable/changed; cannot verify pre-cleanup preservation")
        kinds.add(item["kind"])
    require({"change", "patch", "evidence"}.issubset(kinds), "changes/patch/evidence coverage missing")
    require(manifest["ignored_data"] == "not_needed" or
            (manifest["ignored_data"] == "included" and "ignored_data" in kinds),
            "necessary ignored data not preserved")
    return {"verified_files": len(manifest["files"]), "declared_scope": manifest["scope"],
            "backup_outside_managed_roots": True,
            "limitation": "Checks declared coverage only; no proof of omitted files, secret absence, "
                          "restore usability or cleanup authorization."}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify_bundle(load(args.manifest)), ensure_ascii=False))
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
