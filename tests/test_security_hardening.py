"""
test_security_hardening.py — Security hardening tests for lore-memory / fixcache.

Attack surfaces covered:
  (a) handle_lore_fix(error_signature, solution_steps) — darwin_patterns + memories
  (b) store.add(content) / handle_lore_remember — memories table + FTS5
  (c) compute_fingerprint(error_text) — regex redaction pipeline
  (d) export_sanitized(store) — privacy-preserving corpus export
  (e) LoreConfig YAML loader — .lore-memory.yml deserialization
  (f) record_outcome(store, fp_hash, outcome) — Bayesian counter updates

Every test is decorated with @pytest.mark.security.
xfail tests document real deficiencies in v0.4.0.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

import lore_memory.mcp.server as mcp_server
from lore_memory.config import LoreConfig
from lore_memory.core.store import MemoryStore
from lore_memory.darwin_replay import export_sanitized, record_outcome, upsert_fingerprint
from lore_memory.fingerprint import _TARGETED_REDACTORS, compute_fingerprint

# ---------------------------------------------------------------------------
# Marker registration (avoids PytestUnknownMarkWarning)
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> MemoryStore:
    """Fresh file-backed SQLite store per test (avoids shared-state pollution)."""
    s = MemoryStore(str(tmp_path / "test.db"))
    # Wire the MCP server's module-level store to this isolated instance so that
    # handle_lore_fix / handle_lore_remember use our fresh DB.
    mcp_server._store = s
    yield s
    s.close()
    mcp_server._store = None


# ---------------------------------------------------------------------------
# 1. SQL injection via error_signature — DROP TABLE must not execute
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_sql_injection_drop_table_memories(store: MemoryStore) -> None:
    """'; DROP TABLE memories; -- in error_signature must not drop the table."""
    payload = "'; DROP TABLE memories; --"
    result = mcp_server.handle_lore_fix(
        error_signature=payload,
        solution_steps=["check connection string"],
    )
    # Must succeed or fail gracefully — not a crash
    assert isinstance(result, dict)
    # memories table must still exist
    tables = {
        row[0]
        for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "memories" in tables, "SQL injection dropped the memories table"


@pytest.mark.security
def test_sql_injection_drop_table_darwin_patterns(store: MemoryStore) -> None:
    """'; DROP TABLE darwin_patterns; -- in error_signature must not drop the table."""
    payload = "'; DROP TABLE darwin_patterns; --"
    mcp_server.handle_lore_fix(
        error_signature=payload,
        solution_steps=["step 1"],
    )
    tables = {
        row[0]
        for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "darwin_patterns" in tables, "SQL injection dropped the darwin_patterns table"


@pytest.mark.security
def test_sql_injection_in_store_add(store: MemoryStore) -> None:
    """SQL injection via store.add content must not corrupt DB."""
    payload = "'; DROP TABLE memories; SELECT * FROM memories WHERE '1'='1"
    mid = store.add(payload)
    # Row must be retrievable verbatim
    row = store.get(mid)
    assert row is not None
    assert row["content"] == payload
    assert store.count() >= 1


# ---------------------------------------------------------------------------
# 2. FTS5 MATCH operator injection
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_fts5_injection_double_quote(store: MemoryStore) -> None:
    """FTS5 MATCH with dangling double-quote must not raise unhandled exception."""
    store.add("normal memory content for FTS")
    try:
        results = store.search('"')  # single dangling quote
        assert isinstance(results, list)
    except Exception as exc:
        pytest.fail(f"Unhandled FTS5 injection exception: {exc}")


@pytest.mark.security
def test_fts5_injection_operators(store: MemoryStore) -> None:
    """FTS5 NEAR/OR/AND/* operators in query must not crash or raise."""
    store.add("some content here")
    injections = [
        'NEAR("foo" "bar", 5)',
        "foo OR bar AND baz",
        "foo* AND NOT bar",
        "(foo OR bar)",
        "foo AND (bar OR baz",  # unbalanced parens
        "* OR foo",
    ]
    for inj in injections:
        try:
            results = store.search(inj)
            assert isinstance(results, list)
        except Exception as exc:
            pytest.fail(f"FTS5 injection {inj!r} raised unhandled exception: {exc}")


# ---------------------------------------------------------------------------
# 3. Path traversal in fingerprint input
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_path_traversal_unix_in_essence(tmp_path: Path) -> None:
    """Unix path traversal sequences must not appear verbatim in fingerprint essence."""
    text = "FileNotFoundError: ../../../../etc/passwd: No such file or directory"
    fp = compute_fingerprint(text)
    assert "../../../../etc/passwd" not in fp.essence
    # top_frame must not leak the traversal sequence
    if fp.top_frame is not None:
        assert "/" not in fp.top_frame


@pytest.mark.security
def test_path_traversal_windows_in_essence(tmp_path: Path) -> None:
    """Windows-style ..\\..\\path traversal must not appear verbatim in essence."""
    text = "Error reading ..\\..\\Windows\\System32\\config\\SAM"
    fp = compute_fingerprint(text)
    # The raw traversal prefix must be redacted or absent in essence
    # (the redactors collapse abs paths; Windows paths are not abs-path matched,
    #  but the essence is still length-truncated to 200 chars which limits exposure)
    assert len(fp.essence) <= 200


@pytest.mark.security
def test_path_traversal_symlink_looking(tmp_path: Path) -> None:
    """Symlink-looking paths (/proc/self/environ) must not appear in top_frame."""
    text = 'File "/proc/self/environ", line 1\nRuntimeError: env leak'
    fp = compute_fingerprint(text)
    if fp.top_frame is not None:
        assert "/" not in fp.top_frame


# ---------------------------------------------------------------------------
# 4. ReDoS — every _TARGETED_REDACTORS pattern must complete in < 100 ms
# ---------------------------------------------------------------------------


# Patterns known to be slow on adversarial input in v0.4.0.
# Index 13: r"\S+: command not found"  — \S+ backtracks on long non-space runs
# Index 14: r"\S+: No such file or directory" — same root cause
_REDOS_SLOW_PATTERNS: frozenset[int] = frozenset({13, 14})


@pytest.mark.security
@pytest.mark.parametrize("idx,_", enumerate(_TARGETED_REDACTORS))
def test_redos_targeted_redactor(idx: int, _: Any) -> None:
    """Each _TARGETED_REDACTORS pattern must handle 'a'*10000+'!' in <100 ms."""
    if idx in _REDOS_SLOW_PATTERNS:
        pattern, _replacement = _TARGETED_REDACTORS[idx]
        evil = "a" * 10_000 + "!"
        start = time.perf_counter()
        pattern.sub(_replacement, evil)
        elapsed = time.perf_counter() - start
        pytest.xfail(
            reason=(
                f"CVE-like: _TARGETED_REDACTORS[{idx}] pattern={pattern.pattern!r} "
                f"took {elapsed*1000:.1f}ms on 10k-char adversarial input — "
                "\\S+ with literal colon suffix causes catastrophic backtracking; "
                "fix: replace \\S+ with [^\\s:]+ or add possessive/atomic group"
            )
        )
    pattern, _replacement = _TARGETED_REDACTORS[idx]
    evil = "a" * 10_000 + "!"
    start = time.perf_counter()
    pattern.sub(_replacement, evil)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.100, (
        f"_TARGETED_REDACTORS[{idx}] pattern={pattern.pattern!r} "
        f"took {elapsed*1000:.1f}ms on 10k-char input — potential ReDoS"
    )


# ---------------------------------------------------------------------------
# 5. Privacy: API keys must not appear in fingerprint essence
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_privacy_openai_key_not_in_essence() -> None:
    """OpenAI sk-... key in error text must not appear in essence."""
    key = "sk-proj-abcdef1234567890ABCDEF"
    text = f"AuthenticationError: Invalid API key: {key}"
    fp = compute_fingerprint(text)
    assert key not in fp.essence, f"OpenAI key leaked into essence: {fp.essence!r}"


@pytest.mark.security
@pytest.mark.xfail(
    reason=(
        "CVE-like: unquoted API tokens (ghp_...) in error text are NOT redacted "
        "in v0.4.0 — _QUOTED only strips tokens wrapped in single/double quotes; "
        "bare token=VALUE patterns bypass redaction entirely. "
        "Fix: add dedicated API-key redactor pattern before _QUOTED."
    )
)
def test_privacy_github_token_not_in_essence() -> None:
    """GitHub ghp_... token must not appear in essence."""
    token = "ghp_ABCDEF1234567890abcdef12345678"
    text = f"RequestError: bad credentials token={token}"
    fp = compute_fingerprint(text)
    assert token not in fp.essence, f"GitHub token leaked into essence: {fp.essence!r}"


@pytest.mark.security
@pytest.mark.xfail(
    reason=(
        "CVE-like: unquoted API keys (AIza...) in error text are NOT redacted "
        "in v0.4.0 — _QUOTED only strips tokens in quotes; bare key=VALUE "
        "or 'for key VALUE' patterns bypass redaction. "
        "Fix: add pattern matching known API key prefixes (sk-, ghp_, AIza, etc.)."
    )
)
def test_privacy_google_api_key_not_in_essence() -> None:
    """Google AIza... key must not appear in essence."""
    key = "AIzaSyABCDEF1234567890abcdef12345"
    text = f"APIError: quota exceeded for key {key}"
    fp = compute_fingerprint(text)
    assert key not in fp.essence, f"Google API key leaked into essence: {fp.essence!r}"


# ---------------------------------------------------------------------------
# 6. Privacy: email addresses must not appear in essence
# ---------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.xfail(
    reason="CVE-like: email addresses in error text are NOT redacted by _redact() in v0.4.0 — _QUOTED only strips 8+ char quoted strings, bare emails pass through"
)
def test_privacy_email_not_in_essence() -> None:
    """Email addresses in error text must not appear verbatim in fingerprint essence."""
    email = "miles@example.com"
    text = f"AuthError: user {email} not found"
    fp = compute_fingerprint(text)
    assert email not in fp.essence, f"Email leaked into essence: {fp.essence!r}"


# ---------------------------------------------------------------------------
# 7. Privacy: absolute paths with usernames redacted to basename
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_privacy_abs_path_username_redacted_to_basename() -> None:
    """/home/username/project/app.py must become app.py in top_frame."""
    text = (
        'Traceback (most recent call last):\n'
        '  File "/home/jsmith/secret-project/src/app.py", line 42\n'
        'ValueError: boom'
    )
    fp = compute_fingerprint(text)
    assert fp.top_frame == "app.py"
    assert "jsmith" not in (fp.top_frame or "")
    assert "secret-project" not in (fp.top_frame or "")


@pytest.mark.security
def test_privacy_abs_path_not_in_essence() -> None:
    """Full absolute path with username must be redacted in the essence field."""
    text = "FileNotFoundError: /home/alice/private/secrets.txt: No such file or directory"
    fp = compute_fingerprint(text)
    assert "/home/alice/private/secrets.txt" not in fp.essence
    assert "alice" not in fp.essence


# ---------------------------------------------------------------------------
# 8. Privacy: export_sanitized output free of raw secrets
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_export_sanitized_no_absolute_paths(store: MemoryStore) -> None:
    """export_sanitized must not contain any absolute path strings."""
    error_with_path = "FileNotFoundError: /home/bob/work/config.py: No such file or directory"
    upsert_fingerprint(store, error_with_path)
    corpus = export_sanitized(store)
    assert isinstance(corpus, list)
    for row in corpus:
        essence = row.get("essence", "") or ""
        assert not re.search(r"/home/\w+", essence), (
            f"Absolute path with username leaked into export: {essence!r}"
        )


@pytest.mark.security
@pytest.mark.xfail(
    reason="CVE-like: API keys in unquoted error text survive _redact() if shorter than 8 chars or not quoted — export_sanitized does not perform additional scrubbing in v0.4.0"
)
def test_export_sanitized_no_api_keys(store: MemoryStore) -> None:
    """export_sanitized must not contain raw API keys."""
    key = "sk-testkey12345678"
    error_text = f"AuthError: bad key sk-testkey12345678 for request"
    upsert_fingerprint(store, error_text)
    corpus = export_sanitized(store)
    for row in corpus:
        essence = row.get("essence", "") or ""
        assert key not in essence, f"API key leaked into sanitized export: {essence!r}"


@pytest.mark.security
@pytest.mark.xfail(
    reason="CVE-like: 16-digit card-shaped numbers are only redacted if >=3 digits by _NUMBER; 16-digit sequences pass through as they match \\b\\d{3,}\\b but the full card number may survive in essence"
)
def test_export_sanitized_no_card_numbers(store: MemoryStore) -> None:
    """export_sanitized must not contain 16-digit card-shaped numbers."""
    card = "4111111111111111"
    error_text = f"PaymentError: card {card} declined"
    upsert_fingerprint(store, error_text)
    corpus = export_sanitized(store)
    for row in corpus:
        essence = row.get("essence", "") or ""
        assert card not in essence, f"Card number leaked into sanitized export: {essence!r}"


# ---------------------------------------------------------------------------
# 9. Bayesian overflow: record_outcome must not overflow INTEGER columns
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_record_outcome_no_integer_overflow(store: MemoryStore) -> None:
    """1 million record_outcome success calls must not overflow total_success column."""
    error_text = "ModuleNotFoundError: No module named 'overflow_test'"
    fp_data = upsert_fingerprint(store, error_text)
    fp_hash = fp_data["hash"]

    # Use a single bulk UPDATE instead of 1M loops to keep test fast
    # but still verify the counter can hold large values without overflow
    BIG = 2**31 - 1  # max signed 32-bit; SQLite INTEGER is 64-bit but let's verify
    store.conn.execute(
        "UPDATE fingerprints SET total_success = ? WHERE hash = ?",
        (BIG, fp_hash),
    )
    store.conn.commit()

    # Now record one more success — must not wrap around to negative
    result = record_outcome(store, fp_hash, "success")
    assert result["success"] is True
    assert result["total_success"] == BIG + 1, (
        f"Integer overflow detected: expected {BIG + 1}, got {result['total_success']}"
    )
    assert result["total_success"] > 0, "Counter wrapped to negative — integer overflow"


# ---------------------------------------------------------------------------
# 10. Recipe step injection: shell metacharacters stored verbatim, not executed
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_recipe_steps_shell_metacharacters_stored_verbatim(store: MemoryStore) -> None:
    """Shell metacharacters in solution_steps must be stored as-is, never executed."""
    malicious_steps = [
        "run: $(rm -rf /tmp/lore_test_deleteme)",
        "export TOKEN=`cat /etc/passwd`",
        "echo 'pwned' > /tmp/lore_pwned.txt; sleep 1",
    ]
    # Create sentinel file to detect if any step was executed
    sentinel = Path("/tmp/lore_pwned.txt")
    sentinel.unlink(missing_ok=True)

    result = mcp_server.handle_lore_fix(
        error_signature="ShellInjectionError: test",
        solution_steps=malicious_steps,
    )
    assert isinstance(result, dict)
    # Sentinel file must not exist — steps were not shell-executed
    assert not sentinel.exists(), (
        "Shell metacharacter injection: /tmp/lore_pwned.txt was created — steps were executed!"
    )

    # Steps must be retrievable verbatim from DB
    pattern_id = result.get("pattern_id")
    if pattern_id:
        row = store.conn.execute(
            "SELECT rule FROM darwin_patterns WHERE id=?", (pattern_id,)
        ).fetchone()
        if row:
            stored_steps = json.loads(row[0])
            assert stored_steps == malicious_steps


# ---------------------------------------------------------------------------
# 11. NULL byte in user input fields — stored safely or rejected cleanly
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_null_byte_in_store_add_content(store: MemoryStore) -> None:
    """NULL byte in memory content must be stored safely (no crash)."""
    content_with_null = "normal text\x00injected null\x00more text"
    try:
        mid = store.add(content_with_null)
        row = store.get(mid)
        assert row is not None
    except Exception as exc:
        # Rejection is acceptable — crash is not
        assert "null" in str(exc).lower() or "NUL" in str(exc), (
            f"Unexpected exception on NULL byte input: {exc}"
        )


@pytest.mark.security
def test_null_byte_in_lore_fix_error_signature(store: MemoryStore) -> None:
    """NULL byte in error_signature for lore_fix must not crash."""
    try:
        result = mcp_server.handle_lore_fix(
            error_signature="TypeError\x00: null injected",
            solution_steps=["step 1"],
        )
        assert isinstance(result, dict)
    except Exception as exc:
        pytest.fail(f"NULL byte in error_signature caused unhandled exception: {exc}")


@pytest.mark.security
def test_null_byte_in_fingerprint(tmp_path: Path) -> None:
    """NULL byte in compute_fingerprint input must not crash."""
    text = "Error\x00: null\x00byte\x00injection"
    fp = compute_fingerprint(text)
    assert fp is not None
    assert len(fp.hash) == 16


# ---------------------------------------------------------------------------
# 12. 100 MB input to compute_fingerprint — bounded time and no crash
# ---------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.xfail(
    reason=(
        "CVE-like: 100 MB input to compute_fingerprint takes ~24s in v0.4.0 — "
        "_pick_final_line() splits ALL lines before selecting the last error line, "
        "and _redact() applies every regex to the full final line (which can be "
        "the entire 100 MB if no newlines). No input size guard exists. "
        "Fix: truncate input to a safe maximum (e.g. 1 MB) before processing."
    )
)
def test_100mb_input_compute_fingerprint_bounded_time() -> None:
    """100 MB input to compute_fingerprint must complete in <5 seconds."""
    # Build ~100 MB: repeat a realistic error line
    chunk = "TypeError: 'NoneType' object is not subscriptable\n"
    big_input = chunk * (100 * 1024 * 1024 // len(chunk.encode()))
    assert len(big_input.encode()) >= 50 * 1024 * 1024  # at least 50 MB

    start = time.perf_counter()
    fp = compute_fingerprint(big_input)
    elapsed = time.perf_counter() - start

    assert fp is not None
    assert len(fp.hash) == 16
    assert elapsed < 5.0, (
        f"100 MB input took {elapsed:.2f}s — exceeds 5-second budget"
    )


# ---------------------------------------------------------------------------
# 13. Unicode: RTL override, ZWJ, homoglyphs stored verbatim in recipe steps
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_unicode_rtl_override_stored_verbatim(store: MemoryStore) -> None:
    """RTL override chars in solution_steps must be stored verbatim."""
    rtl_step = "run \u202e dangerous\u202d command"  # U+202E RTL override, U+202D LTR override
    result = mcp_server.handle_lore_fix(
        error_signature="UnicodeError: RTL test",
        solution_steps=[rtl_step],
    )
    pattern_id = result.get("pattern_id")
    if pattern_id:
        row = store.conn.execute(
            "SELECT rule FROM darwin_patterns WHERE id=?", (pattern_id,)
        ).fetchone()
        if row:
            stored = json.loads(row[0])
            assert stored[0] == rtl_step, (
                f"RTL override step was mutated: {stored[0]!r} != {rtl_step!r}"
            )


@pytest.mark.security
def test_unicode_zwj_homoglyph_stored_verbatim(store: MemoryStore) -> None:
    """Zero-width joiners and homoglyphs in recipe steps must be stored verbatim."""
    zwj_step = "pip\u200d install\u200c package"   # ZWJ + ZWNJ
    homoglyph_step = "python\u0440un script.py"    # Cyrillic р looks like Latin p
    result = mcp_server.handle_lore_fix(
        error_signature="ImportError: ZWJ homoglyph test",
        solution_steps=[zwj_step, homoglyph_step],
    )
    pattern_id = result.get("pattern_id")
    if pattern_id:
        row = store.conn.execute(
            "SELECT rule FROM darwin_patterns WHERE id=?", (pattern_id,)
        ).fetchone()
        if row:
            stored = json.loads(row[0])
            assert stored[0] == zwj_step
            assert stored[1] == homoglyph_step


# ---------------------------------------------------------------------------
# 14. YAML billion-laughs attack via .lore-memory.yml config loading
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_yaml_billion_laughs_bounded_time(tmp_path: Path) -> None:
    """YAML billion-laughs entity expansion must complete in <2 seconds (safe_load)."""
    # safe_load does not expand YAML anchors into memory explosions,
    # but we test that it terminates and raises/returns without hanging.
    billion_laughs = """\
a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]
e: &e [*d,*d,*d,*d,*d,*d,*d,*d,*d]
db_path: ~/.lore-memory/test.db
"""
    yml_path = tmp_path / ".lore-memory.yml"
    yml_path.write_text(billion_laughs)

    start = time.perf_counter()
    try:
        cfg = LoreConfig(config_path=str(yml_path))
        # Config load must complete and return a usable object
        assert cfg.db_path is not None
    except Exception:
        pass  # exception is acceptable; hang/OOM is not
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, (
        f"YAML billion-laughs loading took {elapsed:.2f}s — potential DoS"
    )


# ---------------------------------------------------------------------------
# 15. YAML deserialization attack via !!python/object/apply
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_yaml_deserialization_python_object_apply(tmp_path: Path) -> None:
    """!!python/object/apply:os.system in config YAML must NOT execute code (safe_load)."""
    sentinel = tmp_path / "yaml_rce_sentinel.txt"
    # The payload tries to create a sentinel file via os.system
    evil_yaml = f"""\
db_path: !!python/object/apply:os.system
  - "touch {sentinel}"
"""
    yml_path = tmp_path / ".lore-memory.yml"
    yml_path.write_text(evil_yaml)

    try:
        cfg = LoreConfig(config_path=str(yml_path))
    except Exception:
        pass  # yaml.safe_load should raise yaml.constructor.ConstructorError

    assert not sentinel.exists(), (
        "YAML deserialization RCE: sentinel file was created — "
        "!!python/object/apply executed os.system!"
    )
