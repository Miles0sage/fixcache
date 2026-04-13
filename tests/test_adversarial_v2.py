"""
test_adversarial_v2.py — Adversarial stress tests for fixcache fingerprinter.

Goal: find every way the fingerprinter can produce false positives (wrong fix
applied), leak private data, or hang. Each test is either:
  - PASS: fixcache handles it correctly (defended)
  - FAIL/xfail: confirmed kill shot against the 100% purity claim

Categories:
  1. False positives — 10 inputs that could cause the wrong fix to fire
  2. Privacy leaks — 5 inputs where sensitive data survives redaction
  3. ReDoS — 3 patterns that could hang the fingerprinter
"""

from __future__ import annotations

import time

import pytest

from lore_memory.fingerprint import (
    _redact,
    compute_fingerprint,
    fingerprint_hash,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. FALSE POSITIVES — wrong fix applied (purity claim attacks)
#
# For a false positive: two errors from DIFFERENT classes must produce the
# SAME hash. We test that they do NOT collide.
# If they DO collide → fixcache would apply the wrong fix → kill shot.
# ─────────────────────────────────────────────────────────────────────────────


class TestFalsePositives:
    """
    Each test asserts two semantically-different errors produce different hashes.
    A test PASSES means fixcache is DEFENDED (no false positive).
    A test FAILS means a kill shot: the wrong fix would fire.
    """

    def test_fp1_python_vs_node_typeof_collision(self) -> None:
        """
        ATTACK: Python TypeError and Node TypeError on the same message shape.
        Both collapse to 'TypeError | python | unknown | <name> is not a function'.
        Python bare TypeError maps ecosystem via _ERROR_TYPE_TO_ECOSYSTEM → 'python'.
        Node TypeError has no .js cue here either, so also maps to 'python'.
        If a user stored a Python fix for 'TypeError: foo is not a function',
        a Node TypeError with identical message would retrieve the same fix.
        """
        # Python: no Node cues, ecosystem inferred from error_type → python
        py_error = "TypeError: foo is not a function"
        # Node: same message, but real Node stack — has .js cue → node
        node_error = (
            "TypeError: foo is not a function\n"
            "    at Object.<anonymous> (/app/index.js:5:3)"
        )
        h_py = fingerprint_hash(py_error)
        h_node = fingerprint_hash(node_error)
        assert h_py != h_node, (
            "KILL SHOT: Python TypeError 'is not a function' and Node TypeError "
            "with .js stack produce identical hashes — wrong fix would fire "
            f"py={h_py!r} node={h_node!r}"
        )

    def test_fp2_go_undefined_vs_js_undefined(self) -> None:
        """
        ATTACK: Go 'undefined: Foo' vs JavaScript 'Foo is not defined'.
        Both get redacted to similar shapes. Verify they hash differently.
        """
        go_error = "undefined: DatabaseClient\ngo build ./..."
        js_error = "ReferenceError: DatabaseClient is not defined\n    at /app/index.js:3:5"
        h_go = fingerprint_hash(go_error)
        h_js = fingerprint_hash(js_error)
        assert h_go != h_js, (
            f"KILL SHOT: Go 'undefined: X' and JS 'X is not defined' collide: "
            f"go={h_go!r} js={h_js!r}"
        )

    def test_fp3_python_modulenotfound_vs_node_cannot_find(self) -> None:
        """
        ATTACK: Python ModuleNotFoundError and Node 'Cannot find module' for
        the SAME package name ('redis') after redaction.
        Both become 'No module named <mod>' / 'Cannot find module <mod>' but
        the ecosystem differs. If ecosystem detection fails, hashes collide.
        """
        py_error = "ModuleNotFoundError: No module named 'redis'"
        node_error = "Error: Cannot find module 'redis'\nRequire stack:\n- /app/cache.js"
        h_py = fingerprint_hash(py_error)
        h_node = fingerprint_hash(node_error)
        assert h_py != h_node, (
            f"KILL SHOT: Python and Node module-not-found for same package collide: "
            f"py={h_py!r} node={h_node!r}"
        )

    def test_fp4_pytest_failed_prefix_strip_collision(self) -> None:
        """
        ATTACK: A pytest FAILED summary line smuggles a *different* error class
        after the ' - ' separator. The _pick_final_line strips the prefix and
        returns the suffix. If the suffix after stripping matches a stored
        fingerprint from a totally different context, wrong fix fires.

        Example: store fix for bare 'PermissionError: [Errno 13]'.
        Then a pytest line 'FAILED test_deploy.py - PermissionError: [Errno 13]'
        strips to 'PermissionError: [Errno 13]' — same hash, correct.
        But what if the pytest prefix tricks detection into a different error_type?
        """
        bare = "PermissionError: [Errno 13] Permission denied: '/etc/shadow'"
        pytest_wrapped = "FAILED tests/test_deploy.py::test_root - PermissionError: [Errno 13] Permission denied: '/etc/shadow'"
        h_bare = fingerprint_hash(bare)
        h_wrapped = fingerprint_hash(pytest_wrapped)
        assert h_bare == h_wrapped, (
            f"REGRESSION: pytest-wrapped and bare PermissionError hash differently — "
            f"primary lookup would miss stored fix. bare={h_bare!r} wrapped={h_wrapped!r}"
        )

    def test_fp5_rust_error_vs_go_error_same_surface(self) -> None:
        """
        ATTACK: Rust 'cannot find `X` in this scope' vs a message that looks
        similar but belongs to a different language. Ecosystem cue (`.rs` file)
        must differentiate them.
        """
        rust_error = (
            "error[E0412]: cannot find type `Handler` in this scope\n"
            " --> src/main.rs:10:12"
        )
        # Fabricated: same surface text but in a .go context
        go_lookalike = (
            "error: cannot find `Handler` in this scope\n"
            " --> main.go:10:12"
        )
        h_rust = fingerprint_hash(rust_error)
        h_go = fingerprint_hash(go_lookalike)
        assert h_rust != h_go, (
            f"KILL SHOT: Rust and Go 'cannot find X in scope' collide: "
            f"rust={h_rust!r} go={h_go!r}"
        )

    def test_fp6_filenotfound_python_vs_shell(self) -> None:
        """
        ATTACK: Python FileNotFoundError vs shell 'No such file or directory'.
        Both get redacted to similar forms. Ecosystem must differ.
        Python has explicit error class; shell cue triggers on 'bash'.
        """
        py_error = "FileNotFoundError: [Errno 2] No such file or directory: '/app/config.py'"
        shell_error = "bash: /app/config.py: No such file or directory"
        h_py = fingerprint_hash(py_error)
        h_shell = fingerprint_hash(shell_error)
        assert h_py != h_shell, (
            f"KILL SHOT: Python FileNotFoundError and shell No-such-file collide: "
            f"py={h_py!r} shell={h_shell!r}"
        )

    def test_fp7_attribute_error_type_collapse_cross_fix(self) -> None:
        """
        ATTACK: Two AttributeErrors with different object types but same attribute
        SHOULD collapse (same fix applies). But two AttributeErrors on completely
        different attributes must NOT collapse — wrong fix would fire.
        E.g. fix for 'has no attribute send' ≠ fix for 'has no attribute execute'.
        After redaction both become "'<type>' object has no attribute '<attr>'" — same hash!
        This is the fundamental purity tension: collapse-by-design defeats purity.
        """
        error_send = "AttributeError: 'NoneType' object has no attribute 'send'"
        error_execute = "AttributeError: 'NoneType' object has no attribute 'execute'"
        h_send = fingerprint_hash(error_send)
        h_execute = fingerprint_hash(error_execute)
        # By design these COLLAPSE — documenting the purity gap
        # If they're equal: kill shot (wrong fix for send fires on execute)
        # If they're different: defended
        if h_send == h_execute:
            pytest.xfail(
                "KILL SHOT (by design): AttributeError 'has no attribute <attr>' "
                "collapses ALL attributes to one hash. "
                "'send' fix fires for 'execute' — same fingerprint. "
                "This is an intentional design trade-off but violates 100% purity."
            )

    def test_fp8_cuda_oom_vs_generic_runtime_error(self) -> None:
        """
        ATTACK: 'torch.OutOfMemoryError: CUDA out of memory' (class: cuda-oom)
        vs 'RuntimeError: CUDA out of memory' — same message, different error class.
        _pick_final_line should pick the last error line, which in the CUDA case
        may be 'torch.OutOfMemoryError: ...' (a non-standard class not in _ERROR_TYPE_PATTERNS).
        If both fingerprint to the same hash, OOM fix misfires on RuntimeError.
        """
        cuda_oom = (
            "torch.OutOfMemoryError: CUDA out of memory. "
            "Tried to allocate 20.00 MiB. GPU 0 has a total capacity of 79.19 GiB"
        )
        runtime_oom = "RuntimeError: CUDA out of memory. Tried to allocate 2.44 GiB"
        h_cuda = fingerprint_hash(cuda_oom)
        h_runtime = fingerprint_hash(runtime_oom)
        assert h_cuda != h_runtime, (
            f"KILL SHOT: torch.OutOfMemoryError and RuntimeError CUDA-OOM collide: "
            f"cuda={h_cuda!r} runtime={h_runtime!r}"
        )

    def test_fp9_connection_refused_python_vs_node_vs_go(self) -> None:
        """
        ATTACK: ECONNREFUSED appears in Python (requests), Node, and Go errors.
        The error_type detection fires on 'ECONNREFUSED' for all three and returns
        'ConnectionRefused'. If ecosystem detection also fails, all three map to
        the same hash — wrong fix for Python fires on Go connection errors.
        """
        py_conn = (
            "requests.exceptions.ConnectionError: "
            "HTTPConnectionPool(host='localhost', port=8000): "
            "Max retries exceeded with url: /api/health "
            "(Caused by NewConnectionError('Failed to establish a new connection: "
            "[Errno 111] Connection refused'))"
        )
        node_conn = (
            "Error: connect ECONNREFUSED 127.0.0.1:5432\n"
            "    at TCPConnectWrap.afterConnect [as oncomplete] (node:net:1555:16)"
        )
        go_conn = "dial tcp 127.0.0.1:9200: connect: connection refused"

        h_py = fingerprint_hash(py_conn)
        h_node = fingerprint_hash(node_conn)
        h_go = fingerprint_hash(go_conn)

        assert h_py != h_node, (
            f"KILL SHOT: Python and Node ECONNREFUSED collide: py={h_py!r} node={h_node!r}"
        )
        assert h_py != h_go, (
            f"KILL SHOT: Python and Go ECONNREFUSED collide: py={h_py!r} go={h_go!r}"
        )

    def test_fp10_syntax_error_vs_indentation_error_same_line(self) -> None:
        """
        ATTACK: SyntaxError and IndentationError produce the same final line shape
        when both contain 'expected :'. Fingerprinter must distinguish by error_type.
        If both map to 'SyntaxError' after type detection fails on IndentationError,
        the wrong fix fires.
        """
        syntax_err = (
            'File "handler.py", line 22\n'
            "    if x == 1\n"
            "             ^\n"
            "SyntaxError: expected ':'"
        )
        indent_err = (
            'File "handler.py", line 22\n'
            "    if x == 1\n"
            "             ^\n"
            "IndentationError: expected an indented block"
        )
        h_syntax = fingerprint_hash(syntax_err)
        h_indent = fingerprint_hash(indent_err)
        assert h_syntax != h_indent, (
            f"KILL SHOT: SyntaxError and IndentationError collide: "
            f"syntax={h_syntax!r} indent={h_indent!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. PRIVACY LEAKS — sensitive data surviving redaction
# ─────────────────────────────────────────────────────────────────────────────


class TestPrivacyLeaks:
    """
    Each test asserts that a piece of sensitive data does NOT appear in essence.
    A test PASSES → defended.
    A test FAILS/xfails → confirmed privacy leak kill shot.
    """

    def test_pl1_aws_access_key_bare_unquoted(self) -> None:
        """
        ATTACK: AWS access key appearing bare (unquoted) in error text.
        The dedicated AKIA redactor covers 'AKIA[A-Z0-9]{16}'.
        This should be caught — verify it is.
        """
        key = "AKIAIOSFODNN7EXAMPLE"  # 20 chars, matches AKIA[A-Z0-9]{16}
        text = f"AuthError: AWS credentials invalid: AccessKeyId={key} is not authorized"
        fp = compute_fingerprint(text)
        assert key not in fp.essence, (
            f"KILL SHOT: AWS access key leaked into essence: {fp.essence!r}"
        )

    def test_pl2_bearer_token_in_authorization_header_error(self) -> None:
        """
        ATTACK: Bearer token in a logged Authorization header error.
        Token is not a known prefix (not sk-, ghp_, etc.) — generic quoted
        redactor only fires if quoted AND >=8 chars. Unquoted bearer token leaks.
        """
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
        text = f"AuthError: invalid Authorization: Bearer {token}"
        fp = compute_fingerprint(text)
        # This is likely a kill shot — JWT tokens are not quoted, not a known prefix
        if token in fp.essence:
            pytest.xfail(
                f"KILL SHOT: Unquoted Bearer/JWT token leaked into essence. "
                f"The _TARGETED_REDACTORS have no pattern for 'Bearer <token>' "
                f"and _QUOTED only strips quoted values. essence={fp.essence!r}"
            )
        assert token not in fp.essence

    def test_pl3_real_username_in_path_inside_error_message(self) -> None:
        """
        ATTACK: Username embedded in a Windows-style path in error message text
        (not a Python traceback frame, so _ABS_PATH may not fire).
        e.g. 'C:\\Users\\alice.smith\\AppData\\...'
        """
        text = (
            "PermissionError: [Errno 13] Permission denied: "
            "'C:\\\\Users\\\\alice.smith\\\\AppData\\\\Local\\\\Temp\\\\config.db'"
        )
        fp = compute_fingerprint(text)
        # alice.smith should not appear in essence
        if "alice.smith" in fp.essence:
            pytest.xfail(
                "KILL SHOT: Windows username in path leaked into essence. "
                "_ABS_PATH regex only matches Unix-style absolute paths "
                f"(requires leading /). Windows C:\\ paths bypass it. essence={fp.essence!r}"
            )
        assert "alice.smith" not in fp.essence

    def test_pl4_short_api_key_under_8_chars_in_quotes(self) -> None:
        """
        ATTACK: Short API key (< 8 chars) inside quotes — _QUOTED_SINGLE requires
        8+ chars, so 'abc123' survives into essence verbatim.
        This is a documented xfail in test_fingerprint_hardening.py but we
        test the concrete leak scenario here.
        """
        text = "AuthError: invalid token 'k3y!X9' rejected by server"
        fp = compute_fingerprint(text)
        # 'k3y!X9' is 6 chars — below the 8-char threshold
        if "k3y!X9" in fp.essence:
            pytest.xfail(
                "KILL SHOT: Short secret (6 chars) in single quotes leaked into essence. "
                "_QUOTED_SINGLE pattern requires {8,} characters — tokens under 8 "
                f"chars are never redacted. essence={fp.essence!r}"
            )

    def test_pl5_email_address_unquoted_in_error(self) -> None:
        """
        ATTACK: Email address appearing bare (unquoted) in error message.
        The _TARGETED_REDACTORS include an email redactor pattern. Verify it fires.
        If it doesn't, email leaks into the Darwin export corpus.
        """
        email = "alice.smith@company.com"
        text = f"AuthenticationError: user {email} not found in directory"
        fp = compute_fingerprint(text)
        # Check if the email redactor fires
        redacted = _redact(text)
        if email in redacted:
            pytest.xfail(
                f"KILL SHOT: Email address leaked through _redact(). "
                f"The email pattern `(?<![@\\w])[\\w.+-]+@[\\w-]+\\.[\\w.-]+` "
                f"should match but failed on: {text!r}. "
                f"Redacted form: {redacted!r}"
            )
        assert email not in fp.essence, (
            f"Email leaked into essence: {fp.essence!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. REDOS PATTERNS — inputs designed to cause catastrophic backtracking
# ─────────────────────────────────────────────────────────────────────────────


class TestReDoSV2:
    """
    ReDoS tests for patterns not covered by existing test_security_hardening.py.
    Budget: 500ms per test (generous for CI).
    A PASS means the fingerprinter survived (defended).
    A FAIL means the pattern hung (kill shot — DoS vector).
    """

    _BUDGET = 0.500  # seconds

    def _assert_fast(self, text: str, label: str) -> None:
        start = time.perf_counter()
        compute_fingerprint(text)
        elapsed = time.perf_counter() - start
        assert elapsed < self._BUDGET, (
            f"KILL SHOT (ReDoS): {label} took {elapsed*1000:.1f}ms — "
            "potential catastrophic backtracking"
        )

    def test_redos1_email_pattern_pathological(self) -> None:
        """
        ATTACK: The email regex `(?<![@\\w])[\\w.+-]+@[\\w-]+\\.[\\w.-]+` can
        backtrack catastrophically on a long string of word chars with no '@'
        because \\w.+- allows both \\w and . to match the same chars,
        and the engine tries all combinations before failing.
        Input: 10,000 word chars with no '@' to force full backtrack.
        """
        # Force worst case: many chars matching [\w.+-] with no @ anywhere
        evil = "a" * 5_000 + "." + "b" * 5_000 + " something failed"
        self._assert_fast(evil, "email regex on long wordchar string without @")

    def test_redos2_abs_path_with_spaces_in_segments(self) -> None:
        """
        ATTACK: _ABS_PATH = `(?:/[^\\s:/'\"`<>]+){1,20}/([^/\\s:'\"`<>]+)`
        The outer group allows 1-20 repetitions. An input with exactly 20
        deep path segments followed by a character that prevents matching
        forces the engine to try all 20 sub-combinations on backtrack.
        Input: /a/b/c/...20 segments.../FAIL (ends with space, not a valid filename)
        """
        # 21 segments with no trailing file — forces regex to try and fail at each depth
        evil_path = "/seg" * 21 + " failed: something"
        text = f"FileNotFoundError: {evil_path}"
        self._assert_fast(text, "_ABS_PATH 21-segment path with trailing space")

    def test_redos3_quoted_single_near_threshold(self) -> None:
        """
        ATTACK: _QUOTED_SINGLE = `'([^'<>]{8,})'`
        An input with an unclosed single-quote followed by 10,000 non-quote chars
        forces the engine to try all lengths from 8 to 10,000 before failing.
        Input: 'aaaaa....(no closing quote)....error text
        Classic 'evil regex' pattern: nested quantifier with backtrack on no match.
        """
        # Unclosed quote: engine tries to match {8,} then fails on missing closing quote
        evil = "'" + "a" * 10_000 + " Error: something went wrong"
        self._assert_fast(evil, "_QUOTED_SINGLE unclosed quote + 10k chars")
