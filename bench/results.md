# Fingerprint Collapse Benchmark

**Claim under test:** lore-memory collapses semantically-equivalent errors
to the same fingerprint hash, so one learned fix compounds across every
repo and agent that hits the same class of failure.

## TL;DR

| | Samples | Unique fps | Collapse ratio | Macro precision | Across-class purity |
|---|---:|---:|---:|---:|---:|
| **Before** (v0.3.1 shipped code) | 45 | 40 | 1.12x | 37.0% | 100.0% |
| **After** (targeted redactors + top_frame drop) | 45 | **11** | **4.09x** | **100.0%** | 100.0% |

The benchmark itself exposed the gap. We patched `fingerprint.py`, re-ran,
and the numbers moved. No cross-class collisions either way.

## What we measure

A corpus of 45 real-shape errors grouped into 11 equivalence classes
(see `bench/corpus.jsonl`). Every sample in a class is the same underlying
failure with different surface details (package names, filenames, line
numbers, type names). A correct fingerprinter produces one hash per class
and never collides across classes.

- **Collapse ratio** = `samples / unique_fingerprints`.
  `1.0` = no collapse; `10.0` = 10x compression. Higher is better.
- **Macro precision** = average fraction of samples in each class that
  agree on one fingerprint. `100%` = every class collapses cleanly.
- **Across-class purity** = fraction of fingerprints owned by exactly one
  class. `100%` = no false matches between classes.

## Before — v0.3.1 shipped code

```
samples             : 45
unique fingerprints : 40
collapse ratio      : 1.12x
macro precision     : 37.0%
across-class purity : 100.0%
```

| Class | N | Distinct fps | Modal share |
|---|---:|---:|---:|
| `file-not-found` | 4 | 1 | 100% |
| `go-undefined` | 3 | 3 | 33% |
| `node-cannot-find-module` | 5 | 5 | 20% |
| `node-undefined-not-fn` | 4 | 4 | 25% |
| `py-attribute-none` | 5 | 5 | 20% |
| `py-import-error` | 3 | 3 | 33% |
| `py-module-not-found` | 6 | 6 | 17% |
| `py-syntax-invalid` | 4 | 4 | 25% |
| `py-type-not-subscriptable` | 4 | 4 | 25% |
| `rust-unused-import` | 3 | 3 | 33% |
| `shell-command-not-found` | 4 | 2 | 75% |

**The headline claim was false as shipped.** `ModuleNotFoundError: No module
named 'sklearn'` and `... 'pandas'` produced different fingerprints. The
benchmark made this concrete for the first time.

### Root causes

1. **`top_frame` was part of the canonical hash.** Every distinct filename
   (`handler.go`, `api.go`, `db.go`) produced a distinct hash, defeating
   cross-repo collapse. It was also a privacy smell — real filenames
   baked into the hash.
2. **Generic quoted-literal redaction needed 8+ chars.** `'foo'`, `'numpy'`,
   `'pandas'` (all under 8 chars) leaked through unredacted, so every
   package name produced a different essence.
3. **No targeted patterns for common error shapes.** `No module named X`,
   `Cannot find module X`, `'T' object has no attribute X`, `undefined: X`
   all had their value-specific tokens baked into the fingerprint.
4. **`bash: docker: command not found`** routed to ecosystem `docker`
   because the `docker` cue was checked before the `shell` cue.
5. **Relative-path redactor didn't swallow `:line:col` suffixes**, so
   `./handler.go:18:5` and `./api.go:8:5` stayed distinct after redaction.

## After — fingerprint.py patched

```
samples             : 45
unique fingerprints : 11
collapse ratio      : 4.09x
macro precision     : 100.0%
across-class purity : 100.0%
```

| Class | N | Distinct fps | Modal share |
|---|---:|---:|---:|
| `file-not-found` | 4 | 1 | 100% |
| `go-undefined` | 3 | 1 | 100% |
| `node-cannot-find-module` | 5 | 1 | 100% |
| `node-undefined-not-fn` | 4 | 1 | 100% |
| `py-attribute-none` | 5 | 1 | 100% |
| `py-import-error` | 3 | 1 | 100% |
| `py-module-not-found` | 6 | 1 | 100% |
| `py-syntax-invalid` | 4 | 1 | 100% |
| `py-type-not-subscriptable` | 4 | 1 | 100% |
| `rust-unused-import` | 3 | 1 | 100% |
| `shell-command-not-found` | 4 | 1 | 100% |

One fingerprint per equivalence class. No cross-class collisions.

### The fix (five changes to `fingerprint.py`)

1. **Dropped `top_frame` from the canonical hash.** Still returned on the
   `Fingerprint` dataclass for display; no longer contributes to the hash.
2. **Added a `_TARGETED_REDACTORS` table** that canonicalizes common error
   shapes before the generic redactors run:
   - `No module named 'X'` → `No module named '<mod>'`
   - `Cannot find module 'X'` → `Cannot find module '<mod>'`
   - `'T' object has no attribute 'X'` → `'<type>' object has no attribute '<attr>'`
   - `'T' object is not subscriptable / iterable / callable`
   - `X is not a function` / `X is not defined`
   - `cannot import name 'X' from 'Y'`
   - `undefined: X` (Go)
   - `` unused import: `X` `` (Rust)
   - `cmd: command not found`
   - `path: No such file or directory`
3. **Extended the relative-path redactor** to swallow `:line:col` suffixes
   so `./handler.go:18:5` collapses to `./<file>`.
4. **Reordered ecosystem cues** so `shell` is checked before `docker`:
   `bash: docker: command not found` now routes to shell (the surrounding
   context) rather than docker (a word that appears in the error).
5. **Added two new tests** (`test_same_shape_module_not_found_collapses`,
   `test_different_error_classes_do_not_collide`) and inverted the old
   `test_different_errors_different_hash`, which had been silently
   documenting the bug as intended behavior.

## Running the benchmark

```bash
python bench/run_collapse.py            # print table to stdout
python bench/run_collapse.py --md       # regenerate this file
python bench/run_collapse.py --json     # raw metrics for CI
```

## What this benchmark does NOT measure

- **Wall-clock fix recurrence** across real repositories over time. That's
  the V2 benchmark — pick 10 public repos with flaky CI, run lore-memory
  in hook mode for 2 weeks, publish the recurrence delta.
- **Fix efficacy.** Whether a matched recipe actually resolves the error.
  The Darwin Replay layer tracks this via Bayesian alpha/beta counts once
  fixes are rated via `lore-memory darwin report <pattern> success|fail`.
- **Adversarial false-positive rate.** 100% across-class purity shows
  classes don't collide *in this corpus*; adversarial inputs (errors whose
  surface mimics a known class but whose root cause differs) need a
  separate eval.

This benchmark answers exactly one question: *does the fingerprinter
collapse semantically-equivalent errors to the same hash?* Answer:
**yes, now it does.**

---

*Corpus: `bench/corpus.jsonl`. Runner: `bench/run_collapse.py`.*
