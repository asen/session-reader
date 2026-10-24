"""Offline integration tests; fixtures contain no real session data."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "session-reader.py"
SESSION_ID = "01234567-89ab-4cde-8fab-0123456789ab"
CLAUDE_SESSION_ID = "fedcba98-7654-4321-8abc-fedcba987654"
TIMESTAMP = "2026-01-01T12:00:00Z"


def encode(record):
    return (json.dumps(record, ensure_ascii=False) + "\n").encode()


def codex(typ, **payload):
    return {"timestamp": TIMESTAMP, "type": typ, "payload": payload}


def response(typ, **payload):
    return codex("response_item", type=typ, **payload)


def pi_message(entry_id, text, role="user"):
    return {"type": "message", "id": entry_id, "parentId": None, "timestamp": TIMESTAMP,
            "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def claude(typ, **fields):
    return {"type": typ, "sessionId": CLAUDE_SESSION_ID, "timestamp": TIMESTAMP, **fields}


def claude_message(entry_id, text, role="user", **fields):
    return claude(role, uuid=entry_id, parentUuid=None, message={"role": role, "content": text}, **fields)


def cursor_data(token):
    data = token.removeprefix("psr1.")
    return json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))


def read_parts(output):
    header, _, remainder = output.partition("\n---\n")
    text, _, footer = remainder.rpartition("\n---\n")
    return json.loads(header), text, json.loads(footer)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home / ".codex"),
                        CLAUDE_CONFIG_DIR=str(self.home / ".claude"),
                        PI_CODING_AGENT_SESSION_DIR=str(self.home / ".pi/agent/sessions"))
        self.pi_records = [
            {"type": "session", "version": 3, "id": SESSION_ID, "timestamp": TIMESTAMP},
            pi_message("u1", "Pi hello"),
            pi_message("a1", "Pi reply", "assistant"),
            {"type": "compaction", "id": "c1", "summary": "Pi summary"},
        ]
        self.codex_records = [
            codex("session_meta", id=SESSION_ID, cwd="/workspace", cli_version="test",
                  base_instructions={"text": "Saved base instructions"}, forked_from_id="parent-id"),
            response("message", role="developer", content=[{"type": "input_text", "text": "Developer text"}]),
            response("message", role="user", content=[{"type": "input_text", "text": "Codex hello"},
                                                     {"type": "input_image", "image_url": "SECRET_IMAGE"}]),
            codex("event_msg", type="user_message", message="Codex hello", images=["SECRET_EVENT_IMAGE"]),
            response("reasoning", summary=[{"type": "summary_text", "text": "PRIVATE_THINKING"}],
                     content=[{"type": "reasoning_text", "text": "PRIVATE_REASONING"}], encrypted_content="SECRET_CIPHER"),
            response("function_call", id="native-id", name="shell", call_id="call-1", arguments='{"cmd":"pwd"}'),
            response("function_call_output", call_id="call-1", output=[{"type": "input_text", "text": "tool result"}]),
            response("custom_tool_call", name="patch", call_id="call-2", input="patch text"),
            response("custom_tool_call_output", call_id="call-2", output="patch result"),
            codex("event_msg", type="agent_reasoning", text="PRIVATE_EVENT_THINKING"),
            codex("event_msg", type="item_completed", item={"type": "Reasoning", "id": "reason-1",
                  "summary_text": ["PRIVATE_MIRROR_THINKING"], "raw_content": ["PRIVATE_MIRROR_RAW"]}),
            codex("event_msg", type="item_completed", item={"type": "AgentMessage", "id": "agent-1",
                  "content": [{"type": "Text", "text": "Mirrored reply"}]}),
            codex("event_msg", type="token_count", info={"total_tokens": 42, "opaque": "SECRET_ACCOUNTING"}),
            response("message", role="assistant", content=[{"type": "output_text", "text": "Codex reply"}]),
            codex("compacted", message="Codex summary", replacement_history=[{"text": "SECRET_REPLACEMENT"}]),
            codex("world_state", state={"opaque": "SECRET_STATE"}),
            codex("turn_context", cwd="/workspace", model="test-model"),
        ]
        self.claude_records = [
            {"type": "custom-title", "sessionId": CLAUDE_SESSION_ID, "customTitle": "Claude title"},
            {"type": "file-history-snapshot", "messageId": "c-user", "snapshot": {"opaque": "SECRET_SNAPSHOT"}},
            claude_message("c-user", "Claude hello"),
            claude("attachment", uuid="c-prompt", attachment={"type": "prompt_snapshot", "systemPrompt": ["Claude system prompt"]}),
            claude("attachment", uuid="c-instructions", attachment={"type": "instructions"},
                   rendered=[{"content": "Saved CLAUDE.md instructions"}]),
            claude_message("c-thinking", [{"type": "thinking", "thinking": "PRIVATE_CLAUDE_THINKING", "signature": "SECRET_SIGNATURE"},
                                         {"type": "redacted_thinking", "data": "SECRET_REDACTED"}], "assistant"),
            claude_message("c-tool", [{"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "a.py"}}], "assistant"),
            claude("user", uuid="c-result", parentUuid="c-tool", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "is_error": False, "content": [
                    {"type": "text", "text": "Claude tool result"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "SECRET_IMAGE"}},
                    {"type": "tool_reference", "tool_name": "OtherTool"}]}]}),
            claude_message("c-reply", [{"type": "text", "text": "Claude reply"}], "assistant"),
            claude("system", uuid="c-boundary", subtype="compact_boundary", content="Compact boundary",
                   compactMetadata={"trigger": "auto", "preTokens": 42}),
            claude_message("c-summary", "Claude saved compaction summary", isCompactSummary=True),
            claude("attachment", uuid="c-accounting", attachment={"type": "total_tokens_reminder", "text": "SECRET_ACCOUNTING"},
                   rendered=[{"content": "SECRET_ACCOUNTING"}]),
            claude("progress", uuid="c-progress", data={"type": "agent_progress", "message": "SECRET_PROGRESS_STATE"}),
            claude("api-request-blob", message={"role": "assistant", "content": [{"type": "thinking", "thinking": "SECRET_BLOB"}]}),
            claude("api-request-shape", shape={"system": [{"type": "text", "text": "API system prompt"}],
                                              "tools": [{"name": "Read", "input_schema": {"type": "object"}}]}),
            {"type": "summary", "summary": "Claude legacy summary", "leafUuid": "c-summary"},
            claude("queue-operation", operation="enqueue", content="Queued prompt"),
        ]
        self.claude_path = self.write(self.home / ".claude/projects/work" / f"{CLAUDE_SESSION_ID}.jsonl", self.claude_records)
        self.pi_path = self.write(self.home / ".pi/agent/sessions/work" / f"date_{SESSION_ID}.jsonl", self.pi_records)
        self.codex_path = self.write(self.home / ".codex/sessions/2026/01/01" /
                                     f"rollout-2026-01-01T12-00-00-{SESSION_ID}.jsonl", self.codex_records)

    def write(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(encode(e) for e in records))
        return path

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                env=self.env, capture_output=True)
        self.assertEqual(result.returncode, code, result.stderr.decode())
        self.assertNotIn(b"Traceback", result.stderr)
        return result

    def offset(self, index):
        return sum(len(encode(e)) for e in self.codex_records[:index])

    def read(self, path=None, *args):
        return read_parts(self.cli("read", path or self.codex_path, *args).stdout.decode())

    def search(self, path=None, *args):
        return [json.loads(line) for line in self.cli("search", path or self.codex_path, *args).stdout.splitlines()]

    def test_version_and_help(self):
        self.assertEqual(self.cli("--version").stdout, b"session-reader.py 3.1\n")
        self.assertIn(b"--source", self.cli("--help").stdout)

    def test_pi_stats_read_search_show(self):
        stats = json.loads(self.cli("stats", self.pi_path).stdout)
        self.assertEqual(stats["format"], "pi")
        self.assertEqual(stats["entries"], 3)
        self.assertEqual(stats["messages_by_role"], {"user": 1, "assistant": 1})
        _, text, footer = self.read(self.pi_path)
        self.assertTrue(footer["eof"])
        for value in ("Pi hello", "Pi reply", "Pi summary"):
            self.assertIn(value, text)
        hits = self.search(self.pi_path, "hello")
        self.assertEqual(hits[1]["id"], "u1")
        _, text, _ = read_parts(self.cli("show", self.pi_path, "a1").stdout.decode())
        self.assertIn("Pi reply", text)
        self.assertNotIn("Pi summary", text)

    def test_pi_v2(self):
        self.pi_records[0]["version"] = 2
        self.write(self.pi_path, self.pi_records)
        self.assertEqual(json.loads(self.cli("stats", self.pi_path).stdout)["format"], "pi")

    def test_legacy_pi_cursor(self):
        # Same payload/identity/render offsets as pi-session-reader 2.0.
        options = {"raw": False, "thinking": False, "roles": [], "types": []}
        raw = encode(self.pi_records[1])
        cursor = {"f": hashlib.sha256((str(self.pi_path) + "\0" + SESSION_ID).encode()).hexdigest()[:24],
                  "b": len(encode(self.pi_records[0])), "p": 5,
                  "h": hashlib.sha256(raw.rstrip(b"\r\n")).hexdigest()[:24], "o": options}
        token = "psr1." + base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode().rstrip("=")
        _, text, footer = self.read(self.pi_path, "--cursor", token)
        self.assertIn("Pi hello", text)
        self.assertTrue(footer["eof"])

    def test_codex_stats(self):
        stats = json.loads(self.cli("stats", self.codex_path).stdout)
        self.assertEqual(stats["format"], "codex")
        self.assertEqual(stats["session"], SESSION_ID)
        self.assertEqual(stats["entries"], len(self.codex_records))
        self.assertEqual(stats["entry_types"]["session_meta"], 1)
        self.assertEqual(stats["messages_by_role"], {"developer": 1, "user": 1, "assistant": 1})
        self.assertEqual(stats["payload_types"]["function_call"], 1)
        self.assertEqual(stats["parent_session_not_imported"], "parent-id")
        self.assertFalse(stats["incomplete_tail"])

    def test_codex_render_and_omissions(self):
        header, text, footer = self.read()
        self.assertEqual(header["format"], "codex")
        self.assertTrue(footer["eof"])
        for value in ("byte:0", "Saved base instructions", "Developer text", "Codex hello", "Codex reply",
                      "Codex summary", "tool result", "call-1", "patch text", "patch result", "test-model", "Mirrored reply"):
            self.assertIn(value, text)
        self.assertIn("NOT applied", text)
        self.assertNotIn("SECRET_", text)
        self.assertNotIn("PRIVATE_", text)
        self.assertEqual(text.count("Codex hello"), 2)  # Physical mirrors stay visible.

    def test_thinking_includes_only_plaintext(self):
        _, text, _ = self.read(None, "--thinking")
        for value in ("PRIVATE_THINKING", "PRIVATE_REASONING", "PRIVATE_EVENT_THINKING",
                      "PRIVATE_MIRROR_THINKING", "PRIVATE_MIRROR_RAW"):
            self.assertIn(value, text)
        self.assertNotIn("SECRET_CIPHER", text)

    def test_reasoning_text_blocks_require_thinking(self):
        blocks = [{"type": "text", "text": "Public reply"},
                  {"type": "reasoning_text", "text": "PRIVATE_BLOCK_REASONING"}]
        pi_record = pi_message("a-reasoning", "", "assistant")
        pi_record["message"]["content"] = blocks
        cases = (
            (self.codex_path, self.codex_records[0], response("message", role="assistant", content=blocks),
             f"byte:{len(encode(self.codex_records[0]))}"),
            (self.pi_path, self.pi_records[0], pi_record, "a-reasoning"),
            (self.claude_path, self.claude_records[0], claude_message("c-reasoning", blocks, "assistant"), "c-reasoning"),
        )
        for path, header, record, entry_id in cases:
            with self.subTest(format=path.parent):
                self.write(path, [header, record])
                for command in (("read", path), ("show", path, entry_id)):
                    _, text, _ = read_parts(self.cli(*command).stdout.decode())
                    self.assertIn("Public reply", text)
                    self.assertNotIn("PRIVATE_BLOCK_REASONING", text)
                    for flag in ("--thinking", "--raw"):
                        _, text, _ = read_parts(self.cli(*command, flag).stdout.decode())
                        self.assertIn("PRIVATE_BLOCK_REASONING", text)
                self.assertEqual(len(self.search(path, "PRIVATE_BLOCK_REASONING")), 2)
                self.assertEqual(len(self.search(path, "PRIVATE_BLOCK_REASONING", "--thinking")), 3)

    def test_raw_preserves_original_records(self):
        _, text, _ = self.read(None, "--raw")
        self.assertEqual(text.encode(), self.codex_path.read_bytes())
        self.assertIn("SECRET_CIPHER", text)
        self.assertIn("SECRET_IMAGE", text)

    def test_codex_show_ids_and_search_cursors(self):
        hits = self.search(None, "Codex hello", "--type", "response_item", "--role", "user")
        self.assertEqual(len(hits), 3)
        hit = hits[1]
        self.assertEqual(hit["id"], f"byte:{self.offset(2)}")
        self.assertEqual(hit["payload_type"], "message")
        _, text, _ = read_parts(self.cli("show", self.codex_path, hit["id"]).stdout.decode())
        self.assertIn("Codex hello", text)
        self.assertNotIn("Codex reply", text)
        _, text, _ = self.read(None, "--cursor", hit["cursor"])
        self.assertIn("Codex hello", text)
        _, text, _ = read_parts(self.cli("show", self.codex_path, "byte:0").stdout.decode())
        self.assertIn("Saved base instructions", text)

    def test_type_and_role_filters(self):
        for typ in ("response_item", "function_call", "AgentMessage"):
            hits = self.search(None, "--type", typ)
            self.assertGreater(len(hits), 2)
            self.assertTrue(all(typ in (hit["type"], hit["payload_type"], hit["item_type"]) for hit in hits[1:-1]))
        _, text, _ = self.read(None, "--role", "tool")
        self.assertIn("tool result", text)
        self.assertIn("patch result", text)
        self.assertNotIn("Codex hello", text)

    def test_function_search_exposes_native_id(self):
        hits = self.search(None, "--type", "function_call")
        self.assertEqual(hits[1]["native_id"], "native-id")
        self.assertEqual(hits[1]["id"], f"byte:{self.offset(5)}")

    def test_completed_tool_events(self):
        records = [self.codex_records[0]] + [codex("event_msg", type="item_completed", item=item) for item in (
            {"type": "UserMessage", "content": [{"type": "text", "text": "event user"}]},
            {"type": "CommandExecution", "command": ["pwd"], "aggregated_output": "command result", "exit_code": 0},
            {"type": "FileChange", "changes": {"file.py": "patch diff"}, "stdout": "patch applied"},
            {"type": "McpToolCall", "server": "test", "tool": "query", "arguments": {"q": "hi"},
             "result": {"content": [{"type": "text", "text": "MCP text"}, {"type": "image", "data": "SECRET_IMAGE"}]}},
            {"type": "DynamicToolCall", "tool": "dynamic", "content_items": [{"type": "inputText", "text": "dynamic result"}]},
        )]
        self.write(self.codex_path, records)
        _, text, _ = self.read()
        for value in ("event user", "command result", "patch diff", "patch applied", "MCP text", "dynamic result"):
            self.assertIn(value, text)
        self.assertNotIn("SECRET_IMAGE", text)

    def test_discovery_by_id_both_sources(self):
        for source, expected in (("pi", "pi"), ("codex", "codex")):
            stats = json.loads(self.cli("--source", source, "stats", SESSION_ID[:12]).stdout)
            self.assertEqual(stats["format"], expected)
        result = self.cli("stats", SESSION_ID[:12], code=2)
        self.assertIn(b"found 2", result.stderr)
        self.pi_path.unlink()
        stats = json.loads(self.cli("stats", SESSION_ID[:12]).stdout)
        self.assertEqual(stats["format"], "codex")

    def test_directory_override_and_archive(self):
        stats = json.loads(self.cli("--sessions-dir", self.pi_path.parent, "stats", SESSION_ID[:12]).stdout)
        self.assertEqual(stats["format"], "pi")
        archive = self.home / ".codex/archived_sessions" / self.codex_path.name
        archive.parent.mkdir()
        self.codex_path.rename(archive)
        stats = json.loads(self.cli("--source", "codex", "stats", SESSION_ID[:12]).stdout)
        self.assertEqual(stats["source"], str(archive))

    def test_environment_discovery_roots(self):
        for key, root in (("CODEX_HOME", self.home / "custom-codex"),
                          ("PI_CODING_AGENT_SESSION_DIR", self.home / "custom-pi")):
            self.env[key] = str(root)
        new_codex = self.home / "custom-codex/sessions" / self.codex_path.name
        self.write(new_codex, self.codex_records)
        new_pi = self.home / "custom-pi" / self.pi_path.name
        self.write(new_pi, self.pi_records)
        for source, path in (("codex", new_codex), ("pi", new_pi)):
            stats = json.loads(self.cli("--source", source, "stats", SESSION_ID[:12]).stdout)
            self.assertEqual(stats["source"], str(path))

    def test_source_rejects_wrong_format_even_for_path(self):
        result = self.cli("--source", "pi", "stats", self.codex_path, code=2)
        self.assertIn(b"detected codex", result.stderr)

    def test_session_id_only_header(self):
        self.codex_records[0]["payload"]["session_id"] = self.codex_records[0]["payload"].pop("id")
        self.write(self.codex_path, self.codex_records)
        self.assertEqual(json.loads(self.cli("stats", self.codex_path).stdout)["session"], SESSION_ID)

    def test_bounded_unicode_continuation_is_lossless(self):
        records = [self.codex_records[0], response("message", role="user",
                   content=[{"type": "input_text", "text": "界🙂café " * 1000}])]
        self.write(self.codex_path, records)
        _, expected, _ = self.read(None, "--role", "user", "--max-bytes", "50000")
        chunks, cursor = [], None
        for _ in range(100):
            args = ["--cursor", cursor] if cursor else ["--role", "user"]
            result = self.cli("read", self.codex_path, *args, "--max-bytes", "2048")
            self.assertLessEqual(len(result.stdout), 2048)
            _, text, footer = read_parts(result.stdout.decode())
            text = re.sub(r"^\[continuation byte:\d+ at rendered character \d+\]\n", "", text)
            chunks.append(text)
            if footer["eof"]:
                break
            self.assertNotEqual(cursor, footer["next_cursor"])
            cursor = footer["next_cursor"]
        else:
            self.fail("continuation did not reach EOF")
        self.assertEqual("".join(chunks), expected)

    def test_search_pagination_and_budget(self):
        cursor, ids = None, []
        for _ in range(50):
            args = ["--cursor", cursor] if cursor else []
            result = self.cli("search", self.codex_path, "", "--limit", "1", "--max-bytes", "2048", *args)
            self.assertLessEqual(len(result.stdout), 2048)
            lines = [json.loads(line) for line in result.stdout.splitlines()]
            ids.extend(hit["id"] for hit in lines[1:-1])
            if lines[-1]["eof"]:
                break
            cursor = lines[-1]["next_cursor"]
        else:
            self.fail("search did not reach EOF")
        self.assertEqual(ids, [f"byte:{self.offset(i)}" for i in range(len(self.codex_records))])

    def test_append_preserves_eof_cursor(self):
        _, _, footer = self.read()
        with self.codex_path.open("ab") as f:
            f.write(encode(response("message", role="user", content=[{"type": "input_text", "text": "appended message"}])))
        _, text, footer = self.read(None, "--cursor", footer["next_cursor"])
        self.assertIn("appended message", text)
        self.assertNotIn("Codex hello", text)
        self.assertTrue(footer["eof"])

    def test_incomplete_tail_can_be_completed(self):
        record = encode(response("message", role="user", content=[{"type": "input_text", "text": "completed tail"}]))
        with self.codex_path.open("ab") as f:
            f.write(record[:30])
        result = self.cli("read", self.codex_path)
        _, _, footer = read_parts(result.stdout.decode())
        self.assertTrue(footer["incomplete_tail"])
        self.assertFalse(footer["eof"])
        self.assertIn(b"not consumed", result.stderr)
        with self.codex_path.open("ab") as f:
            f.write(record[30:])
        _, text, footer = self.read(None, "--cursor", footer["next_cursor"])
        self.assertIn("completed tail", text)
        self.assertTrue(footer["eof"])
        self.assertFalse(footer["incomplete_tail"])

    def test_cursor_rejects_changed_record_path_and_settings(self):
        _, _, footer = self.read(None, "--max-bytes", "2048")
        token = footer["next_cursor"]
        data = cursor_data(token)
        result = self.cli("read", self.pi_path, "--cursor", token, code=2)
        self.assertIn(b"different session/path", result.stderr)
        result = self.cli("read", self.codex_path, "--cursor", token, "--thinking", code=2)
        self.assertIn(b"different thinking", result.stderr)
        raw = self.codex_path.read_bytes()
        position = data["b"]
        changed = raw[:position] + raw[position:].replace(b"2026", b"2025", 1)
        self.codex_path.write_bytes(changed)
        result = self.cli("read", self.codex_path, "--cursor", token, code=2)
        self.assertIn(b"record changed", result.stderr)

    def test_cursor_rejects_truncation(self):
        _, _, footer = self.read()
        self.write(self.codex_path, self.codex_records[:1])
        result = self.cli("read", self.codex_path, "--cursor", footer["next_cursor"], code=2)
        self.assertIn(b"truncated", result.stderr)

    def test_terminal_controls_are_escaped(self):
        self.write(self.codex_path, [self.codex_records[0], response("message", role="user",
                   content=[{"type": "input_text", "text": "hello\x1b[31m\u202e"}])])
        _, text, _ = self.read()
        self.assertIn(r"hello\x1b[31m\u202e", text)
        self.assertNotIn("\x1b", text)

    def test_errors_are_short_and_no_partial_stdout(self):
        for record in ({"type": "response_item", "payload": None}, {"type": "response_item"}, []):
            self.write(self.codex_path, [self.codex_records[0], record])
            result = self.cli("read", self.codex_path, code=2)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"session-reader.py:", result.stderr)
        self.cli("read", self.pi_path, "--cursor", "garbage", code=2)
        self.cli("show", self.pi_path, "missing", code=2)
        self.cli("stats", self.home / "missing.jsonl", code=2)
        self.write(self.codex_path, [{"type": "unknown"}])
        result = self.cli("stats", self.codex_path, code=2)
        self.assertIn(b"Expected a Pi", result.stderr)

    def test_claude_stats(self):
        stats = json.loads(self.cli("stats", self.claude_path).stdout)
        self.assertEqual(stats["format"], "claude")
        self.assertEqual(stats["session"], CLAUDE_SESSION_ID)
        self.assertEqual(stats["entries"], len(self.claude_records))
        self.assertEqual(stats["entry_types"]["attachment"], 3)
        self.assertEqual(stats["messages_by_role"], {"user": 3, "assistant": 3})
        self.assertFalse(stats["incomplete_tail"])

    def test_claude_content_and_omissions(self):
        header, text, footer = self.read(self.claude_path)
        self.assertEqual(header["format"], "claude")
        self.assertTrue(footer["eof"])
        for value in ("Claude title", "Claude hello", "Claude reply", "Claude tool result", "tool-1", "Read", "a.py",
                      "OtherTool", "Claude system prompt", "Saved CLAUDE.md instructions", "API system prompt",
                      "input_schema", "Claude saved compaction summary", "Claude legacy summary", "Queued prompt"):
            self.assertIn(value, text)
        self.assertIn("replacement NOT applied", text)
        self.assertNotIn("SECRET_", text)
        self.assertNotIn("PRIVATE_", text)
        _, text, _ = self.read(self.claude_path, "--thinking")
        self.assertIn("PRIVATE_CLAUDE_THINKING", text)
        self.assertNotIn("SECRET_", text)

    def test_claude_raw(self):
        _, text, _ = self.read(self.claude_path, "--raw")
        self.assertEqual(text.encode(), self.claude_path.read_bytes())
        self.assertIn("SECRET_IMAGE", text)
        self.assertIn("SECRET_SIGNATURE", text)

    def test_claude_show_native_and_byte_ids(self):
        offset = sum(len(encode(e)) for e in self.claude_records[:2])
        for entry_id in ("c-user", f"byte:{offset}"):
            _, text, _ = read_parts(self.cli("show", self.claude_path, entry_id).stdout.decode())
            self.assertIn("Claude hello", text)
            self.assertNotIn("Claude reply", text)
        _, text, _ = read_parts(self.cli("show", self.claude_path, "byte:0").stdout.decode())
        self.assertIn("Claude title", text)

    def test_claude_search_and_filters(self):
        hits = self.search(self.claude_path, "Claude tool result")
        self.assertEqual(len(hits), 3)
        self.assertEqual(hits[1]["id"], "c-result")
        self.assertEqual(hits[1]["parent"], "c-tool")
        self.assertEqual(hits[1]["role"], "user")
        _, text, _ = self.read(self.claude_path, "--cursor", hits[1]["cursor"])
        self.assertIn("Claude tool result", text)
        for typ, expected_id in (("tool_use", "c-tool"), ("tool_result", "c-result"),
                                 ("compact_boundary", "c-boundary"), ("prompt_snapshot", "c-prompt")):
            hits = self.search(self.claude_path, "--type", typ)
            self.assertEqual([hit["id"] for hit in hits[1:-1]], [expected_id])
        _, text, _ = self.read(self.claude_path, "--role", "assistant")
        self.assertIn("Claude reply", text)
        self.assertNotIn("Claude hello", text)
        self.assertNotIn("Claude tool result", text)

    def test_claude_discovery_and_config_root(self):
        for flags in ([], ["--source", "claude"], ["--sessions-dir", self.claude_path.parent]):
            stats = json.loads(self.cli(*flags, "stats", CLAUDE_SESSION_ID[:12]).stdout)
            self.assertEqual(stats["format"], "claude")
        self.env["CLAUDE_CONFIG_DIR"] = str(self.home / "custom-claude")
        new_path = self.write(self.home / "custom-claude/projects/work" / self.claude_path.name, self.claude_records)
        stats = json.loads(self.cli("--source", "claude", "stats", CLAUDE_SESSION_ID[:12]).stdout)
        self.assertEqual(stats["source"], str(new_path))
        self.cli("--source", "claude", "stats", self.codex_path, code=2)
        self.cli("--source", "codex", "stats", self.claude_path, code=2)

    def test_claude_headerless_variants(self):
        for records in (self.claude_records[1:], self.claude_records[2:],
                        [self.claude_records[15]], [self.claude_records[0]]):
            self.write(self.claude_path, records)
            stats = json.loads(self.cli("stats", self.claude_path).stdout)
            self.assertEqual(stats["format"], "claude")
            self.assertEqual(stats["session"], CLAUDE_SESSION_ID)
            self.assertEqual(stats["entries"], len(records))

    def test_claude_subagents_are_separate(self):
        agent_id = "a1b2c3d4"
        records = [claude_message("agent-user", "Separate subagent prompt", isSidechain=True, agentId=agent_id)]
        path = self.write(self.claude_path.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl", records)
        for query in (agent_id[:5], f"agent-{agent_id}"):
            stats = json.loads(self.cli("--source", "claude", "stats", query).stdout)
            self.assertEqual(stats["source"], str(path))
            self.assertEqual(stats["session"], CLAUDE_SESSION_ID)
        _, text, _ = self.read(self.claude_path)
        self.assertNotIn("Separate subagent prompt", text)
        _, text, _ = self.read(path)
        self.assertIn("Separate subagent prompt", text)
        self.assertIn("NOT imported", text)

    def test_claude_attachment_fallbacks_and_tool_schemas(self):
        records = [claude("attachment", attachment=attachment) for attachment in (
            {"type": "instructions", "files": [{"path": "CLAUDE.md", "content": "Fallback instructions"}]},
            {"type": "deferred_tools_record", "entries": [{"name": "DeferredTool", "input_schema": {"type": "object"}}],
             "toolInputCopies": ["SECRET_INPUT_COPY"]},
            {"type": "hook_system_message", "content": "Saved hook output"},
            {"type": "thinking_drop", "data": "SECRET_THINKING_STATE"},
        )]
        self.write(self.claude_path, records)
        _, text, _ = self.read(self.claude_path)
        for value in ("Fallback instructions", "CLAUDE.md", "DeferredTool", "input_schema", "Saved hook output"):
            self.assertIn(value, text)
        self.assertNotIn("SECRET_", text)

    def test_claude_bounded_unicode_continuation(self):
        self.write(self.claude_path, [claude_message("c-large", "界🙂café " * 1000)])
        _, expected, _ = self.read(self.claude_path, "--max-bytes", "50000")
        chunks, cursor = [], None
        for _ in range(100):
            args = ["--cursor", cursor] if cursor else []
            result = self.cli("read", self.claude_path, "--max-bytes", "2048", *args)
            self.assertLessEqual(len(result.stdout), 2048)
            _, text, footer = read_parts(result.stdout.decode())
            chunks.append(re.sub(r"^\[continuation c-large at rendered character \d+\]\n", "", text))
            if footer["eof"]:
                break
            self.assertNotEqual(cursor, footer["next_cursor"])
            cursor = footer["next_cursor"]
        else:
            self.fail("Claude continuation did not reach EOF")
        self.assertEqual("".join(chunks), expected)

    def test_claude_search_pagination(self):
        cursor, ids = None, []
        for _ in range(50):
            args = ["--cursor", cursor] if cursor else []
            result = self.cli("search", self.claude_path, "", "--limit", "1", "--max-bytes", "2048", *args)
            self.assertLessEqual(len(result.stdout), 2048)
            lines = [json.loads(line) for line in result.stdout.splitlines()]
            ids.extend(hit["id"] for hit in lines[1:-1])
            if lines[-1]["eof"]:
                break
            cursor = lines[-1]["next_cursor"]
        else:
            self.fail("Claude search did not reach EOF")
        expected, byte = [], 0
        for e in self.claude_records:
            expected.append(e.get("uuid", f"byte:{byte}"))
            byte += len(encode(e))
        self.assertEqual(ids, expected)

    def test_claude_cursor_survives_append_and_incomplete_tail(self):
        _, _, footer = self.read(self.claude_path)
        record = encode(claude_message("c-appended", "Appended Claude message"))
        with self.claude_path.open("ab") as f:
            f.write(record[:30])
        _, text, incomplete = self.read(self.claude_path, "--cursor", footer["next_cursor"])
        self.assertNotIn("Claude hello", text)
        self.assertTrue(incomplete["incomplete_tail"])
        with self.claude_path.open("ab") as f:
            f.write(record[30:])
        _, text, footer = self.read(self.claude_path, "--cursor", incomplete["next_cursor"])
        self.assertIn("Appended Claude message", text)
        self.assertNotIn("Claude hello", text)
        self.assertTrue(footer["eof"])

    def test_claude_cursor_rejects_changes_and_different_paths(self):
        hit = self.search(self.claude_path, "Claude hello")[1]
        token = hit["cursor"]
        self.cli("read", self.codex_path, "--cursor", token, code=2)
        result = self.cli("read", self.claude_path, "--cursor", token, "--thinking", code=2)
        self.assertIn(b"different thinking", result.stderr)
        self.claude_path.write_bytes(self.claude_path.read_bytes().replace(b"Claude hello", b"Claude other"))
        result = self.cli("read", self.claude_path, "--cursor", token, code=2)
        self.assertIn(b"record changed", result.stderr)

    def test_claude_control_characters_and_invalid_records(self):
        self.write(self.claude_path, [claude_message("c-control", "hello\x1b[31m\u202e")])
        _, text, _ = self.read(self.claude_path)
        self.assertIn(r"hello\x1b[31m\u202e", text)
        self.assertNotIn("\x1b", text)
        for record in ([], {}, {"type": "user", "message": None}, {"type": "assistant", "message": "invalid"}):
            self.write(self.claude_path, [self.claude_records[0], record])
            result = self.cli("read", self.claude_path, code=2)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"Expected a Claude record", result.stderr)

    def test_claude_extra_blocks_and_malformed_attachments(self):
        records = [claude_message("c-blocks", [
            {"type": "server_tool_use", "id": "server-1", "name": "web_search", "input": {"query": "test"}},
            {"type": "tool_addition", "tool": {"name": "NewTool", "input_schema": {"type": "object"}}},
            {"type": "document", "source": {"data": "SECRET_DOCUMENT"}},
            {"type": "thinking", "thinking": None},
            {"type": "tool_result", "tool_use_id": "server-1", "content":
                {"type": "image", "source": {"media_type": "image/png", "data": "SECRET_IMAGE"}}},
        ], "assistant"),
            claude("attachment", attachment={"type": "prompt_snapshot", "systemPrompt": [
                {"type": "text", "text": "Typed prompt block"},
                {"type": "redacted_thinking", "data": "SECRET_REDACTED_PROMPT"}]}),
            claude("attachment", attachment={"type": "prompt_snapshot", "systemPrompt": None}),
            claude("attachment", attachment={"type": "instructions", "files": None}),
        ]
        self.write(self.claude_path, records)
        _, text, _ = self.read(self.claude_path, "--thinking")
        self.assertIn("server-1", text)
        self.assertIn("NewTool", text)
        self.assertIn("Typed prompt block", text)
        self.assertNotIn("SECRET_", text)

    def test_claude_duplicate_uuids_can_be_shown_by_byte(self):
        records = [claude_message("reused-uuid", "first occurrence"), claude_message("reused-uuid", "second occurrence")]
        self.write(self.claude_path, records)
        byte = len(encode(records[0]))
        _, text, _ = read_parts(self.cli("show", self.claude_path, f"byte:{byte}").stdout.decode())
        self.assertIn("second occurrence", text)
        self.assertNotIn("first occurrence", text)

    def test_claude_snapshot_first_cursor_identity_survives_append(self):
        self.write(self.claude_path, [self.claude_records[1]])
        _, _, footer = self.read(self.claude_path)
        with self.claude_path.open("ab") as f:
            f.write(encode(claude_message("later-message", "Appended after snapshot")))
        _, text, footer = self.read(self.claude_path, "--cursor", footer["next_cursor"])
        self.assertIn("Appended after snapshot", text)
        self.assertTrue(footer["eof"])

    def test_claude_workflow_journal_is_not_a_transcript(self):
        self.write(self.claude_path, [{"type": "launched"}, {"type": "result", "result": "workflow status"}])
        self.cli("stats", self.claude_path, code=2)

    def test_discovery_ambiguity_across_three_sources(self):
        path = self.claude_path.with_name(f"{SESSION_ID}.jsonl")
        self.write(path, [{"type": "custom-title", "sessionId": SESSION_ID, "customTitle": "Shared ID"}])
        result = self.cli("stats", SESSION_ID[:12], code=2)
        self.assertIn(b"found 3", result.stderr)
        stats = json.loads(self.cli("--source", "claude", "stats", SESSION_ID[:12]).stdout)
        self.assertEqual(stats["format"], "claude")

    def test_claude_blocks_do_not_change_existing_codex_rendering(self):
        self.write(self.codex_path, [self.codex_records[0], response("message", role="assistant", content=[
            {"type": "tool_reference", "tool_name": "ClaudeSpecificTool"},
        ])])
        _, text, _ = self.read()
        self.assertIn("[unsupported block 'tool_reference'; use --raw]", text)
        self.assertNotIn("ClaudeSpecificTool", text)


if __name__ == "__main__":
    unittest.main()
