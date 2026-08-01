#!/usr/bin/env python3
"""
generate_catalog.py

Scans /projects/ (one level deep) and produces projects.json at the repo root.

For each immediate subfolder of /projects/:
  - Metadata (title, description, keywords, icon, favicon) comes from project.json
    if present, otherwise from scanning index.html, otherwise falls back to the
    folder name as title with no other metadata.
  - url is derived independently: present only if index.html exists in the folder.
  - source_url is always derived from the configured owner/repo.
  - added is stamped with today's UTC date the first time a folder is seen, and
    carried forward unchanged on subsequent runs by reading the previous
    projects.json.

Exit codes:
  0 - success
  1 - hard failure (missing /projects/ dir, or a project.json syntax error)
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration — update these to match the target repo.
# ---------------------------------------------------------------------------
GITHUB_OWNER = "samar1h"
GITHUB_REPO = "aiasp2"
GITHUB_BRANCH = "main"

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = REPO_ROOT / "projects"
OUTPUT_PATH = REPO_ROOT / "projects.json"

# ---------------------------------------------------------------------------
# Regexes for scanning index.html (only used when project.json is absent, or
# doesn't provide a given field).
# ---------------------------------------------------------------------------
RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
RE_DESCRIPTION = re.compile(
    r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', re.IGNORECASE | re.DOTALL
)
RE_KEYWORDS = re.compile(
    r'<meta\s+name=["\']keywords["\']\s+content=["\'](.*?)["\']', re.IGNORECASE | re.DOTALL
)
RE_ICON = re.compile(
    r'<meta\s+name=["\']app-icon["\']\s+content=["\'](.*?)["\']', re.IGNORECASE | re.DOTALL
)
RE_FAVICON = re.compile(
    r'<link\s+rel=["\']icon["\']\s+href=["\'](.*?)["\']', re.IGNORECASE | re.DOTALL
)

METADATA_FIELDS = ("title", "description", "keywords", "icon", "favicon")


def log_warning(message: str) -> None:
    print(f"::warning::{message}", file=sys.stderr)


def log_error(message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)


def extract_fields_from_html(html: str, folder_path: Path) -> dict:
    """Extract metadata fields from an HTML string via regex. Missing fields
    are simply absent from the returned dict."""
    fields = {}

    m = RE_TITLE.search(html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        if title:
            fields["title"] = title

    m = RE_DESCRIPTION.search(html)
    if m:
        description = m.group(1).strip()
        if description:
            fields["description"] = description

    m = RE_KEYWORDS.search(html)
    if m:
        keywords = [k.strip() for k in m.group(1).split(",") if k.strip()]
        if keywords:
            fields["keywords"] = keywords

    m = RE_ICON.search(html)
    if m:
        icon = m.group(1).strip()
        if icon:
            fields["icon"] = icon

    m = RE_FAVICON.search(html)
    if m:
        favicon_href = m.group(1).strip()
        if favicon_href:
            # Resolve relative to the project's folder path.
            fields["favicon"] = str(Path(folder_path.name) / favicon_href)

    return fields


def scan_index_html(index_html_path: Path, folder_path: Path) -> dict:
    """Scan index.html for metadata fields. Tries a fast path on the first 10
    lines, then falls back to a full read if that doesn't yield every expected
    tag. Returns whatever fields were found (possibly none)."""
    try:
        with index_html_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = []
            for _ in range(10):
                line = f.readline()
                if not line:
                    break  # fewer than 10 lines in the file; that's fine
                lines.append(line)
            first_lines = "".join(lines)
    except OSError as e:
        log_warning(f"Could not open {index_html_path}: {e}")
        return {}

    fast_fields = extract_fields_from_html(first_lines, folder_path)
    if all(field in fast_fields for field in METADATA_FIELDS):
        return fast_fields

    # Fallback: read the entire file and scan in full.
    try:
        full_html = index_html_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log_warning(f"Could not read {index_html_path}: {e}")
        return fast_fields

    return extract_fields_from_html(full_html, folder_path)


def load_project_json(project_json_path: Path) -> dict:
    """Parse project.json. Raises ValueError on a JSON syntax error — the
    caller is responsible for treating that as a hard failure."""
    try:
        with project_json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(str(e)) from e

    if not isinstance(data, dict):
        raise ValueError("project.json must contain a JSON object")

    return {field: data[field] for field in METADATA_FIELDS if field in data}


def load_previous_catalog(output_path: Path) -> dict:
    """Read the previous projects.json (if any) and index it by folder name so
    `added` dates can be carried forward. Returns {} if no previous file."""
    if not output_path.exists():
        return {}

    try:
        with output_path.open("r", encoding="utf-8") as f:
            previous = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_warning(f"Could not read previous {output_path.name}, treating as first run: {e}")
        return {}

    by_folder = {}
    for entry in previous:
        folder = entry.get("_folder") or entry.get("folder")
        # We don't actually persist a "folder" field in the output, so derive
        # it from source_url instead, which always encodes the folder name.
        if not folder and "source_url" in entry:
            folder = entry["source_url"].rstrip("/").rsplit("/", 1)[-1]
        if folder and "added" in entry:
            by_folder[folder] = entry["added"]

    return by_folder


def build_project_entry(folder_path: Path, previous_added: dict, today: str) -> dict:
    folder_name = folder_path.name
    project_json_path = folder_path / "project.json"
    index_html_path = folder_path / "index.html"

    metadata = {}

    if project_json_path.exists():
        # Hard failure on syntax error — let this propagate up.
        metadata = load_project_json(project_json_path)
    elif index_html_path.exists():
        try:
            metadata = scan_index_html(index_html_path, folder_path)
        except Exception as e:
            log_warning(
                f"Failed to scan index.html for project '{folder_name}': {e}. "
                "Falling back to folder name as title."
            )
            metadata = {}

    entry = {}

    entry["title"] = metadata.get("title") or folder_name
    for field in ("description", "keywords", "icon", "favicon"):
        if metadata.get(field):
            entry[field] = metadata[field]

    entry["source_url"] = (
        f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/tree/{GITHUB_BRANCH}/projects/{folder_name}"
    )

    if index_html_path.exists():
        entry["url"] = f"projects/{folder_name}/"

    entry["added"] = previous_added.get(folder_name, today)

    return entry


def main() -> int:
    if not PROJECTS_DIR.is_dir():
        log_error(f"Projects directory not found: {PROJECTS_DIR}")
        return 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    previous_added = load_previous_catalog(OUTPUT_PATH)

    folders = sorted(
        (p for p in PROJECTS_DIR.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )

    entries = []
    for folder_path in folders:
        try:
            entry = build_project_entry(folder_path, previous_added, today)
        except ValueError as e:
            log_error(f"Invalid project.json in '{folder_path.name}': {e}")
            return 1
        entries.append(entry)

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Wrote {len(entries)} project(s) to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
