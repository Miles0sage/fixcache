"""
test_fingerprint_hardening.py — Adversarial hardening tests for fingerprint.py

Designed to BREAK the fingerprinter. xfail tests document real bugs.
"""

from __future__ import annotations

import re
import time
from typing import Any

import pytest

from lore_memory.fingerprint import (
    _redact,
    _TARGETED_REDACTORS,
    compute_fingerprint,
    fingerprint_hash,
)


# ── 1. Edge inputs ────────────────────────────────────────────────────────────


class TestEdgeInputs:
    def test_empty_string(self) -> None:
        fp = compute_fingerprint("")
        assert fp.hash
        assert len(fp.hash) == 16
        assert fp.error_type == "Unknown"
        assert fp.essence == ""

    def test_whitespace_only(self) -> None:
        fp = compute_fingerprint("   \n\t  \n  ")
        assert fp.hash
        assert len(fp.hash) == 16
        assert fp.error_type == "Unknown"

    def test_single_char(self) -> None:
        fp = compute_fingerprint("x")
        assert len(fp.hash) == 16

    def test_10mb_input(self) -> None:
        """10 MB input must not crash or hang."""
        big = "TypeError: something went wrong\n" * 300_000  # ~10 MB
        start = time.perf_counter()
        fp = compute_fingerprint(big)
        elapsed = time.perf_counter() - start
        assert len(fp.hash) == 16
        assert elapsed < 5.0, f"10 MB input took {elapsed:.2f}s — too slow"

    def test_1000_frame_traceback(self) -> None:
        """Deep traceback must resolve to the final error line."""
        frames = "\n".join(
            f'  File "module_{i}.py", line {i}, in func_{i}' for i in range(1000)
        )
        text = f"Traceback (most recent call last):\n{frames}\nValueError: deep error"
        fp = compute_fingerprint(text)
        assert fp.error_type == "ValueError"
        assert len(fp.hash) == 16

    def test_no_final_error_line(self) -> None:
        """Input with no recognizable error pattern still returns a valid fp."""
        fp = compute_fingerprint("this is just a log line\nand another one")
        assert len(fp.hash) == 16
        assert fp.error_type == "Unknown"


# ── 2. Encoding edge cases ────────────────────────────────────────────────────


class TestEncoding:
    def test_utf8_bom(self) -> None:
        text = "\ufeffModuleNotFoundError: No module named 'foo'"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16

    def test_null_bytes(self) -> None:
        """NULL bytes in input must not crash fingerprinter."""
        text = "TypeError\x00: bad\x00value\x00"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16

    def test_control_chars(self) -> None:
        """Control characters \x01-\x1f must not crash fingerprinter."""
        text = "".join(chr(i) for i in range(1, 32)) + "ValueError: test"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16

    def test_rtl_override_chars(self) -> None:
        """RTL/LTR override (U+202E, U+202D) in input must not break fingerprinter."""
        text = "TypeError\u202e: reversing\u202d text"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16

    def test_zero_width_joiners(self) -> None:
        """Zero-width joiners/non-joiners must not affect hash stability."""
        clean = "ModuleNotFoundError: No module named 'foo'"
        with_zwj = "ModuleNotFoundError\u200d: No module named\u200c 'foo'"
        # They may hash differently — that's fine — but neither must crash
        fp1 = compute_fingerprint(clean)
        fp2 = compute_fingerprint(with_zwj)
        assert len(fp1.hash) == 16
        assert len(fp2.hash) == 16

    def test_homoglyphs(self) -> None:
        """Homoglyph substitution (Cyrillic 'е' for Latin 'e') must not crash."""
        # U+0435 CYRILLIC SMALL LETTER IE looks like 'e'
        text = "Typ\u0435Error: test"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16

    def test_latin1_safe_chars(self) -> None:
        """Latin-1 codepoints (non-UTF8 territory) embedded in a Python str."""
        text = "Error: caf\xe9.py not found"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16

    def test_mixed_unicode_planes(self) -> None:
        """Emoji and high-plane Unicode in error text must not crash."""
        text = "RuntimeError: failed at step \U0001f525 (fire)"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16


# ── 3. ReDoS / catastrophic backtracking ──────────────────────────────────────


class TestReDoS:
    _TIMEOUT_MS = 0.500  # 500 ms budget per test

    def _assert_fast(self, text: str, label: str = "") -> None:
        start = time.perf_counter()
        compute_fingerprint(text)
        elapsed = time.perf_counter() - start
        assert elapsed < self._TIMEOUT_MS, (
            f"ReDoS candidate{' (' + label + ')' if label else ''} "
            f"took {elapsed*1000:.1f}ms — potential catastrophic backtracking"
        )

    @pytest.mark.xfail(reason="bug: _QUOTED regex `(['\"])([^'\"]{8,})\\1` has ReDoS on long quoted strings — 21s on 100k chars")
    def test_long_quoted_string_no_redos(self) -> None:
        """_QUOTED pattern on a very long quoted string must stay fast."""
        long_secret = "a" * 100_000
        text = f"Error: token '{long_secret}' rejected"
        self._assert_fast(text, "_QUOTED long literal")

    @pytest.mark.xfail(reason="bug: _ABS_PATH regex `(?:/[^\\s:'\"`]+/)+([^/\\s:'\"`]+)` has ReDoS on deep paths — 22s on 50k segments")
    def test_abs_path_pattern_long_path(self) -> None:
        """_ABS_PATH with a very deep path must stay fast."""
        deep = "/a" * 50_000 + "/foo.py"
        text = f"FileNotFoundError: {deep}"
        self._assert_fast(text, "_ABS_PATH deep nesting")

    def test_targeted_no_module_long_name(self) -> None:
        """'No module named' pattern on a huge module name must stay fast."""
        big_name = "x" * 100_000
        text = f"ModuleNotFoundError: No module named '{big_name}'"
        self._assert_fast(text, "No module named long name")

    def test_hex_id_pattern_many_hex_chunks(self) -> None:
        """_HEX_ID pattern on a line full of hex-looking tokens must stay fast."""
        text = " ".join(f"0x{'ab12cd34' * 1}" for _ in range(10_000))
        self._assert_fast(text, "_HEX_ID many hex tokens")

    def test_number_pattern_many_numbers(self) -> None:
        """_NUMBER on a line full of large numbers must stay fast."""
        text = " ".join(str(i * 999) for i in range(10_000))
        self._assert_fast(text, "_NUMBER many numbers")

    def test_line_col_pattern_many_colons(self) -> None:
        """_LINE_COL on repeated .py:NNN:NNN segments must stay fast."""
        text = " ".join(f"foo.py:{i}:{i}" for i in range(10_000))
        self._assert_fast(text, "_LINE_COL many segments")

    def test_cannot_import_name_long(self) -> None:
        """'cannot import name' pattern on very long names must stay fast."""
        big = "x" * 100_000
        text = f"ImportError: cannot import name '{big}' from 'some.module'"
        self._assert_fast(text, "cannot import long name")


# ── 4. Injection-shaped text ──────────────────────────────────────────────────


class TestInjectionInput:
    def test_sql_injection_in_error(self) -> None:
        text = "DatabaseError: '; DROP TABLE users; --"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16
        assert "DROP TABLE" not in fp.hash

    def test_shell_injection_in_error(self) -> None:
        text = "CommandError: $(rm -rf /tmp/important)"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16

    def test_format_string_in_error(self) -> None:
        text = "ValueError: %s %s %d %f %n overflow"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16

    def test_regex_metacharacters_in_error(self) -> None:
        text = r"Error: pattern .*+?(){}[]|\ failed"
        fp = compute_fingerprint(text)
        assert len(fp.hash) == 16

    @pytest.mark.xfail(reason="bug: NULL bytes are not stripped before redaction — \\x00 survives into fp.essence")
    def test_null_byte_injection(self) -> None:
        text = "Error: file\x00/etc/passwd access denied"
        fp = compute_fingerprint(text)
        assert "\x00" not in fp.essence
        assert len(fp.hash) == 16


# ── 5. Path traversal ─────────────────────────────────────────────────────────


class TestPathTraversal:
    def test_unix_path_traversal_not_in_top_frame(self) -> None:
        text = 'File "../../../../etc/passwd", line 1'
        fp = compute_fingerprint(text)
        if fp.top_frame is not None:
            assert "/" not in fp.top_frame, (
                f"top_frame leaks path component: {fp.top_frame!r}"
            )

    def test_windows_path_in_top_frame(self) -> None:
        text = 'File "C:\\Windows\\System32\\cmd.exe", line 1\nValueError: x'
        fp = compute_fingerprint(text)
        # top_frame should be a basename only — no backslash directory prefix
        if fp.top_frame is not None:
            assert "/" not in fp.top_frame

    def test_top_frame_is_basename_only_python(self) -> None:
        text = (
            'Traceback (most recent call last):\n'
            '  File "/home/user/deep/nested/path/app.py", line 42\n'
            'ValueError: test'
        )
        fp = compute_fingerprint(text)
        assert fp.top_frame == "app.py"
        assert "/" not in fp.top_frame

    def test_top_frame_is_basename_only_node(self) -> None:
        text = "Error: boom\n    at fn (/home/user/project/src/index.js:10:5)"
        fp = compute_fingerprint(text)
        if fp.top_frame is not None:
            assert "/" not in fp.top_frame

    def test_format_string_path_in_frame(self) -> None:
        text = 'File "/home/user/%s/app.py", line 1\nValueError: x'
        fp = compute_fingerprint(text)
        if fp.top_frame is not None:
            assert "/" not in fp.top_frame


# ── 6. Determinism ────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_same_input_same_hash_1000_runs(self) -> None:
        text = "ModuleNotFoundError: No module named 'pandas'"
        hashes = {fingerprint_hash(text) for _ in range(1000)}
        assert len(hashes) == 1, "Hash is non-deterministic across runs"

    def test_as_dict_is_stable(self) -> None:
        text = "TypeError: 'int' object is not subscriptable"
        d1 = compute_fingerprint(text).as_dict()
        d2 = compute_fingerprint(text).as_dict()
        assert d1 == d2

    def test_as_dict_contains_expected_keys(self) -> None:
        fp = compute_fingerprint("ValueError: bad value")
        d = fp.as_dict()
        for key in ("error_type", "ecosystem", "tool", "essence", "top_frame", "hash"):
            assert key in d


# ── 7. Collision avoidance ────────────────────────────────────────────────────


class TestCollisionAvoidance:
    def test_python_vs_node_module_error_no_collision(self) -> None:
        """Python ModuleNotFoundError vs Node 'Cannot find module' must not collide."""
        py = fingerprint_hash("ModuleNotFoundError: No module named 'lodash'")
        node = fingerprint_hash("Error: Cannot find module 'lodash'")
        assert py != node, (
            "Python and Node module-not-found errors hash identically — "
            "cross-ecosystem collision"
        )

    def test_attribute_error_vs_type_error_no_collision(self) -> None:
        attr = fingerprint_hash("AttributeError: 'NoneType' object has no attribute 'x'")
        typ = fingerprint_hash("TypeError: 'NoneType' object is not subscriptable")
        assert attr != typ

    def test_value_error_vs_key_error_no_collision(self) -> None:
        ve = fingerprint_hash("ValueError: invalid literal")
        ke = fingerprint_hash("KeyError: 'missing_key'")
        assert ve != ke

    def test_distinct_ecosystems_no_collision(self) -> None:
        """Same surface error string in different ecosystems must not collide."""
        rust_err = fingerprint_hash("error: cannot find `foo` in this scope\n  --> src/main.rs:5:3")
        go_err = fingerprint_hash("undefined: foo\ngo build ./...")
        assert rust_err != go_err


# ── 8. Same-shape collapse ────────────────────────────────────────────────────


class TestSameShapeCollapse:
    def test_no_module_named_foo_bar_collapse(self) -> None:
        a = fingerprint_hash("ModuleNotFoundError: No module named 'foo'")
        b = fingerprint_hash("ModuleNotFoundError: No module named 'bar'")
        assert a == b, "Same-shape module errors must collapse to one hash"

    def test_no_module_named_many_names_collapse(self) -> None:
        names = ["sklearn", "pandas", "numpy", "torch", "flask", "django"]
        hashes = [fingerprint_hash(f"ModuleNotFoundError: No module named '{n}'") for n in names]
        assert len(set(hashes)) == 1, "All ModuleNotFoundError variants must collapse"

    def test_cannot_find_module_node_collapse(self) -> None:
        a = fingerprint_hash("Error: Cannot find module 'express'")
        b = fingerprint_hash("Error: Cannot find module 'lodash'")
        assert a == b, "Node 'Cannot find module' variants must collapse"

    def test_attribute_error_different_types_collapse(self) -> None:
        a = fingerprint_hash("AttributeError: 'NoneType' object has no attribute 'split'")
        b = fingerprint_hash("AttributeError: 'int' object has no attribute 'split'")
        assert a == b, "AttributeError with different types must collapse"

    def test_undefined_go_collapse(self) -> None:
        a = fingerprint_hash("undefined: MyStruct")
        b = fingerprint_hash("undefined: AnotherType")
        assert a == b, "Go 'undefined: <name>' variants must collapse"


# ── 9. Privacy invariant ──────────────────────────────────────────────────────


class TestPrivacyInvariant:
    def test_api_key_not_in_essence(self) -> None:
        text = "AuthError: invalid token 'sk-abcd1234efgh5678'"
        fp = compute_fingerprint(text)
        assert "sk-abcd1234efgh5678" not in fp.essence, (
            "API key literal leaked into essence"
        )

    def test_github_token_not_in_essence(self) -> None:
        text = "AuthError: bad credentials 'ghp_abcdefg12345678'"
        fp = compute_fingerprint(text)
        assert "ghp_abcdefg12345678" not in fp.essence, (
            "GitHub token leaked into essence"
        )

    def test_secret_not_in_hash(self) -> None:
        """Secret must not appear as a substring of the 16-char hex hash."""
        secret = "sk-supersecretkey"
        text = f"Error: token '{secret}' is invalid"
        fp = compute_fingerprint(text)
        # hash is hex, so this is trivially true, but verify essence too
        assert secret not in fp.essence

    def test_long_quoted_value_redacted(self) -> None:
        """Any 8+ char quoted string must be redacted in essence."""
        text = "ConfigError: bad value 'verylongsecretvalue'"
        fp = compute_fingerprint(text)
        assert "verylongsecretvalue" not in fp.essence

    @pytest.mark.xfail(reason="bug: short secrets (< 8 chars) in quotes are NOT redacted by _QUOTED")
    def test_short_secret_not_in_essence(self) -> None:
        """Short secrets (< 8 chars) in quotes should be redacted but aren't."""
        text = "AuthError: bad token 'abc123'"
        fp = compute_fingerprint(text)
        assert "abc123" not in fp.essence


# ── 10. Hash format ───────────────────────────────────────────────────────────


class TestHashFormat:
    def test_hash_is_exactly_16_chars(self) -> None:
        texts = [
            "TypeError: x",
            "ModuleNotFoundError: No module named 'foo'",
            "",
            "a",
            "x" * 10_000,
        ]
        for text in texts:
            fp = compute_fingerprint(text)
            assert len(fp.hash) == 16, f"Hash length != 16 for input: {text[:40]!r}"

    def test_hash_is_lowercase_hex(self) -> None:
        texts = [
            "ValueError: bad input",
            "RuntimeError: unexpected",
            "FAILED: test_something",
        ]
        for text in texts:
            fp = compute_fingerprint(text)
            assert re.fullmatch(r"[0-9a-f]{16}", fp.hash), (
                f"Hash {fp.hash!r} is not 16 lowercase hex chars"
            )

    def test_hash_no_uppercase(self) -> None:
        fp = compute_fingerprint("AttributeError: 'str' object has no attribute 'append'")
        assert fp.hash == fp.hash.lower()


# ── 11. Top frame invariant ───────────────────────────────────────────────────


class TestTopFrame:
    def test_top_frame_no_slash(self) -> None:
        text = (
            'Traceback (most recent call last):\n'
            '  File "/very/deep/nested/path/to/module.py", line 10\n'
            'RuntimeError: oops'
        )
        fp = compute_fingerprint(text)
        assert fp.top_frame is not None
        assert "/" not in fp.top_frame, f"top_frame contains slash: {fp.top_frame!r}"

    def test_top_frame_none_for_no_frame(self) -> None:
        fp = compute_fingerprint("ValueError: simple error no traceback")
        assert fp.top_frame is None

    def test_top_frame_is_basename(self) -> None:
        text = 'File "/a/b/c/d/e/f/g.py", line 1\nKeyError: x'
        fp = compute_fingerprint(text)
        assert fp.top_frame == "g.py"

    def test_node_relative_top_frame_no_slash(self) -> None:
        """Node stack with relative path — top_frame must still be basename only."""
        text = "Error: boom\n    at fn (./src/deep/nested/index.js:10:5)"
        fp = compute_fingerprint(text)
        if fp.top_frame is not None:
            assert "/" not in fp.top_frame, f"top_frame leaks relative path: {fp.top_frame!r}"


# ── 12. Redactor reachability ─────────────────────────────────────────────────


class TestRedactorReachability:
    """For every pattern in _TARGETED_REDACTORS, at least one input must trigger it."""

    def _fires(self, pattern: re.Pattern[str], text: str) -> bool:
        return bool(pattern.search(text))

    def test_no_module_named_single_quotes(self) -> None:
        assert "No module named '<mod>'" in _redact("No module named 'foo'")

    def test_no_module_named_double_quotes(self) -> None:
        assert "No module named '<mod>'" in _redact('No module named "foo"')

    def test_cannot_import_from(self) -> None:
        result = _redact("cannot import name 'SomeClass' from 'some.module'")
        assert "cannot import name '<name>' from '<mod>'" in result

    def test_cannot_import_without_from(self) -> None:
        result = _redact("cannot import name 'SomeClass'")
        assert "cannot import name '<name>'" in result

    @pytest.mark.xfail(reason="bug: _QUOTED runs after targeted redactors and re-redacts '<attr>' placeholder, producing garbled output like \"'<type>'<val>'<attr>'\" instead of \"'<type>' object has no attribute '<attr>'\"")
    def test_object_has_no_attribute(self) -> None:
        result = _redact("'NoneType' object has no attribute 'split'")
        assert "'<type>' object has no attribute '<attr>'" in result

    def test_object_not_subscriptable(self) -> None:
        result = _redact("'int' object is not subscriptable")
        assert "'<type>' object is not subscriptable" in result

    def test_object_not_iterable(self) -> None:
        result = _redact("'float' object is not iterable")
        assert "'<type>' object is not iterable" in result

    def test_object_not_callable(self) -> None:
        result = _redact("'str' object is not callable")
        assert "'<type>' object is not callable" in result

    def test_cannot_find_module_node(self) -> None:
        result = _redact("Cannot find module 'express'")
        assert "Cannot find module '<mod>'" in result

    def test_is_not_a_function(self) -> None:
        result = _redact("myFunc is not a function")
        assert "<name> is not a function" in result

    def test_is_not_defined(self) -> None:
        result = _redact("myVar is not defined")
        assert "<name> is not defined" in result

    def test_go_undefined(self) -> None:
        result = _redact("undefined: MyType")
        assert "undefined: <name>" in result

    def test_rust_unused_import(self) -> None:
        result = _redact("unused import: `std::collections::HashMap`")
        assert "unused import: `<name>`" in result

    def test_rust_cannot_find(self) -> None:
        result = _redact("cannot find `MyStruct` in this scope")
        assert "cannot find `<name>` in this scope" in result

    def test_shell_command_not_found(self) -> None:
        result = _redact("python3: command not found")
        assert "<cmd>: command not found" in result

    def test_filesystem_no_such_file(self) -> None:
        result = _redact("/etc/foo.conf: No such file or directory")
        assert "<path>: No such file or directory" in result

    def test_relative_path_pattern(self) -> None:
        result = _redact("./src/main.go:12:9: something failed")
        assert "./<file>" in result

    def test_parent_relative_path_pattern(self) -> None:
        """'../foo.go' — the targeted relative-path pattern incidentally matches ../ too.

        The targeted relative-path pattern starts with dot-slash but the leading dot
        is a regex metachar matching any character including '.', so '../src/main.go'
        is also consumed. The replacement is './<file>', meaning '../src/main.go:12:9'
        becomes '../<file>' — './<file>' is present as a substring.
        This documents the actual behavior: partial canonicalization of ../ paths.
        """
        result = _redact("../src/main.go:12:9: something failed")
        # The pattern DOES fire on ../ paths (regex dot matches the extra dot)
        assert "./<file>" in result


# ── 13. Idempotent redaction ──────────────────────────────────────────────────


class TestIdempotentRedaction:
    """_redact(_redact(x)) == _redact(x) for a diverse sample."""

    _SAMPLES = [
        "ModuleNotFoundError: No module named 'foo'",
        "AttributeError: 'NoneType' object has no attribute 'split'",
        "Cannot find module 'express'",
        "FileNotFoundError: /home/user/project/foo.py",
        "ProcessError at 0xdeadbeef: session abc123def456",
        "Error: token 'sk-supersecretkey123' invalid",
        "TypeError: 'int' object is not subscriptable",
        "undefined: MyStruct",
        "unused import: `std::fmt`",
        "./src/main.go:12:9: undefined reference",
        "python3: command not found",
        "",
        "x",
        "plain text no error",
        "ValueError: expected 1000 got 2000",
    ]

    @pytest.mark.parametrize("sample", _SAMPLES)
    def test_redact_is_idempotent(self, sample: str) -> None:
        once = _redact(sample)
        twice = _redact(once)
        assert once == twice, (
            f"_redact is not idempotent for: {sample!r}\n"
            f"  once:  {once!r}\n"
            f"  twice: {twice!r}"
        )
