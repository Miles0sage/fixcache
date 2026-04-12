# bench/ — lore-memory benchmarks

Benchmarks live here. Results are reproducible from source; nothing
network-dependent, nothing machine-specific.

## Benchmarks

### Fingerprint Collapse (`run_collapse.py`)

Measures whether the fingerprinter collapses semantically-equivalent
errors to the same hash. This is the direct test of lore-memory's
headline claim that one learned fix compounds across repos.

```bash
python bench/run_collapse.py            # print table to stdout
python bench/run_collapse.py --md       # write bench/results.md
python bench/run_collapse.py --json     # raw metrics for CI
```

**Corpus:** `bench/corpus.jsonl` — 45 real-shape error samples grouped
into 11 equivalence classes (Python/Node/Rust/Go/shell). Each class is
one kind of failure with different surface details.

**Metrics:**
- Collapse ratio = samples / unique fingerprints
- Macro precision = fraction of each class agreeing on one hash
- Across-class purity = fingerprints owned by exactly one class

**Latest headline:** 45 → 11 (4.09x collapse), 100% precision, 100% purity.
See `results.md` for the full before/after breakdown.

## Adding samples

Append a JSON line to `bench/corpus.jsonl`:

```jsonl
{"class": "py-module-not-found", "text": "Traceback ...\nModuleNotFoundError: No module named 'X'"}
```

Every sample with the same `class` should collapse to the same
fingerprint after the patch lands. If it doesn't, the benchmark
will surface it and point at the specific redactor that needs work.

## Design notes

- **Real-shape only.** Samples are modeled on actual errors from actual
  repos. Synthetic minimal cases go in `tests/`; this directory is for
  realism.
- **Equivalence classes are the spec.** The label on each sample is a
  promise: *this is what should collapse*. If the benchmark shows it
  doesn't, either the fingerprinter is wrong or the label is wrong.
- **No runtime dependencies.** The benchmark uses only stdlib + the
  `lore_memory.fingerprint` module, so it can run in CI without
  touching the database layer.
- **Privacy-first by construction.** Every sample in the corpus is
  synthetic-but-realistic — no real paths, hostnames, or secrets. Safe
  to ship as a published benchmark.

## Roadmap

- **V2 — Real-repo recurrence.** Pick 10 public repos with flaky CI,
  run lore-memory in hook mode for 2 weeks, publish the error-recurrence
  delta. This answers the *economic* claim ("fewer repeated failures")
  that V1 doesn't touch.
- **V3 — Adversarial purity.** Hand-crafted errors whose surface form
  mimics a known class but whose root cause differs. Tests whether
  lore-memory can be tricked into applying the wrong fix.
