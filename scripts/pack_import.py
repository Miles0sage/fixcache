#!/usr/bin/env python3
"""
pack_import.py — Import a Lore Registry pack file into the local fixcache DB.

Usage:
    python scripts/pack_import.py fixcache-python-basics.toml
    python scripts/pack_import.py python-basics          # install from registry
    python scripts/pack_import.py --dry-run mypack.toml  # preview without writing
    python scripts/pack_import.py --policy merge mypack.toml   # default
    python scripts/pack_import.py --policy replace mypack.toml # replace conflicts
    python scripts/pack_import.py --policy skip mypack.toml    # skip conflicts
    python scripts/pack_import.py --pin 0.2.0 python-basics    # pin specific version

This is `fixcache install` under the hood. Conflict resolution:
  merge   — keep both; new recipe gets a suffixed ID (default)
  replace — incoming recipe overwrites existing if same fingerprint
  skip    — skip any recipe whose fingerprint already exists in local DB

Registry protocol (v1 — GitHub-hosted tap):
    Base URL: https://raw.githubusercontent.com/fixcache-registry/index/main/
    Index:    index.json   — { "name": { "latest": "0.2.0", "versions": {...} } }
    Pack:     packs/<name>/<version>.toml
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from urllib.error import URLError


# ── Registry constants ────────────────────────────────────────────────────────

REGISTRY_BASE = "https://raw.githubusercontent.com/fixcache-registry/index/main"
REGISTRY_INDEX_URL = f"{REGISTRY_BASE}/index.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db_path_default() -> str:
    return str(Path.home() / ".lore-memory" / "default.db")


def _bayesian_efficacy(success: int, failure: int) -> float:
    return (success + 1) / (success + failure + 2)


# ── TOML parser (stdlib tomllib in 3.11+, fallback pure-Python) ──────────────

def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file using stdlib tomllib (Python 3.11+) or fallback."""
    try:
        import tomllib  # type: ignore[import]
        with open(path, "rb") as f:
            return tomllib.load(f)
    except ImportError:
        pass
    try:
        import tomli  # type: ignore[import]
        with open(path, "rb") as f:
            return tomli.load(f)
    except ImportError:
        pass
    # Minimal fallback: parse the subset of TOML we generate
    return _minimal_toml_parse(path.read_text(encoding="utf-8"))


def _minimal_toml_parse(text: str) -> dict[str, Any]:
    """
    Minimal TOML parser for the fixcache pack format.
    Handles: string, int, float, bool, list-of-strings, [section], [[array]].
    Not a full TOML parser — only handles what pack_export.py generates.
    """
    import re

    root: dict[str, Any] = {}
    current_section: dict[str, Any] = root
    current_path: list[str] = []
    current_array_key: str | None = None
    current_array_item: dict[str, Any] | None = None

    def _set_at(path: list[str], value: Any) -> None:
        """Set root[path[0]][path[1]]... = value, creating dicts as needed."""
        node = root
        for key in path[:-1]:
            if key not in node:
                node[key] = {}
            node = node[key]
        node[path[-1]] = value

    def _get_at(path: list[str]) -> Any:
        node = root
        for key in path:
            node = node[key]
        return node

    def _parse_value(raw: str) -> Any:
        raw = raw.strip().rstrip(",")
        if raw.startswith('"') and raw.endswith('"'):
            return raw[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        if raw.startswith("'") and raw.endswith("'"):
            return raw[1:-1]
        if raw.lower() == "true":
            return True
        if raw.lower() == "false":
            return False
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            pass
        return raw

    # Accumulate multiline arrays
    in_multiline = False
    multiline_key = ""
    multiline_items: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Close multiline array
        if in_multiline:
            if line == "]":
                current_section[multiline_key] = multiline_items
                in_multiline = False
                multiline_items = []
                multiline_key = ""
            elif line.startswith('"') or line.startswith("'"):
                val = line.rstrip(",")
                if val.startswith('"') and val.endswith('"'):
                    multiline_items.append(val[1:-1].replace('\\"', '"').replace("\\n", "\n"))
                elif val.startswith("'") and val.endswith("'"):
                    multiline_items.append(val[1:-1])
            continue

        # [[array of tables]]
        m = re.match(r"^\[\[(.+)\]\]$", line)
        if m:
            table_key = m.group(1).strip()
            parts = table_key.split(".")
            # Create new item in array
            new_item: dict[str, Any] = {}
            # Navigate to parent
            parent = root
            for part in parts[:-1]:
                if part not in parent:
                    parent[part] = {}
                parent = parent[part]
            arr_key = parts[-1]
            if arr_key not in parent:
                parent[arr_key] = []
            parent[arr_key].append(new_item)
            current_section = new_item
            current_array_key = arr_key
            current_array_item = new_item
            current_path = parts
            continue

        # [section]
        m = re.match(r"^\[([^\[\]]+)\]$", line)
        if m:
            section_path = [p.strip() for p in m.group(1).split(".")]
            # If we're inside an [[array]] item and the section is a sub-key,
            # attach it to the current array item
            if current_array_item is not None and section_path[0] not in root:
                node = current_array_item
                for key in section_path:
                    if key not in node:
                        node[key] = {}
                    node = node[key]
                current_section = node
            else:
                # Navigate from root
                node2 = root
                for key in section_path:
                    if key not in node2:
                        node2[key] = {}
                    node2 = node2[key]
                current_section = node2
                current_array_item = None
            continue

        # key = value
        if "=" in line:
            key, _, raw_val = line.partition("=")
            key = key.strip()
            raw_val = raw_val.strip()

            # Multiline array start
            if raw_val == "[":
                in_multiline = True
                multiline_key = key
                multiline_items = []
                continue

            # Inline array
            if raw_val.startswith("[") and raw_val.endswith("]"):
                inner = raw_val[1:-1].strip()
                if not inner:
                    current_section[key] = []
                else:
                    items = []
                    for item in re.split(r",\s*", inner):
                        item = item.strip()
                        if item:
                            items.append(_parse_value(item))
                    current_section[key] = items
                continue

            current_section[key] = _parse_value(raw_val)

    return root


# ── Registry fetch ────────────────────────────────────────────────────────────

def _fetch_url(url: str, timeout: int = 10) -> bytes:
    try:
        with urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except URLError as e:
        raise RuntimeError(f"Network error fetching {url}: {e}") from e


def resolve_pack_from_registry(name: str, version: str | None = None) -> Path:
    """
    Download a pack from the Lore Registry (GitHub-hosted tap) to a temp file.
    Returns path to the downloaded .toml file.
    """
    import tempfile

    print(f"Fetching registry index from {REGISTRY_INDEX_URL}...", file=sys.stderr)
    try:
        index_data = json.loads(_fetch_url(REGISTRY_INDEX_URL))
    except RuntimeError as e:
        raise RuntimeError(
            f"Could not reach Lore Registry.\n"
            f"  {e}\n"
            f"Use a local file path instead: pack_import.py ./mypack.toml"
        ) from e

    if name not in index_data:
        available = ", ".join(sorted(index_data.keys()))
        raise RuntimeError(
            f"Pack '{name}' not found in registry.\nAvailable: {available}"
        )

    pack_meta = index_data[name]
    resolved_version = version or pack_meta.get("latest", "0.1.0")
    versions = pack_meta.get("versions", {})
    if resolved_version not in versions:
        raise RuntimeError(
            f"Version '{resolved_version}' not found for pack '{name}'.\n"
            f"Available: {', '.join(versions.keys())}"
        )

    pack_url = f"{REGISTRY_BASE}/packs/{name}/{resolved_version}.toml"
    print(f"Downloading {pack_url}...", file=sys.stderr)
    content = _fetch_url(pack_url)

    tmp = tempfile.NamedTemporaryFile(suffix=".toml", delete=False, mode="wb")
    tmp.write(content)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


# ── DB operations ─────────────────────────────────────────────────────────────

def _fingerprint_exists(conn: sqlite3.Connection, fp_hash: str) -> bool:
    if not fp_hash:
        return False
    row = conn.execute(
        "SELECT 1 FROM fingerprints WHERE hash = ? LIMIT 1", (fp_hash,)
    ).fetchone()
    return row is not None


def _recipe_exists_by_fingerprint(conn: sqlite3.Connection, fp_hash: str) -> bool:
    """Check if a darwin_pattern already exists for this fingerprint hash."""
    if not fp_hash:
        return False
    # metadata column is JSON with fingerprint_hash field
    row = conn.execute(
        "SELECT 1 FROM darwin_patterns WHERE pattern_type='error_recipe' AND metadata LIKE ? LIMIT 1",
        (f'%{fp_hash}%',),
    ).fetchone()
    return row is not None


def _upsert_fingerprint_from_pack(
    conn: sqlite3.Connection,
    fp_hash: str,
    recipe: dict[str, Any],
    stats: dict[str, Any],
) -> None:
    """
    Insert fingerprint row from pack data if not already present.
    If already present, merge the Darwin stats (take max of seen counts).
    """
    now = time.time()
    eco = recipe.get("fingerprint_ecosystem", "unknown")
    err_type = recipe.get("fingerprint_error_type", "Unknown")
    essence = recipe.get("error_signature", "")[:200]

    conn.execute(
        """
        INSERT OR IGNORE INTO fingerprints
            (hash, error_type, ecosystem, tool, essence, top_frame,
             total_seen, total_success, total_failure,
             first_seen, last_seen, best_pattern_id, metadata)
        VALUES (?, ?, ?, 'unknown', ?, NULL, 0, 0, 0, ?, ?, NULL, NULL)
        """,
        (fp_hash, err_type, eco, essence, now, now),
    )
    # Merge stats: take max so we never decrease counts (data from pack
    # represents aggregate of many users — always >= local counts for
    # a fresh install)
    pack_seen = stats.get("total_seen", 0)
    pack_success = stats.get("total_success", 0)
    pack_failure = stats.get("total_failure", 0)
    conn.execute(
        """
        UPDATE fingerprints
           SET total_seen    = MAX(total_seen,    ?),
               total_success = MAX(total_success, ?),
               total_failure = MAX(total_failure, ?),
               last_seen     = ?
         WHERE hash = ?
        """,
        (pack_seen, pack_success, pack_failure, now, fp_hash),
    )


def _insert_recipe(
    conn: sqlite3.Connection,
    recipe: dict[str, Any],
    pack_name: str,
    pack_version: str,
) -> dict[str, str]:
    """
    Insert one recipe into darwin_patterns + darwin_journal + memories.
    Returns dict with inserted IDs.
    """
    now = time.time()
    pattern_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())
    mem_id = str(uuid.uuid4())

    sig = recipe.get("error_signature", "")
    steps: list[str] = recipe.get("solution_steps", [])
    tags: list[str] = recipe.get("tags", [])
    fp_hash: str = recipe.get("fingerprint_hash", "")
    fp_eco: str = recipe.get("fingerprint_ecosystem", "unknown")
    fp_err: str = recipe.get("fingerprint_error_type", "Unknown")
    steps_json = json.dumps(steps)
    steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))

    meta = json.dumps({
        "tags": tags,
        "recipe_id": recipe_id,
        "fingerprint_hash": fp_hash,
        "fingerprint_error_type": fp_err,
        "fingerprint_ecosystem": fp_eco,
        "pack_name": pack_name,
        "pack_version": pack_version,
        "source_type": "pack",
        "trust_score": 0.85,  # slightly below user-rated (0.95) but above default (0.5)
    })

    description = f"Fix for: {sig[:120]}"

    # darwin_patterns — fast regex matcher
    conn.execute(
        """
        INSERT INTO darwin_patterns
            (id, pattern_type, description, rule, frequency, confidence,
             created_at, last_triggered, metadata)
        VALUES (?, 'error_recipe', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pattern_id,
            description,
            steps_json,
            recipe.get("darwin_stats", {}).get("total_seen", 1),
            min(0.9, recipe.get("darwin_stats", {}).get("efficacy", 0.5)),
            now,
            now,
            meta,
        ),
    )

    # darwin_journal — audit trail
    conn.execute(
        """
        INSERT INTO darwin_journal
            (id, query, result_ids, outcome, correction, timestamp, metadata)
        VALUES (?, ?, ?, 'success', ?, ?, ?)
        """,
        (recipe_id, sig, pattern_id, steps_json, now, meta),
    )

    # memories — FTS5 searchable fallback
    mem_content = f"ERROR FIX: {sig}\nSOLUTION:\n{steps_text}"
    if tags:
        mem_content += f"\nTAGS: {','.join(tags)}"

    mem_meta = json.dumps({
        "recipe_id": recipe_id,
        "pattern_id": pattern_id,
        "error_signature": sig,
        "tags": tags,
        "trust_score": 0.85,
        "source_type": "pack",
        "pack_name": pack_name,
        "pack_version": pack_version,
    })

    conn.execute(
        """
        INSERT INTO memories
            (id, content, memory_type, source_format, created_at,
             drawer_id, chunk_index, parent_id, metadata)
        VALUES (?, ?, 'experience', 'pack', ?, NULL, NULL, NULL, ?)
        """,
        (mem_id, mem_content, now, mem_meta),
    )

    return {"pattern_id": pattern_id, "recipe_id": recipe_id, "memory_id": mem_id}


# ── Core import logic ─────────────────────────────────────────────────────────

class ImportResult:
    def __init__(self) -> None:
        self.inserted: int = 0
        self.skipped: int = 0
        self.replaced: int = 0
        self.errors: list[str] = []

    def summary(self) -> str:
        parts = [f"inserted={self.inserted}"]
        if self.skipped:
            parts.append(f"skipped={self.skipped}")
        if self.replaced:
            parts.append(f"replaced={self.replaced}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return ", ".join(parts)


def import_pack(
    db_path: str,
    pack_path: Path,
    policy: str = "merge",
    dry_run: bool = False,
    pin_version: str | None = None,
) -> ImportResult:
    """
    Import a pack TOML file into the local fixcache DB.

    Args:
        db_path:    Path to the local SQLite DB.
        pack_path:  Path to the .toml pack file.
        policy:     Conflict resolution: 'merge' | 'replace' | 'skip'.
        dry_run:    If True, parse and validate but do not write.
        pin_version: If set, validate that pack.version == pin_version.
    """
    result = ImportResult()

    # Parse pack file
    pack_data = _load_toml(pack_path)

    pack_meta = pack_data.get("pack", {})
    pack_name = pack_meta.get("name", "unknown")
    pack_version = pack_meta.get("version", "0.1.0")
    fixcache_min = pack_meta.get("fixcache_min_version", "0.1.0")

    if pin_version and pack_version != pin_version:
        raise RuntimeError(
            f"Version mismatch: pack is {pack_version}, pin requires {pin_version}"
        )

    recipes_raw: list[dict[str, Any]] = pack_data.get("recipe", [])
    if not recipes_raw:
        print("Warning: pack contains no recipes.", file=sys.stderr)
        return result

    print(
        f"Pack: {pack_name}@{pack_version}  "
        f"({len(recipes_raw)} recipes, fixcache>={fixcache_min})",
        file=sys.stderr,
    )

    if dry_run:
        print(f"[dry-run] Would import {len(recipes_raw)} recipes (no DB writes)", file=sys.stderr)
        for r in recipes_raw:
            sig = r.get("error_signature", "")[:80]
            steps_count = len(r.get("solution_steps", []))
            fp = r.get("fingerprint", r.get("fingerprint_hash", ""))
            print(f"  recipe: {sig}  ({steps_count} steps, fp={fp[:8] if fp else 'none'})", file=sys.stderr)
        result.inserted = len(recipes_raw)
        return result

    # Open DB with schema already applied
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        for r in recipes_raw:
            # Normalize: support both 'fingerprint' (sample pack) and 'fingerprint_hash' (export)
            fp_hash: str = r.get("fingerprint", r.get("fingerprint_hash", ""))
            # Also pull darwin_stats from nested table if present
            darwin_stats: dict[str, Any] = r.get("darwin_stats", {})

            # Build a normalized recipe dict regardless of which TOML variant we got
            recipe = {
                "error_signature": r.get("error_signature", ""),
                "solution_steps": r.get("solution_steps", []),
                "tags": r.get("tags", []),
                "fingerprint_hash": fp_hash,
                "fingerprint_ecosystem": r.get("fingerprint_ecosystem", "unknown"),
                "fingerprint_error_type": r.get("fingerprint_error_type", "Unknown"),
                "darwin_stats": darwin_stats,
            }

            if not recipe["error_signature"] or not recipe["solution_steps"]:
                result.errors.append(f"Recipe missing required fields: {r.get('id', '?')}")
                continue

            conn.execute("BEGIN")
            try:
                # Handle conflict
                exists = fp_hash and _recipe_exists_by_fingerprint(conn, fp_hash)

                if exists:
                    if policy == "skip":
                        conn.execute("ROLLBACK")
                        result.skipped += 1
                        continue
                    elif policy == "replace":
                        # Delete existing pattern(s) for this fingerprint
                        conn.execute(
                            "DELETE FROM darwin_patterns WHERE metadata LIKE ? AND pattern_type='error_recipe'",
                            (f'%{fp_hash}%',),
                        )
                        result.replaced += 1
                    else:  # merge — both coexist
                        pass

                # Upsert fingerprint with pack stats
                if fp_hash:
                    _upsert_fingerprint_from_pack(conn, fp_hash, recipe, darwin_stats)

                # Insert the recipe rows
                _insert_recipe(conn, recipe, pack_name, pack_version)
                conn.execute("COMMIT")
                result.inserted += 1

            except Exception as e:
                conn.execute("ROLLBACK")
                result.errors.append(f"Recipe '{r.get('id', '?')}': {e}")

    finally:
        conn.close()

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pack_import",
        description="Import a Lore Registry pack into the local fixcache DB",
    )
    parser.add_argument(
        "source",
        help="Pack name (fetched from registry) or local .toml file path",
    )
    parser.add_argument(
        "--db", default=_db_path_default(), metavar="PATH",
        help="Path to SQLite DB (default: ~/.lore-memory/default.db)",
    )
    parser.add_argument(
        "--policy", default="merge", choices=["merge", "replace", "skip"],
        help="Conflict resolution (default: merge)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and validate without writing to DB",
    )
    parser.add_argument(
        "--pin", metavar="VERSION",
        help="Require exact pack version (e.g. 0.2.0)",
    )

    args = parser.parse_args(argv)

    source = args.source
    pack_path: Path

    # Detect local file vs registry name
    if source.endswith(".toml") or Path(source).exists():
        pack_path = Path(source).expanduser().resolve()
        if not pack_path.exists():
            print(f"Error: file not found: {pack_path}", file=sys.stderr)
            return 1
    else:
        # Fetch from registry
        try:
            pack_path = resolve_pack_from_registry(source, version=args.pin)
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    db_path = str(Path(args.db).expanduser().resolve())

    # Ensure DB exists (auto-create with schema if missing)
    if not Path(db_path).exists():
        print(f"DB not found at {db_path} — creating...", file=sys.stderr)
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent))
            from lore_memory.core.store import MemoryStore
            store = MemoryStore(db_path)
            store.close()
            print(f"Created: {db_path}", file=sys.stderr)
        except ImportError:
            print(
                "Error: fixcache is not installed. Run: pip install fixcache",
                file=sys.stderr,
            )
            return 1

    print(f"Importing {pack_path.name} → {db_path}  [policy={args.policy}]", file=sys.stderr)

    try:
        result = import_pack(
            db_path=db_path,
            pack_path=pack_path,
            policy=args.policy,
            dry_run=args.dry_run,
            pin_version=args.pin,
        )
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    status = "dry-run preview" if args.dry_run else "done"
    print(f"Import {status}: {result.summary()}", file=sys.stderr)

    if result.errors:
        print(f"\nErrors ({len(result.errors)}):", file=sys.stderr)
        for err in result.errors[:10]:
            print(f"  {err}", file=sys.stderr)

    return 0 if not result.errors else 2


if __name__ == "__main__":
    sys.exit(main())
