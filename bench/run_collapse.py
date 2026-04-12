#!/usr/bin/env python3
"""
bench/run_collapse.py — Fingerprint Collapse Benchmark

Measures whether lore-memory's fingerprinting correctly collapses
semantically-equivalent errors to the same hash.

Metrics:
  collapse ratio   = (# unique errors) / (# unique fingerprints)
                     higher is better; 1.0 = no collapse; 10.0 = 10x collapse
  within-class     = for each class, fraction of samples that share
                     the *modal* fingerprint (precision)
  across-class     = fraction of fingerprints owned by exactly one class
                     (no false collisions between classes)

Usage:
    python bench/run_collapse.py [--corpus bench/corpus.jsonl] [--md]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Ensure repo root on path when run directly
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lore_memory.fingerprint import compute_fingerprint  # noqa: E402


def load_corpus(path: Path) -> list[dict]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def evaluate(rows: list[dict]) -> dict:
    by_class: dict[str, list[str]] = defaultdict(list)
    fp_to_classes: dict[str, set[str]] = defaultdict(set)
    fp_counter: Counter[str] = Counter()
    sample_details: list[dict] = []

    for row in rows:
        cls = row["class"]
        fp = compute_fingerprint(row["text"])
        by_class[cls].append(fp.hash)
        fp_to_classes[fp.hash].add(cls)
        fp_counter[fp.hash] += 1
        sample_details.append(
            {
                "class": cls,
                "hash": fp.hash,
                "error_type": fp.error_type,
                "ecosystem": fp.ecosystem,
                "tool": fp.tool,
                "essence": fp.essence,
            }
        )

    total = len(rows)
    unique_fps = len(fp_counter)
    collapse_ratio = total / unique_fps if unique_fps else 0.0

    # within-class precision: what fraction of samples hit the modal fp?
    within_class = {}
    for cls, fps in by_class.items():
        c = Counter(fps)
        modal_count = c.most_common(1)[0][1] if c else 0
        within_class[cls] = {
            "n": len(fps),
            "distinct_fps": len(c),
            "modal_share": modal_count / len(fps) if fps else 0.0,
        }

    # across-class: fraction of fingerprints that are class-pure (owned by 1 class)
    pure_fps = sum(1 for classes in fp_to_classes.values() if len(classes) == 1)
    across_class_purity = pure_fps / unique_fps if unique_fps else 0.0

    # average within-class modal share (macro)
    macro_precision = (
        sum(v["modal_share"] for v in within_class.values()) / len(within_class)
        if within_class
        else 0.0
    )

    return {
        "total": total,
        "unique_fps": unique_fps,
        "collapse_ratio": collapse_ratio,
        "macro_precision": macro_precision,
        "across_class_purity": across_class_purity,
        "by_class": within_class,
        "samples": sample_details,
        "fp_counter": dict(fp_counter),
        "fp_to_classes": {k: sorted(v) for k, v in fp_to_classes.items()},
    }


def render_md(result: dict) -> str:
    lines = []
    lines.append("# Fingerprint Collapse Benchmark")
    lines.append("")
    lines.append("**Claim under test:** lore-memory collapses semantically-equivalent errors")
    lines.append("to the same fingerprint hash, enabling cross-repo fix efficacy tracking.")
    lines.append("")
    lines.append("## Headline numbers")
    lines.append("")
    lines.append("| Metric | Value | What it means |")
    lines.append("|---|---:|---|")
    lines.append(
        f"| Samples | {result['total']} | raw errors in corpus |"
    )
    lines.append(
        f"| Unique fingerprints | {result['unique_fps']} | distinct hashes produced |"
    )
    lines.append(
        f"| **Collapse ratio** | **{result['collapse_ratio']:.2f}x** | 1.0 = no collapse; higher = better |"
    )
    lines.append(
        f"| Within-class precision (macro) | {result['macro_precision']*100:.1f}% | % of samples in each class agreeing on one fp |"
    )
    lines.append(
        f"| Across-class purity | {result['across_class_purity']*100:.1f}% | % of fingerprints owned by exactly one class |"
    )
    lines.append("")
    lines.append("## Per-class breakdown")
    lines.append("")
    lines.append("| Class | N | Distinct fps | Modal share |")
    lines.append("|---|---:|---:|---:|")
    for cls in sorted(result["by_class"]):
        v = result["by_class"][cls]
        lines.append(
            f"| `{cls}` | {v['n']} | {v['distinct_fps']} | {v['modal_share']*100:.0f}% |"
        )
    lines.append("")
    lines.append("## Cross-class collisions (fingerprints shared by multiple classes)")
    lines.append("")
    collisions = {
        h: classes
        for h, classes in result["fp_to_classes"].items()
        if len(classes) > 1
    }
    if not collisions:
        lines.append("_None. Every fingerprint belongs to a single class._")
    else:
        lines.append("| Fingerprint | Classes |")
        lines.append("|---|---|")
        for h, classes in collisions.items():
            lines.append(f"| `{h}` | {', '.join(classes)} |")
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("- **Collapse ratio** is the compounding story: 100 errors becoming 10 fingerprints")
    lines.append("  means one learned fix applies to 10x as many future failures.")
    lines.append("- **Within-class precision** near 100% means each equivalence class collapses")
    lines.append("  cleanly — one fix recipe per class.")
    lines.append("- **Across-class purity** near 100% means no false collisions: a fix for one")
    lines.append("  class never gets mistakenly applied to another.")
    lines.append("")
    lines.append("Generated by `python bench/run_collapse.py --md`.")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(REPO / "bench" / "corpus.jsonl"))
    ap.add_argument("--md", action="store_true", help="Write bench/results.md")
    ap.add_argument("--json", action="store_true", help="Print raw JSON")
    args = ap.parse_args()

    rows = load_corpus(Path(args.corpus))
    result = evaluate(rows)

    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2))
        return 0

    print(f"samples             : {result['total']}")
    print(f"unique fingerprints : {result['unique_fps']}")
    print(f"collapse ratio      : {result['collapse_ratio']:.2f}x")
    print(f"macro precision     : {result['macro_precision']*100:.1f}%")
    print(f"across-class purity : {result['across_class_purity']*100:.1f}%")
    print()
    print(f"{'class':<32} {'n':>3} {'dfps':>5} {'modal%':>7}")
    for cls in sorted(result["by_class"]):
        v = result["by_class"][cls]
        print(
            f"{cls:<32} {v['n']:>3} {v['distinct_fps']:>5} {v['modal_share']*100:>6.0f}%"
        )

    collisions = {
        h: classes for h, classes in result["fp_to_classes"].items() if len(classes) > 1
    }
    if collisions:
        print()
        print("CROSS-CLASS COLLISIONS:")
        for h, classes in collisions.items():
            print(f"  {h}  <- {sorted(classes)}")

    if args.md:
        md = render_md(result)
        out = REPO / "bench" / "results.md"
        out.write_text(md)
        print(f"\nwrote {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
