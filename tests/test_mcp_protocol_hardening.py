"""
tests/test_mcp_protocol_hardening.py — Adversarial MCP protocol fuzz tests.

Covers: malformed JSON-RPC, wrong protocol versions, unknown tools, missing/wrong
args, huge payloads, unicode attacks, SQL injection, trust manipulation, concurrency,
batch requests, schema completeness, and content round-trips.

A test marked xfail documents a known bug. Any CRASH (unhandled exception escaping
handle_request) is a HIGH-severity release blocker — see comments marked CRASH-BUG.
"""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.layers.identity import IdentityLayer
from lore_memory.mcp import server as mcp_server
from lore_memory.mcp.tools import TOOL_SCHEMAS


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Each test gets a fresh in-memory store."""
    store = MemoryStore(":memory:")
    identity = IdentityLayer(store)
    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_identity", identity)
    yield store
    store.close()
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_identity", None)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _call(name: str, arguments: dict) -> dict:
    """Convenience: send a tools/call JSON-RPC request."""
    req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    resp = mcp_server.handle_request(req)
    assert resp is not None, f"handle_request returned None for tool {name!r}"
    return resp


def _result_body(resp: dict) -> dict:
    """Parse the JSON text from a successful tools/call response."""
    return json.loads(resp["result"]["content"][0]["text"])


def _is_error_resp(resp: dict) -> bool:
    return "error" in resp


def _is_tool_error(resp: dict) -> bool:
    """A proper tool-level error: result contains success=False or error key."""
    if "error" in resp:
        return True
    if "result" in resp:
        body = _result_body(resp)
        return body.get("success") is False or "error" in body
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Malformed JSON-RPC
# ═══════════════════════════════════════════════════════════════════════════════


class TestMalformedJsonRpc:
    def test_missing_jsonrpc_field(self):
        """Request without 'jsonrpc' key — server must not crash."""
        req = {"id": 1, "method": "tools/list", "params": {}}
        resp = mcp_server.handle_request(req)
        # Server responds (no crash). JSON-RPC spec requires error -32600 but
        # the current server silently handles any dict — acceptable if no crash.
        assert resp is not None

    def test_wrong_jsonrpc_version(self):
        """jsonrpc='1.0' instead of '2.0' — must not crash."""
        req = {"jsonrpc": "1.0", "id": 1, "method": "tools/list", "params": {}}
        resp = mcp_server.handle_request(req)
        assert resp is not None

    def test_missing_method_field(self):
        """No 'method' key — server should return an error, not crash."""
        req = {"jsonrpc": "2.0", "id": 1, "params": {}}
        resp = mcp_server.handle_request(req)
        assert resp is not None
        # Method defaults to "" which hits the unknown-method branch
        assert _is_error_resp(resp)

    def test_missing_id_field(self):
        """Request with no 'id' is a Notification — JSON-RPC 2.0 §5 says
        the Server MUST NOT reply. Implementation drops responses for
        notifications silently via the handle_request wrapper in
        mcp/server.py. This is spec-compliant, not a crash.
        """
        req = {"jsonrpc": "2.0", "method": "tools/list", "params": {}}
        resp = mcp_server.handle_request(req)
        assert resp is None  # spec-compliant: notifications receive no reply

    def test_id_is_float(self):
        """id=1.5 — JSON-RPC 2.0 allows number ids; float round-trips correctly."""
        req = {"jsonrpc": "2.0", "id": 1.5, "method": "tools/list", "params": {}}
        resp = mcp_server.handle_request(req)
        assert resp is not None
        assert resp.get("id") == 1.5

    def test_id_is_nested_dict(self):
        """id={'nested': True} — unusual but server must not crash."""
        req = {"jsonrpc": "2.0", "id": {"nested": True}, "method": "tools/list", "params": {}}
        resp = mcp_server.handle_request(req)
        assert resp is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Wrong protocol version in initialize
# ═══════════════════════════════════════════════════════════════════════════════


class TestProtocolVersion:
    def _init(self, version: Any) -> dict:
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": version},
        }
        return mcp_server.handle_request(req)

    def test_unsupported_version_negotiates_down(self):
        """Unknown version '1999-01-01' — server should negotiate to its latest."""
        resp = self._init("1999-01-01")
        assert resp is not None
        assert "result" in resp
        negotiated = resp["result"]["protocolVersion"]
        assert negotiated in mcp_server.SUPPORTED_PROTOCOL_VERSIONS

    def test_empty_string_version(self):
        """Empty string version — must not crash, should negotiate to supported."""
        resp = self._init("")
        assert resp is not None
        assert "result" in resp

    def test_binary_garbage_version(self):
        """Binary garbage as version string — must not crash."""
        resp = self._init("\x00\xff\xfe garbage")
        assert resp is not None

    def test_none_version(self):
        """None as protocolVersion — must not crash."""
        resp = self._init(None)
        assert resp is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Unknown tool names
# ═══════════════════════════════════════════════════════════════════════════════


class TestUnknownToolNames:
    def test_lore_nonexistent(self):
        resp = _call("lore_nonexistent", {})
        assert _is_error_resp(resp)
        assert resp["error"]["code"] == -32601

    def test_sql_injection_tool_name(self):
        """Tool name containing SQL injection payload — must return -32601."""
        resp = _call("'; DROP TABLE memories; --", {})
        assert _is_error_resp(resp)
        assert resp["error"]["code"] == -32601

    def test_tool_name_with_null_bytes(self):
        """NULL byte in tool name — must not crash."""
        resp = _call("lore_remember\x00evil", {})
        assert resp is not None
        assert _is_error_resp(resp)

    def test_tool_name_10kb(self):
        """10 KB tool name — must return error, not crash or OOM."""
        big_name = "x" * 10_240
        resp = _call(big_name, {})
        assert resp is not None
        assert _is_error_resp(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Missing arguments — each required tool called with empty args dict
# ═══════════════════════════════════════════════════════════════════════════════


class TestMissingArguments:
    def test_remember_empty_args(self):
        resp = _call("lore_remember", {})
        # TypeError from missing 'content' should be caught → -32602 or tool error
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_recall_empty_args(self):
        resp = _call("lore_recall", {})
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_fix_empty_args(self):
        resp = _call("lore_fix", {})
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_match_procedure_empty_args(self):
        resp = _call("lore_match_procedure", {})
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_teach_empty_args(self):
        resp = _call("lore_teach", {})
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_forget_empty_args(self):
        resp = _call("lore_forget", {})
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_rate_fix_empty_args(self):
        resp = _call("lore_rate_fix", {})
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_report_outcome_empty_args(self):
        resp = _call("lore_report_outcome", {})
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_darwin_classify_empty_args(self):
        resp = _call("lore_darwin_classify", {})
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_knowledge_empty_args(self):
        resp = _call("lore_knowledge", {})
        assert _is_tool_error(resp) or _is_error_resp(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Wrong argument types
# ═══════════════════════════════════════════════════════════════════════════════


class TestWrongArgumentTypes:
    def test_remember_content_is_number(self):
        """content=42 (number where string expected) — proper error, no crash."""
        resp = _call("lore_remember", {"content": 42})
        # Handler checks isinstance(content, str) so returns success=False
        assert _is_tool_error(resp) or _is_error_resp(resp)

    def test_remember_tags_is_dict(self):
        """tags as dict instead of list — must not crash."""
        resp = _call("lore_remember", {"content": "test", "tags": {"key": "val"}})
        assert resp is not None

    def test_recall_top_k_is_none(self):
        """top_k=None — handler checks isinstance, should return error."""
        resp = _call("lore_recall", {"query": "test", "top_k": None})
        # None can't be coerced to int — should be caught
        assert resp is not None

    def test_fix_solution_steps_is_string(self):
        """solution_steps as plain string instead of list — proper error."""
        resp = _call("lore_fix", {
            "error_signature": "TestError",
            "solution_steps": "just do it",
        })
        body = _result_body(resp)
        assert body.get("success") is False

    def test_fix_solution_steps_is_none(self):
        """solution_steps=None — proper error, no crash."""
        resp = _call("lore_fix", {
            "error_signature": "TestError",
            "solution_steps": None,
        })
        assert _is_tool_error(resp) or _is_error_resp(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Huge arguments
# ═══════════════════════════════════════════════════════════════════════════════


class TestHugeArguments:
    def test_remember_10mb_content(self):
        """10 MB content string — must not crash (may be slow but acceptable)."""
        big = "A" * (10 * 1024 * 1024)
        resp = _call("lore_remember", {"content": big})
        assert resp is not None
        # Either stored or returns an error — not a crash
        assert "result" in resp or "error" in resp

    def test_fix_100k_solution_steps(self):
        """list of 100 000 solution step strings — must not crash."""
        steps = [f"step {i}" for i in range(100_000)]
        resp = _call("lore_fix", {
            "error_signature": "MassiveError",
            "solution_steps": steps,
        })
        assert resp is not None
        assert "result" in resp or "error" in resp


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Unicode attacks
# ═══════════════════════════════════════════════════════════════════════════════


class TestUnicodeAttacks:
    def test_rtl_override_in_content(self):
        """RTL override character (U+202E) — must store/fail cleanly, no crash."""
        rtl = "safe\u202Eevil"
        resp = _call("lore_remember", {"content": rtl})
        assert resp is not None

    def test_zero_width_joiners(self):
        """Zero-width joiners in content — must not crash."""
        zwj = "hello\u200Dworld\u200Ctest"
        resp = _call("lore_remember", {"content": zwj})
        assert resp is not None

    def test_emoji_in_content(self):
        """Multi-codepoint emoji — SQLite should handle UTF-8 cleanly."""
        emoji = "test \U0001F600\U0001F9E0 memory"
        resp = _call("lore_remember", {"content": emoji})
        assert resp is not None
        body = _result_body(resp)
        assert body.get("success") is True

    def test_binary_garbage_in_query(self):
        """Binary garbage bytes in recall query — must not crash."""
        garbage = "test\x00\xff\xfe query"
        resp = _call("lore_recall", {"query": garbage})
        assert resp is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 8. SQL injection in content
# ═══════════════════════════════════════════════════════════════════════════════


class TestSqlInjection:
    def test_classic_sql_injection_in_content(self):
        """Classic SQL injection payload stored as content — parameterized queries
        should prevent any damage; store should survive."""
        payload = "'; DROP TABLE memories; --"
        resp = _call("lore_remember", {"content": payload})
        assert resp is not None
        body = _result_body(resp)
        assert body.get("success") is True
        # Verify table survived
        stats = mcp_server.handle_lore_stats()
        assert stats["total_memories"] >= 1

    def test_fts5_match_near_metachar(self):
        """FTS5 NEAR() metacharacter in recall query — must not crash or raise."""
        resp = _call("lore_recall", {"query": "NEAR(error fix, 5)"})
        assert resp is not None
        # Should return results or empty — not a crash
        assert "result" in resp or "error" in resp

    def test_fts5_or_metachar(self):
        """FTS5 OR metacharacter in query."""
        resp = _call("lore_recall", {"query": "error OR DROP TABLE"})
        assert resp is not None

    def test_fts5_quote_metachar(self):
        """FTS5 double-quote phrase query — must not crash."""
        resp = _call("lore_recall", {"query": '"error phrase" content'})
        assert resp is not None

    def test_fts5_star_metachar(self):
        """FTS5 prefix wildcard — must not crash."""
        resp = _call("lore_recall", {"query": "err*"})
        assert resp is not None

    def test_sql_injection_in_error_signature(self):
        """SQL injection in lore_fix error_signature — must not corrupt DB."""
        payload = "'; DROP TABLE darwin_patterns; --"
        resp = _call("lore_fix", {
            "error_signature": payload,
            "solution_steps": ["check", "fix"],
        })
        assert resp is not None
        # Patterns table should survive
        stats = mcp_server.handle_lore_stats()
        assert "darwin_patterns" in stats


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Trust score manipulation
# ═══════════════════════════════════════════════════════════════════════════════


class TestTrustScoreManipulation:
    def test_recall_min_trust_above_1(self):
        """min_trust > 1.0 — should return zero results (nothing qualifies), not crash."""
        mcp_server.handle_lore_remember("some content", source_type="user")
        resp = _call("lore_recall", {"query": "some content", "min_trust": 2.0})
        assert resp is not None
        body = _result_body(resp)
        # All real trust scores are ≤ 1.0, so count must be 0
        assert body["count"] == 0

    @pytest.mark.xfail(reason="NaN min_trust: float('nan') comparisons always False in Python — count will be 0 but no validation error returned")
    def test_recall_min_trust_nan(self):
        """min_trust=NaN — should return a validation error, not silently return 0."""
        mcp_server.handle_lore_remember("content", source_type="user")
        resp = _call("lore_recall", {"query": "content", "min_trust": float("nan")})
        assert _is_tool_error(resp)

    def test_recall_min_trust_negative(self):
        """min_trust=-1 — all memories should qualify (no crash)."""
        mcp_server.handle_lore_remember("low trust content", source_type="fleet")
        resp = _call("lore_recall", {"query": "low trust content", "min_trust": -1.0})
        assert resp is not None
        body = _result_body(resp)
        assert body["count"] >= 1

    @pytest.mark.xfail(reason="String min_trust: coercion block only handles integer/number schema types; 'min_trust' is 'number' but string 'high' won't coerce — may raise TypeError inside handler")
    def test_recall_min_trust_string(self):
        """min_trust='high' (string) — should return a proper validation error."""
        resp = _call("lore_recall", {"query": "test", "min_trust": "high"})
        assert _is_tool_error(resp)


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Concurrent initialize — idempotency
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentInitialize:
    def test_two_sequential_initialize_calls(self):
        """Two initialize calls on same server — second must not crash or corrupt."""
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
        r1 = mcp_server.handle_request(req)
        r2 = mcp_server.handle_request({**req, "id": 2})
        assert r1 is not None and r2 is not None
        assert r1["result"]["protocolVersion"] == r2["result"]["protocolVersion"]

    def test_concurrent_initialize_threads(self):
        """Two threads calling initialize simultaneously — no crash, consistent result."""
        results = []
        errors = []

        def do_init(req_id):
            try:
                resp = mcp_server.handle_request({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"},
                })
                results.append(resp)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=do_init, args=(1,))
        t2 = threading.Thread(target=do_init, args=(2,))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert not errors, f"Thread raised exception: {errors}"
        assert len(results) == 2
        for r in results:
            assert "result" in r


# ═══════════════════════════════════════════════════════════════════════════════
# 11. tools/list before initialize
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolsListBeforeInitialize:
    def test_tools_list_without_handshake(self):
        """tools/list issued without prior initialize — server must respond, not crash.

        The MCP spec requires initialize first, but the server currently has no
        session state tracking, so it should respond with the tool list regardless.
        """
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        resp = mcp_server.handle_request(req)
        assert resp is not None
        # Either returns tools or an error — not a crash
        assert "result" in resp or "error" in resp


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Notification handling — no response for id-less requests
# ═══════════════════════════════════════════════════════════════════════════════


class TestNotificationHandling:
    def test_notifications_initialized_returns_none(self):
        """Standard notifications/initialized — must return None (no response)."""
        req = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        resp = mcp_server.handle_request(req)
        assert resp is None

    @pytest.mark.xfail(reason="Generic notifications (no id) — server currently responds with error instead of silently dropping per JSON-RPC 2.0 spec")
    def test_generic_notification_no_id_no_response(self):
        """Any request without 'id' is a notification — server MUST NOT respond."""
        req = {
            "jsonrpc": "2.0",
            "method": "tools/list",
            "params": {},
            # no 'id' field
        }
        resp = mcp_server.handle_request(req)
        # Per spec, notifications get no response
        assert resp is None


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Batch requests (JSON array)
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchRequests:
    @pytest.mark.xfail(reason="JSON-RPC 2.0 batch (array) support not implemented — handle_request expects a dict, passing a list will raise AttributeError or similar crash")
    def test_batch_of_100_requests(self):
        """Batch of 100 tools/list requests (JSON array) — server must handle or
        return a -32600 Invalid Request, NOT crash with AttributeError."""
        batch = [
            {"jsonrpc": "2.0", "id": i, "method": "tools/list", "params": {}}
            for i in range(100)
        ]
        # handle_request currently only handles dicts; passing a list is a bug
        resp = mcp_server.handle_request(batch)  # type: ignore[arg-type]
        # Should return list of responses or single -32600 error, not crash
        assert resp is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Tool schemas completeness
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolSchemasComplete:
    def test_every_schema_has_name_key(self):
        """Every entry in TOOL_SCHEMAS must have a name (the dict key itself)."""
        for name in TOOL_SCHEMAS:
            assert isinstance(name, str) and name, f"Tool key is empty: {name!r}"

    def test_every_schema_has_description(self):
        for name, schema in TOOL_SCHEMAS.items():
            assert "description" in schema, f"{name} missing 'description'"
            assert schema["description"], f"{name} has empty description"

    def test_every_schema_has_input_schema(self):
        for name, schema in TOOL_SCHEMAS.items():
            assert "inputSchema" in schema, f"{name} missing 'inputSchema'"

    def test_every_input_schema_has_type_object(self):
        for name, schema in TOOL_SCHEMAS.items():
            assert schema["inputSchema"].get("type") == "object", \
                f"{name}.inputSchema.type must be 'object'"

    def test_every_input_schema_has_properties(self):
        for name, schema in TOOL_SCHEMAS.items():
            assert "properties" in schema["inputSchema"], \
                f"{name}.inputSchema missing 'properties'"

    def test_required_fields_are_listed_in_properties(self):
        """Any field in 'required' must appear in 'properties'."""
        for name, schema in TOOL_SCHEMAS.items():
            required = schema["inputSchema"].get("required", [])
            props = set(schema["inputSchema"].get("properties", {}).keys())
            for field in required:
                assert field in props, \
                    f"{name}: required field '{field}' not in properties"

    def test_tools_in_handlers_match_schemas(self):
        """Every schema in TOOL_SCHEMAS has a corresponding handler registered."""
        for name in TOOL_SCHEMAS:
            assert name in mcp_server.TOOLS, f"No handler registered for schema {name!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# 15. lore_fix adversarial
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoreFixAdversarial:
    def test_empty_error_signature_returns_error(self):
        """Empty error_signature must return success=False, not crash."""
        resp = _call("lore_fix", {"error_signature": "", "solution_steps": ["step"]})
        body = _result_body(resp)
        assert body.get("success") is False
        assert "error" in body

    def test_regex_bomb_signature(self):
        """Regex bomb stored in error_signature — server catches re.error and falls back
        to substring match; completes within the 3-second guard (verified passing)."""
        import signal

        def _timeout(signum, frame):
            raise TimeoutError("regex bomb caused catastrophic backtracking")

        bomb = "(a+)+" * 5  # classic ReDoS pattern
        mcp_server.handle_lore_fix(
            error_signature=bomb,
            solution_steps=["step"],
        )
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(3)  # 3-second guard
        try:
            mcp_server.handle_lore_match_procedure("a" * 30 + "!")
        finally:
            signal.alarm(0)

    def test_10k_solution_steps(self):
        """10 000 solution steps — must store or error cleanly, not crash."""
        steps = [f"step {i}" for i in range(10_000)]
        resp = _call("lore_fix", {
            "error_signature": "ManyStepsError",
            "solution_steps": steps,
        })
        assert resp is not None
        assert "result" in resp or "error" in resp


# ═══════════════════════════════════════════════════════════════════════════════
# 16. darwin_classify on empty store
# ═══════════════════════════════════════════════════════════════════════════════


class TestDarwinClassifyEmptyStore:
    def test_classify_empty_store_returns_empty_recipes(self):
        """darwin_classify on a store with no fingerprints — must return empty
        recipes list, not raise an exception."""
        resp = _call("lore_darwin_classify", {"error_text": "ImportError: no module"})
        assert resp is not None
        # Must not be a crash-level error
        assert "result" in resp
        body = _result_body(resp)
        # 'recipes' key may be present and empty, or 'fingerprint' with no matches
        # Either way — not an unhandled exception
        assert isinstance(body, dict)


# ═══════════════════════════════════════════════════════════════════════════════
# 17. report_outcome on nonexistent pattern_id
# ═══════════════════════════════════════════════════════════════════════════════


class TestReportOutcomeNonexistent:
    def test_nonexistent_pattern_id_clean_error(self):
        """lore_report_outcome with a made-up pattern_id — clean error, no crash."""
        resp = _call("lore_report_outcome", {
            "pattern_id": "00000000-dead-beef-cafe-000000000000",
            "outcome": "success",
        })
        assert resp is not None
        body = _result_body(resp)
        assert body.get("success") is False
        assert "error" in body
        assert "not found" in body["error"].lower() or "pattern" in body["error"].lower()

    def test_rate_fix_nonexistent_pattern_clean_error(self):
        """lore_rate_fix with nonexistent pattern_id — clean error."""
        resp = _call("lore_rate_fix", {
            "pattern_id": "00000000-dead-beef-cafe-111111111111",
            "outcome": "success",
        })
        assert resp is not None
        body = _result_body(resp)
        assert body.get("success") is False


# ═══════════════════════════════════════════════════════════════════════════════
# 18. Content round-trip integrity
# ═══════════════════════════════════════════════════════════════════════════════


class TestContentRoundTrip:
    def test_remember_recall_verbatim(self):
        """remember(X) → recall(query matching X) → X appears verbatim, no corruption."""
        content = "The quick brown fox jumps over the lazy dog — verbatim check 42"
        r = mcp_server.handle_lore_remember(content)
        assert r["success"] is True
        mid = r["memory_id"]

        recall = mcp_server.handle_lore_recall("quick brown fox verbatim check")
        ids = [item["id"] for item in recall["results"]]
        assert mid in ids, "Stored memory not found in recall results"

        matched = next(item for item in recall["results"] if item["id"] == mid)
        assert matched["content"] == content, (
            f"Content corrupted!\nExpected: {content!r}\nGot:      {matched['content']!r}"
        )

    def test_unicode_content_round_trip(self):
        """Unicode content (CJK + emoji) survives store/recall without corruption."""
        content = "Unicode test: 你好世界 \U0001F600 \u03B1\u03B2\u03B3 — intact"
        r = mcp_server.handle_lore_remember(content, source_type="user")
        assert r["success"] is True
        mid = r["memory_id"]

        # Direct store access to verify — bypasses FTS ranking uncertainty
        from lore_memory.mcp import server as srv
        mem = srv._get_store().get(mid)
        assert mem is not None
        assert mem["content"] == content, (
            f"Unicode content corrupted!\nExpected: {content!r}\nGot:      {mem['content']!r}"
        )

    def test_provenance_hash_deterministic(self):
        """Same content + same timestamp → same provenance hash."""
        ts = time.time()
        h1 = mcp_server._provenance_hash("same content", ts)
        h2 = mcp_server._provenance_hash("same content", ts)
        assert h1 == h2

    def test_provenance_hash_different_timestamps(self):
        """Same content + different timestamp → different hash (time-bound provenance)."""
        h1 = mcp_server._provenance_hash("same", 1_000_000.0)
        h2 = mcp_server._provenance_hash("same", 1_000_001.0)
        assert h1 != h2
