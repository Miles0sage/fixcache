#!/usr/bin/env python3
"""
ingest_swesmith.py — Darwin corpus ingestion from SWE-smith / bench corpus.

Loads labeled error→patch pairs, runs each error through fixcache's
fingerprinter, stores fix recipes in the Darwin corpus, and reports stats.

Usage:
    python scripts/ingest_swesmith.py [--limit N] [--db PATH]

Data sources (tried in order):
    1. SWE-bench/SWE-smith HuggingFace dataset (52K pairs)
    2. /root/lore-lite/bench/corpus.jsonl (45 real errors, fallback)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── Ensure lore_memory is importable ─────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from lore_memory.mcp.server import handle_lore_fix
from lore_memory.darwin_replay import darwin_stats, upsert_fingerprint
from lore_memory.fingerprint import compute_fingerprint
from lore_memory.core.store import MemoryStore


# ── Fix-recipe derivation from corpus class labels ───────────────────────────
# Maps corpus `class` labels → solution_steps.
# These are ground-truth fix recipes for the bench corpus's 11 error classes.
_CLASS_TO_FIX: dict[str, list[str]] = {
    "py-module-not-found": [
        "Identify the missing module from the ModuleNotFoundError message",
        "Install with: pip install <module>",
        "If in a virtual environment, ensure it is activated: source .venv/bin/activate",
        "Verify installation: python -c 'import <module>'",
    ],
    "py-import-error": [
        "Check the ImportError message for the specific symbol or module",
        "Verify the package is installed: pip show <package>",
        "Check for circular imports in your codebase",
        "Ensure __init__.py exports the symbol if it is a local package",
    ],
    "py-attribute-none": [
        "Identify the variable that is None from the AttributeError traceback",
        "Add a None guard: if obj is not None: ... or assert obj is not None",
        "Trace back to where the variable is assigned and fix the None-returning call",
        "Consider using Optional type hints to catch this at static analysis time",
    ],
    "py-type-not-subscriptable": [
        "Identify the non-subscriptable type from the TypeError message",
        "Use isinstance() to verify the type before subscripting",
        "If expecting a list/dict, check the function returning this value",
        "For Python < 3.9, use typing.List / typing.Dict instead of list[]/dict[]",
    ],
    "py-syntax-invalid": [
        "Read the SyntaxError message and line number carefully",
        "Check for missing colons, parentheses, or brackets near the reported line",
        "Look one line above the reported line — Python often flags the wrong line",
        "Use a linter: flake8 or ruff to catch syntax errors before runtime",
    ],
    "node-cannot-find-module": [
        "Identify the missing module from the 'Cannot find module' message",
        "Install with: npm install <module> or yarn add <module>",
        "For local imports check the relative path is correct",
        "Run: npm install to restore all dependencies from package.json",
    ],
    "node-undefined-not-fn": [
        "Identify which variable is undefined from the TypeError message",
        "Check the import/require statement for the module exporting this function",
        "Verify the function name spelling matches the export",
        "Add a typeof guard: if (typeof fn === 'function') fn()",
    ],
    "shell-command-not-found": [
        "Identify the missing command from 'command not found' message",
        "Install the package that provides the command: apt-get install <pkg> or brew install <pkg>",
        "Check if the command is in PATH: which <command>",
        "Add the binary directory to PATH: export PATH=$PATH:/path/to/bin",
    ],
    "file-not-found": [
        "Read the FileNotFoundError / No such file path carefully",
        "Verify the file exists: ls -la <path>",
        "Check the working directory: pwd and adjust relative paths",
        "Create the file or directory if it should exist: mkdir -p <dir> or touch <file>",
    ],
    "rust-unused-import": [
        "Identify the unused import from the compiler warning",
        "Remove the unused use statement",
        "Or suppress with: #[allow(unused_imports)] if needed for re-export",
        "Run: cargo fix --allow-dirty to auto-remove unused imports",
    ],
    "go-undefined": [
        "Identify the undefined symbol from the build error",
        "Check the import path is correct in the go import block",
        "Run: go get <package> to fetch the missing dependency",
        "Verify the symbol is exported (starts with uppercase) from its package",
    ],
}

# Generic fallback for unknown classes
_FALLBACK_FIX = [
    "Read the full error message and traceback carefully",
    "Search the error message verbatim to find relevant documentation or issues",
    "Isolate the failing component with a minimal reproduction",
    "Apply the fix and run tests to verify",
]


def derive_solution_steps(entry: dict[str, Any]) -> list[str]:
    """
    Derive fix recipe steps from a corpus entry.

    For the bench corpus (class + text), look up from _CLASS_TO_FIX.
    For SWE-smith entries, extract from patch/fix fields.
    """
    # SWE-smith format: has patch, FAIL_TO_PASS, problem_statement fields
    if "patch" in entry:
        patch = entry.get("patch", "")
        problem = entry.get("problem_statement", entry.get("issue_text", ""))[:200]
        # FAIL_TO_PASS is a JSON string of test names in SWE-smith
        fail_tests_raw = entry.get("FAIL_TO_PASS", entry.get("fail_to_pass", ""))
        if isinstance(fail_tests_raw, str):
            try:
                fail_tests = json.loads(fail_tests_raw)
            except (json.JSONDecodeError, TypeError):
                fail_tests = [fail_tests_raw] if fail_tests_raw else []
        elif isinstance(fail_tests_raw, list):
            fail_tests = fail_tests_raw
        else:
            fail_tests = []
        steps = [
            f"Problem: {problem}" if problem else "Review the failing test or error",
        ]
        if fail_tests:
            steps.append(f"Fix failing tests: {', '.join(str(t) for t in fail_tests[:3])}")
        steps.append(f"Apply patch:\n{patch[:400]}" if patch else "Apply the fix patch")
        steps.append("Run the test suite to verify: python -m pytest")
        return steps

    # Bench corpus format: class label
    cls = entry.get("class", "")
    if cls in _CLASS_TO_FIX:
        return _CLASS_TO_FIX[cls]

    return _FALLBACK_FIX


def extract_error_signature(entry: dict[str, Any]) -> str:
    """
    Extract the canonical error signature from an entry.

    - Bench corpus: uses the 'text' field (raw traceback/error text)
    - SWE-smith: uses problem_statement + test output
    """
    if "text" in entry:
        return entry["text"]

    # SWE-smith fields (instance_id, patch, FAIL_TO_PASS, repo, problem_statement)
    parts = []
    if "problem_statement" in entry:
        parts.append(entry["problem_statement"][:500])
    # FAIL_TO_PASS is a JSON string of failing test IDs
    fail_raw = entry.get("FAIL_TO_PASS", entry.get("fail_to_pass", ""))
    if fail_raw:
        if isinstance(fail_raw, str):
            try:
                fail_list = json.loads(fail_raw)
                fail_str = ", ".join(str(t) for t in fail_list[:5])
            except (json.JSONDecodeError, TypeError):
                fail_str = fail_raw[:200]
        elif isinstance(fail_raw, list):
            fail_str = ", ".join(str(t) for t in fail_raw[:5])
        else:
            fail_str = str(fail_raw)[:200]
        parts.append(f"Failing tests: {fail_str}")
    if "repo" in entry:
        parts.append(f"Repo: {entry['repo']}")
    return "\n".join(parts) if parts else str(entry)[:500]


def load_swesmith() -> tuple[list[dict[str, Any]], str]:
    """
    Try to load SWE-smith dataset. Returns (entries, source_label).
    Falls back to bench corpus if unavailable.
    """
    try:
        from datasets import load_dataset
        print("Attempting to load SWE-bench/SWE-smith from HuggingFace (streaming)...")
        # Use streaming to avoid loading all 52K entries at once
        ds = load_dataset("SWE-bench/SWE-smith", split="train", streaming=True)
        entries = []
        for item in ds:
            entries.append(dict(item))
        print(f"Loaded {len(entries):,} entries from SWE-smith")
        return entries, "SWE-smith (HuggingFace)"
    except Exception as e:
        print(f"SWE-smith unavailable: {e}")
        print("Falling back to bench corpus...")

    bench_path = _REPO_ROOT / "bench" / "corpus.jsonl"
    if not bench_path.exists():
        raise FileNotFoundError(f"Bench corpus not found at {bench_path}")

    entries = [json.loads(line) for line in bench_path.read_text().splitlines() if line.strip()]
    print(f"Loaded {len(entries)} entries from {bench_path}")
    return entries, f"bench/corpus.jsonl ({bench_path})"


def run_ingestion(
    limit: int | None = None,
    db_path: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Main ingestion pipeline. Returns final stats dict.
    """
    # Override DB path if specified
    if db_path:
        os.environ["LORE_MEMORY_DB"] = db_path

    entries, source = load_swesmith()
    if limit:
        entries = entries[:limit]
        print(f"Limited to {len(entries)} entries")

    total = len(entries)
    print(f"\nIngesting {total} entries from: {source}")
    print("=" * 60)

    ingested = 0
    errors = 0
    seen_fingerprints: set[str] = set()
    t0 = time.time()

    for i, entry in enumerate(entries):
        try:
            error_sig = extract_error_signature(entry)
            solution_steps = derive_solution_steps(entry)

            # Compute fingerprint to track uniqueness
            fp = compute_fingerprint(error_sig)
            seen_fingerprints.add(fp.hash)

            # Derive tags from class label or ecosystem
            tags: list[str] = []
            if "class" in entry:
                tags.append(entry["class"])
            if fp.ecosystem and fp.ecosystem != "unknown":
                tags.append(fp.ecosystem)

            result = handle_lore_fix(
                error_signature=error_sig,
                solution_steps=solution_steps,
                tags=tags if tags else None,
                outcome="success",
            )

            if result.get("success"):
                ingested += 1
                if verbose or (i < 3):
                    print(f"[{i+1}/{total}] OK  fp={fp.hash}  type={fp.error_type}  eco={fp.ecosystem}")
                    print(f"         sig={error_sig[:80].replace(chr(10), ' ')!r}")
            else:
                errors += 1
                print(f"[{i+1}/{total}] ERR {result.get('error')}")

        except Exception as exc:
            errors += 1
            print(f"[{i+1}/{total}] EXCEPTION: {exc}")

        # Progress every 500
        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            print(f"  Progress: {i+1}/{total} ({rate:.0f}/s)  unique_fps={len(seen_fingerprints)}")

    elapsed = time.time() - t0
    unique_fps = len(seen_fingerprints)
    collapse_ratio = 1.0 - (unique_fps / total) if total > 0 else 0.0

    # Fetch corpus-wide darwin stats
    from lore_memory.mcp.server import _get_store
    store = _get_store()
    stats = darwin_stats(store)

    summary = {
        "source": source,
        "total_processed": total,
        "ingested_ok": ingested,
        "errors": errors,
        "unique_fingerprints_this_run": unique_fps,
        "collapse_ratio": round(collapse_ratio, 4),
        "elapsed_seconds": round(elapsed, 2),
        "rate_per_second": round(total / elapsed, 1) if elapsed > 0 else 0,
        "corpus_total_fingerprints": stats["total_fingerprints"],
        "corpus_total_seen_events": stats["total_seen_events"],
        "corpus_top_ecosystems": stats["top_ecosystems"],
        "corpus_top_error_types": stats["top_error_types"],
        "corpus_efficacy_bands": stats["efficacy_bands"],
    }

    return summary


def print_summary(s: dict[str, Any]) -> None:
    print()
    print("=" * 60)
    print("INGESTION COMPLETE")
    print("=" * 60)
    print(f"Source          : {s['source']}")
    print(f"Total processed : {s['total_processed']}")
    print(f"Ingested OK     : {s['ingested_ok']}")
    print(f"Errors          : {s['errors']}")
    print(f"Elapsed         : {s['elapsed_seconds']}s  ({s['rate_per_second']}/s)")
    print()
    print("── Fingerprint stats ─────────────────────────────────────")
    print(f"Unique fingerprints (this run) : {s['unique_fingerprints_this_run']}")
    print(f"Collapse ratio                 : {s['collapse_ratio']:.1%}  "
          f"({s['total_processed'] - s['unique_fingerprints_this_run']} duplicates collapsed)")
    print()
    print("── Darwin corpus (cumulative) ────────────────────────────")
    print(f"Total fingerprints in corpus   : {s['corpus_total_fingerprints']}")
    print(f"Total seen events              : {s['corpus_total_seen_events']}")
    print(f"Efficacy bands                 : {s['corpus_efficacy_bands']}")
    print()
    print("Top ecosystems:")
    for eco, count in s["corpus_top_ecosystems"].items():
        print(f"  {eco:<20} {count}")
    print()
    print("Top error types:")
    for etype, count in s["corpus_top_error_types"].items():
        print(f"  {etype:<35} {count}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest SWE-smith (or bench fallback) into the Darwin corpus"
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Max entries to process (default: all)")
    parser.add_argument("--db", type=str, default=None,
                        help="Override Darwin DB path (default: ~/.lore-memory/default.db)")
    parser.add_argument("--verbose", action="store_true",
                        help="Print every ingested entry")
    args = parser.parse_args()

    summary = run_ingestion(limit=args.limit, db_path=args.db, verbose=args.verbose)
    print_summary(summary)


if __name__ == "__main__":
    main()
