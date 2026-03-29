#!/usr/bin/env python3
"""
Walks bin/, lib/, lambda/ and their subfolders, collects all .ts files,
and writes a single text file containing:
  1. The full directory tree structure
  2. The contents of every .ts file found
"""

import os
import sys
from pathlib import Path
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
ROOT_DIRS   = ["bin", "lib", "lambda", "frontend"]  # top-level folders to scan
OUTPUT_FILE = "ts_export.txt"                       # output file name
EXTENSIONS  = {".ts", ".tsx", ".html", ".htm"}      # file extensions to include
EXCLUDE_DIRS = {"node_modules", ".git", "dist", "cdk.out", ".cache"}
# ────────────────────────────────────────────────────────────────────────────


def build_tree(base: Path, root_dirs: list[str]) -> list[str]:
    """Return a list of lines representing the directory tree."""
    lines = [f"{base.resolve().name}/"]

    for root_name in root_dirs:
        root_path = base / root_name
        if not root_path.exists():
            continue
        lines.append(f"├── {root_name}/")
        lines.extend(_walk_tree(root_path, prefix="│   "))

    return lines


def _walk_tree(directory: Path, prefix: str = "") -> list[str]:
    lines = []
    try:
        entries = sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name))
    except PermissionError:
        return lines

    entries = [e for e in entries if e.name not in EXCLUDE_DIRS]

    for i, entry in enumerate(entries):
        connector = "└── " if i == len(entries) - 1 else "├── "
        lines.append(f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}")
        if entry.is_dir():
            extension = "    " if i == len(entries) - 1 else "│   "
            lines.extend(_walk_tree(entry, prefix + extension))

    return lines


def collect_ts_files(base: Path, root_dirs: list[str]) -> list[Path]:
    """Return all .ts files under the specified root directories."""
    ts_files = []
    for root_name in root_dirs:
        root_path = base / root_name
        if not root_path.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            # Prune excluded directories in-place
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fname in sorted(filenames):
                if Path(fname).suffix in EXTENSIONS:
                    ts_files.append(Path(dirpath) / fname)
    return ts_files


def export(base_dir: str = ".") -> None:
    base = Path(base_dir).resolve()
    out_path = base / OUTPUT_FILE

    tree_lines = build_tree(base, ROOT_DIRS)
    ts_files   = collect_ts_files(base, ROOT_DIRS)

    found_roots = [r for r in ROOT_DIRS if (base / r).exists()]
    missing     = [r for r in ROOT_DIRS if r not in found_roots]

    with open(out_path, "w", encoding="utf-8") as out:
        # ── Header ────────────────────────────────────────────────────────
        out.write("=" * 70 + "\n")
        out.write(f"  TypeScript Export\n")
        out.write(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        out.write(f"  Root      : {base}\n")
        out.write(f"  Scanned   : {', '.join(found_roots) or 'none'}\n")
        if missing:
            out.write(f"  Missing   : {', '.join(missing)}\n")
        out.write(f"  Files     : {len(ts_files)} (.ts/.tsx/.html)\n")
        out.write("=" * 70 + "\n\n")

        # ── Directory tree ─────────────────────────────────────────────────
        out.write("DIRECTORY STRUCTURE\n")
        out.write("-" * 70 + "\n")
        out.write("\n".join(tree_lines) + "\n\n")

        # ── File contents ──────────────────────────────────────────────────
        out.write("FILE CONTENTS\n")
        out.write("-" * 70 + "\n\n")

        for ts_file in ts_files:
            rel = ts_file.relative_to(base)
            suffix = ts_file.suffix.lstrip(".")
            out.write(f"### {rel}  [{suffix}]\n\n")
            try:
                content = ts_file.read_text(encoding="utf-8")
                out.write(content)
            except Exception as e:
                out.write(f"[ERROR reading file: {e}]")
                content = ""
            if not content.endswith("\n"):
                out.write("\n")
            out.write("\n" + "-" * 70 + "\n\n")

    print(f"✓ Exported {len(ts_files)} file(s) (.ts/.tsx/.html) → {out_path}")
    if missing:
        print(f"  (skipped missing dirs: {', '.join(missing)})")


if __name__ == "__main__":
    base_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    export(base_dir)