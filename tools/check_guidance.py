"""Check local links in current guidance, in source or a verified portable archive.

Historical contracts/snapshots remain evidence, not a recursively loaded instruction tree.
This structural check cannot establish natural-language semantic equivalence.
"""
import argparse
from pathlib import PurePosixPath
import re
import unicodedata
from urllib.parse import unquote, urlsplit

import package

SKILL = "skills/isolated-parallel-development-coordination/"
CURRENT = ("README.md", "docs/validation.md", "docs/lifecycle.md", SKILL + "SKILL.md") + tuple(
    SKILL + "references/" + name for name in (
        "current-rules.md", "platform-boundary.md", "history/index.md",
        "roles/main.md", "roles/developer.md", "roles/integrator.md",
        "core-runtime.md", "delivery-delegation.md", "state-protocol.md", "stage-feedback.md"))


def prose(data):
    return re.sub(r"^```.*?^```[^\n]*$", "", data.decode("utf-8-sig"), flags=re.M | re.S)


def anchors(data):
    found, counts = set(), {}
    for heading in re.findall(r"^#{1,6} +(.+?) *#* *$", prose(data), re.M):
        slug = "".join(c for c in heading.lower() if c in "-_ " or unicodedata.category(c)[0] in "LN").replace(" ", "-")
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        found.add(slug + ("-" + str(number) if number else ""))
    return found


def check(files):
    errors, checked = [], 0
    for name in CURRENT:
        if name not in files:
            errors.append(name + ": current guide missing")
            continue
        for target in re.findall(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)", prose(files[name])):
            link = urlsplit(target)
            if link.scheme or link.netloc:
                continue
            parts = list(PurePosixPath(name).parent.parts)
            if link.path.startswith("/"):
                errors.append(name + ": absolute local link " + target)
                continue
            for part in PurePosixPath(unquote(link.path)).parts:
                if part == "..":
                    if not parts:
                        break
                    parts.pop()
                elif part != ".":
                    parts.append(part)
            else:
                resolved = "/".join(parts) if link.path else name
                checked += 1
                if resolved not in files:
                    errors.append(name + ": missing " + target)
                elif link.fragment and unquote(link.fragment) not in anchors(files[resolved]):
                    errors.append(name + ": missing anchor " + target)
                continue
            errors.append(name + ": link escapes package " + target)
    return {"documents": len(CURRENT), "local_links": checked, "errors": errors}


if __name__ == "__main__":
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    files = package.inspect(args.archive)[1] if args.archive else package.payload(package.ROOT)
    result = check(files)
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(bool(result["errors"]))
