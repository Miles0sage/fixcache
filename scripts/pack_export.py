#!/usr/bin/env python3
"""
pack_export.py — Export fixcache recipes from a local DB to a Lore Registry pack file.

Usage:
    python scripts/pack_export.py                     # → stdout TOML
    python scripts/pack_export.py --out mypack.toml
    python scripts/pack_export.py --name my-org/django-errors --version 0.1.0
    python scripts/pack_export.py --ecosystem python --min-seen 2
    python scripts/pack_export.py --db ~/.lore-memory/custom.db --out pack.toml

The exported pack is valid TOML that can be published to the Lore Registry
(a GitHub-hosted JSON index + individual TOML files) and imported by anyone
with `fixcache install`.

Design notes:
- Recipes come from darwin_patterns (pattern_type = 'error_recipe')
- Fingerprint stats come from the fingerprints table (joined via metadata.fingerprint_hash)
- All PII/paths are already stripped by the fingerprinter — export is safe to publish
- Pack-level aggregate stats are computed from individual recipe darwin_stats
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db_path_default() -> str:
    return str(Path.home() / ".lore-memory" / "default.db")


def _bayesian_efficacy(success: int, failure: int) -> float:
    """Bayesian Beta MAP estimate: avoids 0/1 extremes on low counts."""
    return (success + 1) / (success + failure + 2)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _toml_string(s: str) -> str:
    """Escape a string for TOML basic string (double-quoted)."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _toml_multiline_array(items: list[str], indent: str = "    ") -> str:
    """Render a list of strings as a TOML multiline array."""
    if not items:
        return "[]"
    lines = ["["]
    for item in items:
        lines.append(f'{indent}"{_toml_string(item)}",')
    lines.append("]")
    return "\n".join(lines)


# ── Core export logic ─────────────────────────────────────────────────────────

def load_recipes(
    conn: sqlite3.Connection,
    ecosystem_filter: str | None = None,
    min_seen: int = 1,
) -> list[dict[str, Any]]:
    """
    Load error recipes from darwin_patterns + fingerprints tables.

    Returns list of dicts with fields:
        id, error_signature, solution_steps, tags,
        fingerprint_hash, fingerprint_ecosystem, fingerprint_error_type,
        total_seen, total_success, total_failure, efficacy
    """
    rows = conn.execute(
        """
        SELECT dp.id, dp.description, dp.rule, dp.metadata,
               dp.frequency, dp.confidence
          FROM darwin_patterns dp
         WHERE dp.pattern_type = 'error_recipe'
         ORDER BY dp.frequency DESC, dp.confidence DESC
        """
    ).fetchall()

    recipes = []
    for row in rows:
        pat_id, description, rule_json, meta_json, frequency, confidence = row

        # Extract error_signature from description ("Fix for: <sig>")
        sig = description.replace("Fix for: ", "", 1) if description else ""

        # Parse solution_steps from rule (JSON array of strings)
        try:
            solution_steps: list[str] = json.loads(rule_json) if rule_json else []
        except (json.JSONDecodeError, TypeError):
            solution_steps = [rule_json] if rule_json else []

        if not solution_steps:
            continue

        # Parse metadata for fingerprint hash + tags
        try:
            meta: dict[str, Any] = json.loads(meta_json) if meta_json else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}

        fp_hash: str = meta.get("fingerprint_hash", "")
        fp_ecosystem: str = meta.get("fingerprint_ecosystem", "unknown")
        fp_error_type: str = meta.get("fingerprint_error_type", "Unknown")
        tags: list[str] = meta.get("tags", [])

        # Apply ecosystem filter
        if ecosystem_filter and fp_ecosystem != ecosystem_filter:
            continue

        # Join fingerprint table for Darwin stats
        total_seen = total_success = total_failure = 0
        if fp_hash:
            fp_row = conn.execute(
                "SELECT total_seen, total_success, total_failure FROM fingerprints WHERE hash = ?",
                (fp_hash,),
            ).fetchone()
            if fp_row:
                total_seen, total_success, total_failure = fp_row

        # Apply min-seen floor (noise filter)
        if total_seen < min_seen:
            continue

        efficacy = _bayesian_efficacy(total_success, total_failure)

        recipes.append(
            {
                "id": pat_id,
                "error_signature": sig,
                "solution_steps": solution_steps,
                "tags": tags,
                "fingerprint_hash": fp_hash,
                "fingerprint_ecosystem": fp_ecosystem,
                "fingerprint_error_type": fp_error_type,
                "total_seen": total_seen,
                "total_success": total_success,
                "total_failure": total_failure,
                "efficacy": efficacy,
            }
        )

    return recipes


def build_pack_toml(
    recipes: list[dict[str, Any]],
    name: str = "my-pack",
    version: str = "0.1.0",
    description: str = "fixcache memory pack",
    author: str = "",
    license_: str = "MIT",
) -> str:
    """Render a list of recipe dicts to a valid fixcache pack TOML string."""

    # Compute pack-level aggregate stats
    total_seen = sum(r["total_seen"] for r in recipes)
    total_success = sum(r["total_success"] for r in recipes)
    total_failure = sum(r["total_failure"] for r in recipes)
    pack_efficacy = _bayesian_efficacy(total_success, total_failure)

    # Collect all ecosystem tags
    all_ecosystems = sorted({r["fingerprint_ecosystem"] for r in recipes if r["fingerprint_ecosystem"] != "unknown"})
    all_tags = sorted({tag for r in recipes for tag in r["tags"]})

    lines: list[str] = []

    # ── File header ──────────────────────────────────────────────────────────
    lines.append("# fixcache memory pack")
    lines.append("# Format: fixcache-pack/1.0")
    lines.append(f"# Generated: {_now_iso()}")
    lines.append(f"# Recipes: {len(recipes)}")
    lines.append("")

    # ── [pack] block ─────────────────────────────────────────────────────────
    lines.append("[pack]")
    lines.append(f'name        = "{_toml_string(name)}"')
    lines.append(f'version     = "{_toml_string(version)}"')
    lines.append(f'description = "{_toml_string(description)}"')
    if author:
        lines.append(f'author      = "{_toml_string(author)}"')
    lines.append(f'license     = "{_toml_string(license_)}"')
    lines.append(f'fixcache_min_version = "0.4.0"')
    lines.append(f'corpus_source   = "local"')
    lines.append(f'corpus_snapshot = "{datetime.now(timezone.utc).strftime("%Y-%m-%d")}"')
    lines.append("")

    eco_toml = json.dumps(all_ecosystems)
    tag_toml = json.dumps(all_tags[:20])  # cap at 20 to avoid bloat
    lines.append(f"ecosystem_tags = {eco_toml}")
    lines.append(f"keywords       = {tag_toml}")
    lines.append("")

    # ── [pack.stats] block ───────────────────────────────────────────────────
    lines.append("[pack.stats]")
    lines.append(f"total_installs = 0")
    lines.append(f"total_seen     = {total_seen}")
    lines.append(f"total_success  = {total_success}")
    lines.append(f"total_failure  = {total_failure}")
    lines.append(f"efficacy       = {pack_efficacy:.3f}")
    lines.append(f'last_updated   = "{_now_iso()}"')
    lines.append("")

    # ── [[recipe]] blocks ────────────────────────────────────────────────────
    for r in recipes:
        lines.append("")
        lines.append("[[recipe]]")

        # Use fingerprint hash as stable ID slug (truncated for readability)
        slug = f"{r['fingerprint_ecosystem']}-{r['fingerprint_error_type'].lower()}-{r['fingerprint_hash'][:8]}"
        lines.append(f'id              = "{_toml_string(slug)}"')
        lines.append(f'error_signature = "{_toml_string(r["error_signature"][:200])}"')

        tags_str = json.dumps(r["tags"])
        lines.append(f"tags            = {tags_str}")
        lines.append(f'outcome         = "success"')

        if r["fingerprint_hash"]:
            lines.append(f'fingerprint     = "{r["fingerprint_hash"]}"')

        # solution_steps as TOML multiline array
        steps_repr = _toml_multiline_array(r["solution_steps"])
        lines.append(f"solution_steps = {steps_repr}")
        lines.append("")

        lines.append("[recipe.darwin_stats]")
        lines.append(f"total_seen    = {r['total_seen']}")
        lines.append(f"total_success = {r['total_success']}")
        lines.append(f"total_failure = {r['total_failure']}")
        lines.append(f"efficacy      = {r['efficacy']:.3f}")

    lines.append("")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pack_export",
        description="Export fixcache recipes to a Lore Registry pack file (.toml)",
    )
    parser.add_argument("--db", default=_db_path_default(), metavar="PATH",
                        help="Path to SQLite DB (default: ~/.lore-memory/default.db)")
    parser.add_argument("--out", metavar="PATH",
                        help="Output file path. Defaults to stdout.")
    parser.add_argument("--name", default="my-pack",
                        help="Pack name (default: my-pack)")
    parser.add_argument("--version", default="0.1.0",
                        help="Pack version (default: 0.1.0)")
    parser.add_argument("--description", default="fixcache memory pack",
                        help="Pack description")
    parser.add_argument("--author", default="",
                        help="Author string")
    parser.add_argument("--license", default="MIT", dest="license_",
                        help="License identifier (default: MIT)")
    parser.add_argument("--ecosystem", metavar="ECO",
                        help="Filter recipes to one ecosystem (python, node, rust, ...)")
    parser.add_argument("--min-seen", type=int, default=1, metavar="N",
                        help="Only export fingerprints seen >= N times (default: 1)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Output as JSON instead of TOML (for debugging)")

    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"Error: DB not found at {db_path}", file=sys.stderr)
        print("Run 'fixcache fix <error> --steps ...' first to populate the DB.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        recipes = load_recipes(conn, ecosystem_filter=args.ecosystem, min_seen=args.min_seen)
    finally:
        conn.close()

    if not recipes:
        print("No recipes found matching the filters. Try lowering --min-seen.", file=sys.stderr)
        return 1

    print(f"Exporting {len(recipes)} recipes...", file=sys.stderr)

    if args.as_json:
        output = json.dumps(recipes, indent=2)
    else:
        output = build_pack_toml(
            recipes=recipes,
            name=args.name,
            version=args.version,
            description=args.description,
            author=args.author,
            license_=args.license_,
        )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
        print(f"Written: {out_path}  ({len(recipes)} recipes)", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
