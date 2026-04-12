from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.watch import (
    WatchResult,
    _tail,
    classify_and_format,
    run_command,
    watch_command,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(str(tmp_path / "hardening.db"))
    yield s
    s.close()


@pytest.fixture
def py(tmp_path: Path):
    """Factory: write a Python script to tmp_path and return its path."""

    def _make(content: str) -> Path:
        p = tmp_path / "script.py"
        p.write_text(content, encoding="utf-8")
        return p

    return _make


# ── 1. Output volume ──────────────────────────────────────────────────────────


def test_large_stderr_no_unbounded_memory(py: Any) -> None:
    """10 MB stderr must not OOM; _tail ring-buffer keeps captured text ≤ 16 KB."""
    script = py(
        "import sys; sys.stderr.write('X' * (10 * 1024 * 1024)); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    # _tail caps at 16 KB — captured text must fit within that bound
    assert len(captured) <= 16 * 1024
    # Content should be non-empty
    assert "X" in captured


def test_large_stderr_watch_command_exits_cleanly(store: MemoryStore, py: Any) -> None:
    """watch_command must return the child exit code even with huge stderr."""
    script = py(
        "import sys; sys.stderr.write('E' * (12 * 1024 * 1024)); sys.exit(3)"
    )
    code = watch_command(store, [sys.executable, str(script)], json_output=True)
    assert code == 3


# ── 2. Encoding chaos ─────────────────────────────────────────────────────────


@pytest.mark.xfail(
    reason="BUG watch.py:102 — Popen(text=True) uses strict UTF-8; latin-1 bytes raise UnicodeDecodeError"
)
def test_latin1_bytes_in_stderr(py: Any) -> None:
    """latin-1 bytes that are invalid UTF-8 must not crash run_command.

    REAL BUG: watch.py opens stderr with text=True and no errors= handler,
    so any non-UTF-8 byte causes UnicodeDecodeError at lore_memory/watch.py:102
    (the `for line in proc.stderr` loop). Fix: add errors='replace' to Popen.
    """
    script = py(
        "import sys; sys.stderr.buffer.write(b'h\\xe9llo w\\xf6rld'); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert isinstance(captured, str)


@pytest.mark.xfail(
    reason="BUG watch.py:102 — Popen(text=True) uses strict UTF-8; invalid continuation byte raises UnicodeDecodeError"
)
def test_invalid_utf8_bytes_in_stderr(py: Any) -> None:
    """Sequences like \\xc3\\x28 (overlong) must not raise; text mode uses 'replace'.

    REAL BUG: same root cause as test_latin1_bytes_in_stderr — missing errors='replace'.
    """
    script = py(
        "import sys; sys.stderr.buffer.write(b'Hello \\xc3\\x28 World'); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert "Hello" in captured
    assert "World" in captured


@pytest.mark.xfail(
    reason="BUG watch.py:102 — Popen(text=True) uses strict UTF-8; UTF-16 BOM byte 0xff raises UnicodeDecodeError"
)
def test_utf16_bom_bytes_in_stderr(py: Any) -> None:
    """UTF-16 LE BOM + payload — run_command must capture bytes without crash.

    REAL BUG: same root cause as the latin-1 test — Popen lacks errors='replace'.
    """
    utf16_payload = "Hello".encode("utf-16-le")
    bom = b"\xff\xfe"
    raw = (bom + utf16_payload).hex()
    script = py(
        f"import sys; sys.stderr.buffer.write(bytes.fromhex('{raw}')); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert isinstance(captured, str)


@pytest.mark.xfail(reason="cp1252 0x80 byte is invalid UTF-8; text=True Popen may raise on some platforms")
def test_cp1252_euro_byte_in_stderr(py: Any) -> None:
    """0x80 (€ in CP1252) is an illegal lone byte in UTF-8; text=True errors may surface."""
    script = py(
        "import sys; sys.stderr.buffer.write(b'Price: \\x80 EUR'); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert "Price:" in captured


# ── 3. Binary control bytes ───────────────────────────────────────────────────


def test_binary_control_bytes_in_stderr(py: Any) -> None:
    """NUL + full 0x01–0x1f range must be captured without crashing."""
    control_hex = "".join(f"\\x{i:02x}" for i in range(0, 32))
    script = py(
        f"import sys; sys.stderr.buffer.write(b'{control_hex}End'); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert isinstance(captured, str)


# ── 4. ANSI + progress bars ───────────────────────────────────────────────────


def test_ansi_color_codes_captured(py: Any) -> None:
    """ANSI escape codes must pass through capture unchanged."""
    script = py(
        r"import sys; sys.stderr.buffer.write(b'\x1b[31mError\x1b[0m\n'); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert "Error" in captured


def test_ansi_progress_bar_carriage_returns(py: Any) -> None:
    """Carriage returns in stderr must not crash or corrupt capture."""
    script = py(
        "import sys\n"
        "for i in range(5):\n"
        "    sys.stderr.write(f'\\rProgress {i*20}%')\n"
        "    sys.stderr.flush()\n"
        "sys.stderr.write('\\nDone\\n')\n"
        "sys.exit(1)\n"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert "Done" in captured


def test_backspace_chars_in_stderr(py: Any) -> None:
    """Backspace bytes (0x08) should be captured, not interpreted."""
    script = py(
        r"import sys; sys.stderr.write('AB\x08C\n'); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert isinstance(captured, str)


# ── 5. Signal handling ────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
def test_sigkill_mid_stderr(py: Any) -> None:
    """SIGKILL mid-output: run_command must return negative/137 exit, not raise."""
    script = py(
        "import sys, os, signal, time\n"
        "sys.stderr.write('before-kill\\n'); sys.stderr.flush()\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
        "sys.stderr.write('after-kill\\n')\n"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code in (-9, 137)
    assert "before-kill" in captured
    assert "after-kill" not in captured


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals only")
def test_sigterm_mid_stderr(py: Any) -> None:
    """SIGTERM mid-output: exit code must be -15 or 143."""
    script = py(
        "import sys, os, signal, time\n"
        "sys.stderr.write('before-term\\n'); sys.stderr.flush()\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
        "time.sleep(2)\n"
        "sys.stderr.write('after-term\\n')\n"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code in (-15, 143)
    assert "before-term" in captured


@pytest.mark.skipif(sys.platform == "win32", reason="SIGHUP is POSIX-only")
def test_sighup_mid_stderr(py: Any) -> None:
    """SIGHUP mid-output: exit code must be -1 or 129."""
    script = py(
        "import sys, os, signal, time\n"
        "sys.stderr.write('before-hup\\n'); sys.stderr.flush()\n"
        "os.kill(os.getpid(), signal.SIGHUP)\n"
        "time.sleep(2)\n"
        "sys.stderr.write('after-hup\\n')\n"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code in (-1, 129)
    assert "before-hup" in captured


# ── 6. Timeout ────────────────────────────────────────────────────────────────


@pytest.mark.xfail(reason="run_command has no timeout parameter; infinite subprocess will hang")
def test_subprocess_never_exits_timeout(py: Any) -> None:
    """A subprocess that never exits should be killed after a short timeout.

    run_command() does NOT currently support a timeout= parameter, so this
    test documents the missing feature and is marked xfail. If timeout support
    is added, this test should pass.
    """
    script = py(
        "import sys, time\n"
        "sys.stderr.write('start\\n'); sys.stderr.flush()\n"
        "while True: time.sleep(1)\n"
    )
    # This call will hang indefinitely without timeout support — hence xfail.
    exit_code, captured = run_command(  # type: ignore[call-arg]
        [sys.executable, str(script)], tee=False, timeout=1
    )
    assert exit_code != 0
    assert "start" in captured


# ── 7. Exit code matrix ───────────────────────────────────────────────────────


def test_exit_code_0_with_warnings(py: Any) -> None:
    """Exit 0 with warning on stderr must not trigger classify_and_format."""
    script = py(
        "import sys; sys.stderr.write('Warning: something happened\\n'); sys.exit(0)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 0
    assert "Warning" in captured


def test_exit_code_1_empty_stderr(store: MemoryStore, py: Any) -> None:
    """Exit 1 + empty stderr: WatchResult fingerprint_hash must be None."""
    script = py("import sys; sys.exit(1)")
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert captured.strip() == ""
    result = classify_and_format(store, captured)
    assert result.fingerprint_hash is None


def test_exit_code_127_command_not_found(store: MemoryStore) -> None:
    """Non-existent binary must raise FileNotFoundError which watch_command maps to 127."""
    code = watch_command(store, ["__nonexistent_binary_xyz__"], json_output=False)
    assert code == 127


def test_exit_code_137_sigkill_via_python() -> None:
    """Subprocess killed with SIGKILL returns 137 (or -9) to the parent."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
        stderr=subprocess.PIPE,
    )
    assert result.returncode in (-9, 137)


def test_exit_code_143_sigterm_via_python() -> None:
    """Subprocess killed with SIGTERM returns 143 (or -15) to the parent."""
    result = subprocess.run(
        [sys.executable, "-c",
         "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
        stderr=subprocess.PIPE,
    )
    assert result.returncode in (-15, 143)


# ── 8. Shell-metacharacter argv — no injection ───────────────────────────────


def test_semicolon_no_shell_injection(py: Any) -> None:
    """Semicolon in argv must be passed as literal arg — the script should NOT
    produce a second stderr line from executing 'echo INJECTED'."""
    evil = "; echo INJECTED >&2"
    script = py(
        # Print argv[1] literally; a shell-injection would add a *second* line
        "import sys; sys.stderr.write('ARG_ECHO:' + sys.argv[1] + '\\n'); sys.exit(0)"
    )
    exit_code, captured = run_command(
        [sys.executable, str(script), evil], tee=False
    )
    assert exit_code == 0
    # The word INJECTED appears only as part of the echoed literal arg, never
    # on its own line as if the shell had executed the injection.
    lines = captured.splitlines()
    assert len(lines) == 1, f"Expected 1 line, got {len(lines)}: {lines!r}"
    assert lines[0].startswith("ARG_ECHO:")


def test_subshell_dollar_paren_no_injection(py: Any) -> None:
    """$() substitution in argv must not be expanded (shell=False)."""
    evil = "$(id)"
    script = py(
        "import sys; sys.stderr.write(f'arg={sys.argv[1]}\\n'); sys.exit(0)"
    )
    exit_code, captured = run_command(
        [sys.executable, str(script), evil], tee=False
    )
    assert exit_code == 0
    assert "$(id)" in captured
    assert "uid=" not in captured


def test_pipe_and_ampersand_no_injection(py: Any) -> None:
    """| and && in argv must be literal strings, not shell operators."""
    evil = "foo | cat && echo PWNED"
    script = py(
        "import sys; sys.stderr.write('ARG_ECHO:' + sys.argv[1] + '\\n'); sys.exit(0)"
    )
    exit_code, captured = run_command(
        [sys.executable, str(script), evil], tee=False
    )
    assert exit_code == 0
    # Only one line: the echoed argument. If shell injection ran, there would be
    # an extra line from `echo PWNED`.
    lines = captured.splitlines()
    assert len(lines) == 1, f"Expected 1 line, got {len(lines)}: {lines!r}"
    assert lines[0].startswith("ARG_ECHO:")
    assert "foo | cat" in lines[0]


# ── 9. Unicode argv ───────────────────────────────────────────────────────────


def test_cjk_argv(py: Any) -> None:
    """CJK characters in argv must round-trip through subprocess correctly."""
    cjk = "你好世界"
    script = py(
        "import sys; sys.stderr.write(f'got={sys.argv[1]}\\n'); sys.exit(0)"
    )
    exit_code, captured = run_command(
        [sys.executable, str(script), cjk], tee=False
    )
    assert exit_code == 0
    assert cjk in captured


def test_emoji_argv(py: Any) -> None:
    """Emoji in argv must pass through without corruption."""
    emoji = "fire=🔥 thumbs=👍"
    script = py(
        "import sys; sys.stderr.write(f'got={sys.argv[1]}\\n'); sys.exit(0)"
    )
    exit_code, captured = run_command(
        [sys.executable, str(script), emoji], tee=False
    )
    assert exit_code == 0
    assert "🔥" in captured


def test_rtl_arabic_argv(py: Any) -> None:
    """RTL Arabic text in argv must pass through without corruption."""
    rtl = "مرحبا بالعالم"
    script = py(
        "import sys; sys.stderr.write(f'got={sys.argv[1]}\\n'); sys.exit(0)"
    )
    exit_code, captured = run_command(
        [sys.executable, str(script), rtl], tee=False
    )
    assert exit_code == 0
    assert rtl in captured


# ── 10. Empty stderr on nonzero exit — no false match ────────────────────────


def test_empty_stderr_no_false_fingerprint(store: MemoryStore, py: Any) -> None:
    """Exit 42 with zero stderr bytes must yield fingerprint_hash=None."""
    script = py("import sys; sys.exit(42)")
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 42
    result = classify_and_format(store, captured)
    assert result.fingerprint_hash is None
    assert result.suggestions == []


# ── 11. Partial line — no trailing newline ────────────────────────────────────


def test_partial_line_no_trailing_newline(py: Any) -> None:
    """stderr that ends mid-line (no \\n) must be captured intact."""
    script = py(
        "import sys; sys.stderr.write('partial line no newline'); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert "partial line no newline" in captured


# ── 12. WatchResult.to_dict always produces JSON-serialisable output ──────────


def test_watchresult_to_dict_valid_json(store: MemoryStore) -> None:
    """to_dict() must always yield a structure serialisable by json.dumps."""
    result = classify_and_format(
        store, "ModuleNotFoundError: No module named 'requests'"
    )
    d = result.to_dict()
    serialised = json.dumps(d)
    parsed = json.loads(serialised)
    assert "exit_code" in parsed
    assert "fingerprint_hash" in parsed
    assert "suggestions" in parsed


def test_watchresult_to_dict_with_binary_like_text(store: MemoryStore) -> None:
    """to_dict() must not raise even when stderr_tail contains replacement chars."""
    # Simulate text that came through errors='replace'
    weird_text = "Error \ufffd\ufffd boom"
    result = classify_and_format(store, weird_text)
    d = result.to_dict()
    serialised = json.dumps(d)
    assert json.loads(serialised)


def test_watchresult_to_dict_large_suggestion_list(store: MemoryStore) -> None:
    """to_dict() must handle WatchResult with empty suggestions list cleanly."""
    wr = WatchResult(
        exit_code=1,
        stderr_tail="some error",
        fingerprint_hash="abc123",
        suggestions=[],
    )
    d = wr.to_dict()
    assert json.dumps(d)
    assert d["suggestions"] == []


# ── 13. Mixed encoding chaos ─────────────────────────────────────────────────


@pytest.mark.xfail(
    reason="BUG watch.py:102 — Popen(text=True) uses strict UTF-8; mixed non-UTF-8 bytes raise UnicodeDecodeError"
)
def test_mixed_encoding_stderr_captured(py: Any) -> None:
    """Mix of valid UTF-8, latin-1 escapes, and ANSI must not crash capture.

    REAL BUG: same root cause as the other encoding tests — Popen lacks errors='replace'.
    """
    # valid UTF-8 smiley + invalid lone latin-1 byte + ANSI reset
    payload_hex = (
        "Hello \xe2\x98\xba"  # UTF-8 smiley
        " bad:\xe9"           # lone latin-1 byte (invalid UTF-8)
        " \x1b[0m end"        # ANSI reset
    ).encode("latin-1").hex()
    script = py(
        f"import sys; sys.stderr.buffer.write(bytes.fromhex('{payload_hex}')); sys.exit(1)"
    )
    exit_code, captured = run_command([sys.executable, str(script)], tee=False)
    assert exit_code == 1
    assert isinstance(captured, str)
    assert "Hello" in captured
    assert "end" in captured
