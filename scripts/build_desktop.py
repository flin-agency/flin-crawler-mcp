"""Create a deterministic UV Desktop extension from the same source as the wheel."""

import json
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(tag=None):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    manifest = json.loads((ROOT / "manifest.json").read_text())
    if manifest["version"] != version or (tag and tag != f"v{version}"):
        raise SystemExit(
            "The release tag, project version and manifest version must match"
        )
    files = [
        ROOT / name
        for name in ("manifest.json", "pyproject.toml", "README.md", "src/desktop.py")
    ]
    files.extend(sorted((ROOT / "src/flin_crawler_mcp").glob("*.py")))
    (ROOT / "dist").mkdir(exist_ok=True)
    path = ROOT / "dist" / f"flin-crawler-mcp-{version}.mcpb"
    with zipfile.ZipFile(path, "w") as bundle:
        for file in files:
            item = zipfile.ZipInfo(
                file.relative_to(ROOT).as_posix(), date_time=(2026, 1, 1, 0, 0, 0)
            )
            item.external_attr = 0o100644 << 16
            bundle.writestr(item, file.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    return path


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else None))
