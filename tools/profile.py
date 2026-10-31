"""Stage plugin files in an explicit isolated profile; never enable a host plugin."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile

from package import NAME, digest, encoded, inspect

STATE = "coordination-installation.json"


def ordinary(path):
    value = str(path)
    if os.name == "nt" and value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif os.name == "nt" and value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def filesystem(path):
    value = str(path)
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)
    return path


def profile_path(path):
    if not path.is_absolute():
        raise ValueError("explicit absolute profile required")
    target = ordinary(path).resolve()
    protected = [Path.home(), Path.home() / ".codex", Path.home() / ".agents"]
    if os.environ.get("CODEX_HOME"):
        protected.append(ordinary(Path(os.environ["CODEX_HOME"])).resolve())
    if any(target == item or item.is_relative_to(target) for item in protected):
        raise ValueError("refuse real home or its ancestor")
    if any(target.is_relative_to(item) for item in protected[1:]):
        raise ValueError("refuse live Codex/agent configuration subtree")
    return filesystem(target)


def write(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(root):
    lock = root / ".coordination-install.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    try:
        yield
    finally:
        lock.unlink()


def load(root):
    state = json.loads((root / STATE).read_text(encoding="utf-8"))
    if state.get("owner") != NAME or state.get("schema") != "coordination-profile/1":
        raise ValueError("unknown existing profile metadata; preserved")
    return state


def verify_revision(root, revision):
    directory = (root / revision["directory"]).resolve()
    if not directory.is_relative_to(root) or not directory.is_relative_to(root / "plugins" / NAME):
        raise ValueError("revision escapes owned plugin directory")
    for name, expected in revision["files"].items():
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise ValueError("installed link changed; preserve and reconcile")
        if digest(path.read_bytes()) != expected:
            raise ValueError("installed content edited; preserve and reconcile")


def action(command, profile, archive=None):
    root = profile_path(profile)
    if command == "init":
        root.mkdir(parents=True, exist_ok=True)
        with locked(root):
            if (root / STATE).exists():
                state = load(root)
            else:
                state = {"owner": NAME, "schema": "coordination-profile/1", "active": None,
                         "revisions": [], "host_plugin_enabled": False}
                write(root / STATE, state)
    elif command == "status":
        state = load(root)
    else:
        with locked(root):
            state = load(root)
            active = state["active"]
            if active is not None:
                verify_revision(root, state["revisions"][active])
            if command == "install":
                manifest, files = inspect(archive)
                identity = digest(encoded(manifest))
                directory = Path("plugins") / NAME / (manifest["version"] + "-" + identity[:16])
                target = root / directory
                owned = (root / "plugins" / NAME).resolve()
                if (owned != root / "plugins" / NAME or target.resolve() != target
                        or not owned.is_relative_to(root) or not target.resolve().is_relative_to(owned)):
                    raise ValueError("plugin destination link escapes profile")
                known = next((i for i, rev in enumerate(state["revisions"])
                              if rev["digest"] == identity), None)
                if known is None:
                    target.mkdir(parents=True, exist_ok=False)
                    for name, content in files.items():
                        path = target / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                    state["revisions"].append({"digest": identity, "version": manifest["version"],
                                               "directory": directory.as_posix(), "files": manifest["files"]})
                    known = len(state["revisions"]) - 1
                verify_revision(root, state["revisions"][known])
                state["active"] = known
            elif command == "rollback":
                if active is None or active == 0:
                    raise ValueError("no earlier installed revision")
                verify_revision(root, state["revisions"][active - 1])
                state["active"] = active - 1
            elif command == "uninstall":
                state["active"] = None
            else:
                raise ValueError("unknown profile action")
            write(root / STATE, state)
    return {"profile": str(ordinary(root)), "active": state["active"], "revisions": len(state["revisions"]),
            "host_plugin_enabled": False, "global_configuration_changed": False,
            "data_and_user_content_preserved": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "status", "install", "rollback", "uninstall"))
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.command == "install" and args.archive is None:
        parser.error("install requires --archive")
    print(json.dumps(action(args.command, args.profile, args.archive), ensure_ascii=False))
