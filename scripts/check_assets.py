"""Guard against malicious assets and VS Code auto-run tasks.

This repository was once used to ship an obfuscated JavaScript payload disguised
as a web font (``public/fonts/fa-solid-500.woff2``) together with a
``.vscode/tasks.json`` that executed it whenever the folder was opened in
VS Code. This check fails CI if either pattern ever comes back:

* a file with a font extension (``.woff``/``.woff2``/``.ttf``/``.otf``/``.eot``/
  ``.svg``) is not actually a font of that type, or contains script markers; and
* a ``.vscode`` JSON file runs a task on ``folderOpen`` or enables
  ``task.allowAutomaticTasks``.

It has no third-party dependencies so it can run before anything is installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Directories that never contain first-party source worth scanning.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}

FONT_EXTENSIONS = {".woff", ".woff2", ".ttf", ".otf", ".eot", ".svg"}

#: Bytes that should never appear inside a real font or its SVG licence wrapper.
SCRIPT_MARKERS = (b"eval(", b"spawn(", b"child_process", b"require(")


def _looks_like_font(path: Path) -> bool:
    """Return whether ``path`` is a real font of its declared extension."""
    ext = path.suffix.lower()
    head = path.read_bytes()[:8]

    if ext == ".woff":
        return head[:4] == b"wOFF"
    if ext == ".woff2":
        return head[:4] == b"wOF2"
    if ext in {".ttf", ".otf"}:
        return head[:4] in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"}
    if ext == ".eot":
        # An EOT file starts with its own total size as a little-endian uint32.
        return int.from_bytes(head[:4], "little") == path.stat().st_size
    if ext == ".svg":
        stripped = path.read_bytes()[:256].lstrip()
        return stripped.startswith(b"<?xml") or stripped.startswith(b"<svg")
    return True


def find_violations(root: Path = REPO_ROOT) -> list[str]:
    """Return human-readable descriptions of every suspicious asset found."""
    problems: list[str] = []

    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue

        if path.suffix.lower() in FONT_EXTENSIONS:
            if not _looks_like_font(path):
                problems.append(f"{path.relative_to(root)} is not a valid {path.suffix} font")
                continue
            blob = path.read_bytes()[:4096]
            marker = next((m for m in SCRIPT_MARKERS if m in blob), None)
            if marker is not None:
                problems.append(f"{path.relative_to(root)} contains script marker {marker!r}")

        if path.parent == root / ".vscode" and path.suffix.lower() == ".json":
            text = path.read_text(encoding="utf-8", errors="replace")
            if '"folderOpen"' in text or "folderOpen" in text:
                problems.append(f"{path.relative_to(root)} runs a task on folderOpen")
            if "task.allowAutomaticTasks" in text:
                problems.append(f"{path.relative_to(root)} enables task.allowAutomaticTasks")

    return problems


def main() -> int:
    problems = find_violations()
    if problems:
        print("Blocked: suspicious assets or auto-run tasks found:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("No suspicious assets or auto-run tasks found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
