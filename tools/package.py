"""Build and inspect a deterministic, allowlisted portable plugin archive."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = "codex-parallel-coordination"
MANIFEST = "PACKAGE-MANIFEST.json"
FILES = (".gitignore", "README.md", "requirements-validation.txt", "plugin.json", "capabilities.json", ".codex-plugin/plugin.json",
         ".agents/plugins/marketplace.json")
TREES = {"src": {".py"}, "skills": {".py", ".mjs", ".md", ".yaml", ".json"},
         "tools": {".py"}, "tests": {".py"}, "docs": {".md"}, "provenance": {".json"}}
LOCAL_DIRS = {".git", "__pycache__", ".venv", "venv", "cache", ".cache", ".pytest_cache",
              "evidence", "runtime", "profiles", "local", "build", "dist"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or path.is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("unsafe archive path: " + name)
    return path


def source_path(path, root):
    """Check the whole lexical source chain before reading or descending."""
    current, boundary = path.absolute(), root.absolute()
    if not current.is_relative_to(boundary):
        raise ValueError("source path escapes root")
    while True:
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise ValueError("source links/reparse points are not packaged: " + str(current))
        if current == boundary:
            break
        current = current.parent


def payload(root):
    result = {}
    candidates = [root / name for name in FILES]
    for directory, extensions in TREES.items():
        tree = root / directory
        try:
            source_path(tree, root)
        except FileNotFoundError:
            continue
        for parent, directories, filenames in os.walk(tree, topdown=True, followlinks=False):
            base = Path(parent)
            kept = []
            for name in directories:
                if name.casefold() not in LOCAL_DIRS:
                    source_path(base / name, root)
                    kept.append(name)
            directories[:] = kept
            candidates.extend(base / name for name in filenames if Path(name).suffix in extensions)
    for path in candidates:
        source_path(path, root)
        result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def validate(files):
    for name in files:
        safe_name(name)
    if len({name.casefold() for name in files}) != len(files):
        raise ValueError("case-colliding paths")
    plugin = json.loads(files["plugin.json"])
    legacy = json.loads(files[".codex-plugin/plugin.json"])
    capabilities = json.loads(files["capabilities.json"])
    if plugin["name"] != NAME or legacy["name"] != NAME:
        raise ValueError("unexpected plugin identity")
    if len({plugin["version"], legacy["version"], capabilities["version"]}) != 1:
        raise ValueError("inconsistent versions")
    if (len(plugin["version"]) > 64 or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", plugin["version"])):
        raise ValueError("version must be a safe semantic version")
    if capabilities["idle_automatic_wakeup"] is not False:
        raise ValueError("unsupported idle wakeup claim")
    if not any(name.startswith("skills/") and name.endswith("/SKILL.md") for name in files):
        raise ValueError("no packaged skill")
    catalog = json.loads(files[".agents/plugins/marketplace.json"])
    if catalog["plugins"][0]["source"]["path"] != "./":
        raise ValueError("marketplace does not reference packaged root")
    if any(name.startswith("hooks/") for name in files):
        raise ValueError("this release does not activate hooks")
    return plugin


def build(root, output):
    files = payload(root)
    plugin = validate(files)
    manifest = {"schema": "coordination-package/1", "name": NAME, "version": plugin["version"],
                "files": {name: digest(data) for name, data in sorted(files.items())}}
    files[MANIFEST] = encoded(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, data)
    return {"archive": str(output), "sha256": digest(output.read_bytes()),
            "files": len(manifest["files"]), "version": plugin["version"]}


def inspect(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [item.filename for item in entries]
        if len(names) != len(set(names)) or len(names) > 1000:
            raise ValueError("duplicate or excessive archive entries")
        if sum(item.file_size for item in entries) > 30_000_000:
            raise ValueError("archive exceeds size limit")
        for item in entries:
            safe_name(item.filename)
            if stat.S_ISLNK(item.external_attr >> 16) or item.is_dir():
                raise ValueError("archive must contain regular files only")
        files = {item.filename: archive.read(item) for item in entries}
    manifest = json.loads(files.pop(MANIFEST))
    if manifest["schema"] != "coordination-package/1" or manifest["name"] != NAME:
        raise ValueError("unsupported package manifest")
    actual = {name: digest(data) for name, data in sorted(files.items())}
    if actual != manifest["files"]:
        raise ValueError("package contents differ from manifest")
    plugin = validate(files)
    if manifest["version"] != plugin["version"]:
        raise ValueError("manifest version differs")
    return manifest, files


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("build")
    command.add_argument("--output", type=Path, required=True)
    command = sub.add_parser("inspect")
    command.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        result = build(ROOT, args.output.resolve())
    else:
        manifest, _ = inspect(args.archive)
        result = {"name": manifest["name"], "version": manifest["version"], "files": len(manifest["files"])}
    print(json.dumps(result, ensure_ascii=False))
