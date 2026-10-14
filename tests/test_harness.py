"""Automated unit and integration tests for One Agent Harness."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import urllib.request

from harness.adapters import consolidate_turns, is_meta_handoff_text, normalize_path, paths_match
from harness.adapters.claude import encode_claude_project_dir
from harness.engine import HarnessEngine, is_scratch_cwd
from harness.formatting import agent_badge, relative_time, shorten_path
from harness.mcp_server import MCPServer
from harness.watcher import ContextWatcher


class TestOneAgentHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = self.tmp_dir.name
        # Test workspace with a trailing space (matching real macOS edge case)
        self.workspace = os.path.join(self.root, "workspace ")
        os.makedirs(self.workspace, exist_ok=True)

        with open(os.path.join(self.workspace, "AGENTS.md"), "w", encoding="utf-8") as fp:
            fp.write("# Project Instructions\nAlways run unit tests.\n")

        self.claude_home = os.path.join(self.root, ".claude")
        self.agy_home = os.path.join(self.root, ".gemini", "antigravity")
        self.codex_home = os.path.join(self.root, ".codex")
        self.grok_home = os.path.join(self.root, ".grok")

        self._seed_claude_fixture()
        self._seed_antigravity_fixture()
        self._seed_codex_fixture()
        self._seed_grok_fixture()

        self.engine = HarnessEngine(
            claude_home=self.claude_home,
            agy_home=self.agy_home,
            codex_home=self.codex_home,
            grok_home=self.grok_home,
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _seed_claude_fixture(self) -> None:
        proj_dir = os.path.join(self.claude_home, "projects", encode_claude_project_dir(self.workspace))
        plans_dir = os.path.join(self.claude_home, "plans")
        os.makedirs(proj_dir, exist_ok=True)
        os.makedirs(plans_dir, exist_ok=True)

        with open(os.path.join(plans_dir, "auth-plan.md"), "w", encoding="utf-8") as fp:
            fp.write("# Auth Refactor Plan\n- [x] Step 1: Token parser\n- [ ] Step 2: Refresh endpoint\n")

        sid = "11111111-2222-3333-4444-555555555555"
        fpath = os.path.join(proj_dir, f"{sid}.jsonl")
        records = [
            {
                "type": "user",
                "isMeta": False,
                "cwd": self.workspace,
                "gitBranch": "feat/auth",
                "timestamp": "2026-10-14T20:00:00Z",
                "message": {
                    "role": "user",
                    "content": '<pasted_content id="p1">Implement JWT token parser and refresh endpoint</pasted_content>',
                },
            },
            # Simulate multiple consecutive assistant chunks (thinking, tool_use, text)
            {
                "type": "assistant",
                "cwd": self.workspace,
                "gitBranch": "feat/auth",
                "timestamp": "2026-10-14T20:00:10Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "Need to edit auth/jwt.py and run pytest."}],
                },
            },
            {
                "type": "assistant",
                "cwd": self.workspace,
                "gitBranch": "feat/auth",
                "timestamp": "2026-10-14T20:00:20Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "tu_1", "name": "Edit", "input": {"file_path": f"{self.workspace}/auth/jwt.py"}}],
                },
            },
            {
                "type": "assistant",
                "cwd": self.workspace,
                "gitBranch": "feat/auth",
                "timestamp": "2026-10-14T20:01:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I implemented the token parser in auth/jwt.py. Next is the refresh endpoint."},
                        {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"command": "pytest tests/test_jwt.py"}},
                    ],
                },
            },
            # Meta command outputs and /exit should be ignored
            {
                "type": "user",
                "isMeta": False,
                "cwd": self.workspace,
                "timestamp": "2026-10-14T20:02:00Z",
                "message": {"role": "user", "content": "<local-command-stdout>Set model</local-command-stdout>"},
            },
            {
                "type": "user",
                "isMeta": False,
                "cwd": self.workspace,
                "timestamp": "2026-10-14T20:03:00Z",
                "message": {"role": "user", "content": "/exit"},
            },
            {"type": "ai-title", "aiTitle": "JWT Token Parser & Refresh", "sessionId": sid},
        ]
        with open(fpath, "w", encoding="utf-8") as fp:
            for r in records:
                fp.write(json.dumps(r) + "\n")

    def _seed_antigravity_fixture(self) -> None:
        os.makedirs(self.agy_home, exist_ok=True)
        conv_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        conv_dir = os.path.join(self.agy_home, "brain", conv_id)
        logs_dir = os.path.join(conv_dir, ".system_generated", "logs")
        os.makedirs(logs_dir, exist_ok=True)

        # Also create a subagent transcript that should be filtered out
        sub_id = "subagent-1111-2222-3333-444444444444"
        sub_logs = os.path.join(self.agy_home, "brain", sub_id, ".system_generated", "logs")
        os.makedirs(sub_logs, exist_ok=True)
        with open(os.path.join(sub_logs, "transcript.jsonl"), "w", encoding="utf-8") as fp:
            fp.write(
                json.dumps(
                    {
                        "step_index": 0,
                        "type": "SYSTEM_MESSAGE",
                        "content": "<SYSTEM_MESSAGE>Subagent task</SYSTEM_MESSAGE>",
                    }
                )
                + "\n"
            )

        # Seed conversation_summaries.db
        dbp = os.path.join(self.agy_home, "conversation_summaries.db")
        conn = sqlite3.connect(dbp)
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT, title TEXT, preview TEXT, workspace_uris TEXT, "
            "parent_conversation_id TEXT, nesting_depth INTEGER)"
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?)",
            (
                conv_id,
                "Antigravity Adapter Build",
                "",
                json.dumps([f"file://{self.workspace}"]),
                "",
                0,
            ),
        )
        conn.commit()
        conn.close()

        with open(os.path.join(conv_dir, "implementation_plan.md"), "w", encoding="utf-8") as fp:
            fp.write("# Antigravity Plan\nBuild cross-agent context bridge.\n")

        # Test truncated_fields fallback to transcript_full.jsonl
        tpath = os.path.join(logs_dir, "transcript.jsonl")
        tfull_path = os.path.join(logs_dir, "transcript_full.jsonl")
        compact_records = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": "2026-10-14T21:00:00Z",
                "content": "<USER_REQUEST>\nTRUNCATED\n</USER_REQUEST>",
                "truncated_fields": ["content"],
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-10-14T21:01:00Z",
                "content": "Created the adapter and verified schema.",
                "thinking": "Checking transcript.jsonl structure.",
                "tool_calls": [
                    {
                        "name": "write_to_file",
                        "args": {
                            "TargetFile": f'"{self.workspace}/harness/adapters/antigravity.py"',
                            "Cwd": f'"{self.workspace}"',
                        },
                    }
                ],
            },
        ]
        full_records = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": "2026-10-14T21:00:00Z",
                "content": "<USER_REQUEST>\nBuild the Antigravity adapter and test it thoroughly\n</USER_REQUEST>",
            },
            compact_records[1],
        ]
        with open(tpath, "w", encoding="utf-8") as fp:
            for r in compact_records:
                fp.write(json.dumps(r) + "\n")
        with open(tfull_path, "w", encoding="utf-8") as fp:
            for r in full_records:
                fp.write(json.dumps(r) + "\n")

    def _seed_codex_fixture(self) -> None:
        sdir = os.path.join(self.codex_home, "sessions", "2026", "10", "14")
        os.makedirs(sdir, exist_ok=True)

        # Create state_5.sqlite
        dbp = os.path.join(self.codex_home, "state_5.sqlite")
        conn = sqlite3.connect(dbp)
        conn.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER, "
            "source TEXT, model_provider TEXT, cwd TEXT, title TEXT, sandbox_policy TEXT, "
            "approval_mode TEXT, tokens_used INTEGER, has_user_event INTEGER, archived INTEGER, "
            "cli_version TEXT, first_user_message TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, "
            "thread_source TEXT, preview TEXT, recency_at INTEGER, recency_at_ms INTEGER, "
            "history_mode TEXT, name TEXT, is_pinned INTEGER, originator TEXT, git_branch TEXT, agent_path TEXT)"
        )
        conn.commit()
        conn.close()

        sid = "01a13b07-87fb-7bd1-a7a6-b50c2766287a"
        fpath = os.path.join(sdir, f"rollout-2026-10-14T21-30-00-{sid}.jsonl")

        records = [
            {
                "timestamp": "2026-10-14T21:30:00Z",
                "ordinal": 0,
                "type": "session_meta",
                "payload": {"session_id": sid, "id": sid, "cwd": self.workspace, "thread_source": "user"},
            },
            {
                "timestamp": "2026-10-14T21:30:01Z",
                "ordinal": 1,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<skill>\n<name>handoff</name>\n</skill>"}],
                },
            },
            {
                "timestamp": "2026-10-14T21:30:02Z",
                "ordinal": 2,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Optimize SQLite query performance in codex adapter"}],
                },
            },
            {
                "timestamp": "2026-10-14T21:30:04Z",
                "ordinal": 3,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "input": 'text(await tools.exec_command({cmd:"sqlite3 state.db \'VACUUM\'" , max_output_tokens:1000}));',
                },
            },
            {
                "timestamp": "2026-10-14T21:30:05Z",
                "ordinal": 4,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Added composite index on updated_at."}],
                },
            },
        ]
        with open(fpath, "w", encoding="utf-8") as fp:
            for r in records:
                fp.write(json.dumps(r) + "\n")

        # Also seed a guardian_review rollout that MUST be ignored
        g_id = "01a13b07-9999-7bd1-a7a6-b50c2766287a"
        g_path = os.path.join(sdir, f"rollout-2026-10-14T21-35-00-{g_id}.jsonl")
        with open(g_path, "w", encoding="utf-8") as fp:
            fp.write(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "session_id": sid,
                            "id": g_id,
                            "parent_thread_id": sid,
                            "thread_source": "guardian_review",
                            "cwd": self.workspace,
                        },
                    }
                )
                + "\n"
            )

        with open(os.path.join(self.codex_home, "session_index.jsonl"), "w", encoding="utf-8") as fp:
            fp.write(json.dumps({"id": sid, "thread_name": "Optimize SQLite query", "updated_at": "2026-10-14T21:30:05Z"}) + "\n")

    def _seed_grok_fixture(self) -> None:
        sdir = os.path.join(self.grok_home, "sessions")
        os.makedirs(sdir, exist_ok=True)
        sid = "99999999-8888-7777-6666-555555555555"
        fpath = os.path.join(sdir, f"{sid}.jsonl")
        records = [
            {
                "session_id": sid,
                "cwd": self.workspace,
                "title": "Grok CLI deployment script",
                "timestamp": "2026-10-14T21:45:00Z",
                "role": "user",
                "content": "Create deployment script for Grok CLI",
            },
            {
                "session_id": sid,
                "cwd": self.workspace,
                "timestamp": "2026-10-14T21:45:10Z",
                "role": "assistant",
                "content": "Created deploy.sh and verified permissions.",
            },
        ]
        with open(fpath, "w", encoding="utf-8") as fp:
            for r in records:
                fp.write(json.dumps(r) + "\n")

    def test_trailing_space_normalization_and_empty_cwd_rejection(self) -> None:
        # Stripped path should resolve to the existing trailing-space directory on disk
        stripped_ws = self.workspace.rstrip(" ")
        self.assertEqual(normalize_path(stripped_ws), self.workspace)
        self.assertTrue(paths_match(stripped_ws, self.workspace))
        # Empty session_cwd must NEVER match a specific cwd filter
        self.assertFalse(paths_match("/some/other/dir", ""))

        non_matching = self.engine.list_sessions(cwd="/non/existent/project/path")
        self.assertEqual(len(non_matching), 0)

    def test_list_and_extract_across_all_4_agents(self) -> None:
        sessions = self.engine.list_sessions(cwd=self.workspace)
        agents_found = {s.agent for s in sessions}
        self.assertEqual(agents_found, {"claude", "antigravity", "codex", "grok"})

        # Verify Antigravity subagent and Codex guardian_review were excluded
        session_ids = {s.session_id for s in sessions}
        self.assertNotIn("subagent-1111-2222-3333-444444444444", session_ids)
        self.assertNotIn("01a13b07-9999-7bd1-a7a6-b50c2766287a", session_ids)

        # Check Claude extraction (unwrapped <pasted_content>, ignored /exit, consolidated assistant chunks)
        c_bundle = self.engine.extract_bundle(source_agent="claude", cwd=self.workspace, max_turns=2)
        assert c_bundle is not None
        self.assertEqual(c_bundle.title, "JWT Token Parser & Refresh")
        self.assertEqual(c_bundle.initial_goal, "Implement JWT token parser and refresh endpoint")
        self.assertEqual(c_bundle.latest_user_request, "Implement JWT token parser and refresh endpoint")
        self.assertEqual(len(c_bundle.turns), 2)
        self.assertIn(f"{self.workspace}/auth/jwt.py", c_bundle.files_modified)

        # Check Antigravity extraction (resolved truncated_fields from transcript_full.jsonl + DB title)
        a_bundle = self.engine.extract_bundle(source_agent="antigravity", cwd=self.workspace)
        assert a_bundle is not None
        self.assertEqual(a_bundle.title, "Antigravity Adapter Build")
        self.assertIn("Build the Antigravity adapter and test it thoroughly", a_bundle.turns[0].content)

        # Check Codex extraction (filtered <skill>, unwrapped tools.exec_command)
        x_bundle = self.engine.extract_bundle(source_agent="codex", cwd=self.workspace)
        assert x_bundle is not None
        self.assertEqual(x_bundle.title, "Optimize SQLite query")
        self.assertEqual(len(x_bundle.turns), 2)
        self.assertTrue(any("sqlite3 state.db 'VACUUM'" in cmd for cmd in x_bundle.recent_commands_and_errors))

    def test_bidirectional_session_switch_and_sqlite_injection(self) -> None:
        res_to_codex = self.engine.switch_session(
            to_agent="codex",
            from_agent="claude",
            cwd=self.workspace,
        )
        new_codex_id = res_to_codex["injected_session_id"]

        # Verify injected into state_5.sqlite
        conn = sqlite3.connect(os.path.join(self.codex_home, "state_5.sqlite"))
        row = conn.execute("SELECT id, title FROM threads WHERE id = ?", (new_codex_id,)).fetchone()
        conn.close()
        self.assertIsNotNone(row)

        # Verify Codex can read back the injected session
        re_read_codex = self.engine.extract_bundle(source_agent="codex", session_id=new_codex_id)
        assert re_read_codex is not None
        self.assertIn("HANDOFF FROM CLAUDE", re_read_codex.turns[0].content)

    def test_mcp_server_readonly_root_and_scratch_cwd_fallback(self) -> None:
        mcp = MCPServer(engine=self.engine)

        # Simulate Antigravity / Codex Desktop calling pull_handoff_context with cwd="/" or scratch path
        call_resp = mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "pull_handoff_context",
                    "arguments": {
                        "cwd": "/Users/developer/Documents/Codex/2026-10-14/hand",
                        "max_turns": "5",
                    },
                },
            }
        )
        assert call_resp is not None
        self.assertFalse(call_resp["result"]["isError"])
        text_out = call_resp["result"]["content"][0]["text"]
        self.assertIn("## 1. Distilled Handoff Summary", text_out)
        self.assertIn("## 5. Other Recent Sessions Across All Platforms", text_out)

    def test_watcher_sync_once(self) -> None:
        watcher = ContextWatcher(engine=self.engine)
        live_path = watcher.sync_once(cwd=self.workspace, force=True)
        assert live_path is not None
        self.assertTrue(os.path.isfile(live_path))

    def test_resolve_session_by_number_alias_and_fuzzy_query(self) -> None:
        sessions = self.engine.list_sessions(cwd=self.workspace)
        self.assertGreaterEqual(len(sessions), 4)

        # 1. Resolve by 1-based index ("1", "#2")
        s1 = self.engine.resolve_session("1", cwd=self.workspace)
        assert s1 is not None
        self.assertEqual(s1.session_id, sessions[0].session_id)

        s2 = self.engine.resolve_session("#2", cwd=self.workspace)
        assert s2 is not None
        self.assertEqual(s2.session_id, sessions[1].session_id)

        # 2. Resolve by agent alias ("cc", "agy", "codex")
        s_cc = self.engine.resolve_session("cc", cwd=self.workspace)
        assert s_cc is not None
        self.assertEqual(s_cc.agent, "claude")

        s_agy = self.engine.resolve_session("agy", cwd=self.workspace)
        assert s_agy is not None
        self.assertEqual(s_agy.agent, "antigravity")

        # 3. Resolve by fuzzy title / project / prompt keyword ("JWT Token", "SQLite")
        s_jwt = self.engine.resolve_session("JWT Token", cwd=self.workspace)
        assert s_jwt is not None
        self.assertEqual(s_jwt.agent, "claude")
        self.assertEqual(s_jwt.title, "JWT Token Parser & Refresh")

        s_sql = self.engine.resolve_session("SQLite query", cwd=self.workspace)
        assert s_sql is not None
        self.assertEqual(s_sql.agent, "codex")

        # 4. Extract bundle directly via query
        b_jwt = self.engine.extract_bundle(query="JWT Token", cwd=self.workspace)
        assert b_jwt is not None
        self.assertEqual(b_jwt.source_agent, "claude")

    def test_formatting_helpers(self) -> None:
        self.assertIn("CLAUDE", agent_badge("claude"))
        self.assertIn("CODEX", agent_badge("codex"))
        self.assertEqual(relative_time("invalid"), "unknown")
        self.assertTrue(shorten_path(os.path.expanduser("~/projects/app")).startswith("~/projects/app"))

    def test_mcp_server_query_parameter(self) -> None:
        mcp = MCPServer(engine=self.engine)
        call_resp = mcp.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "pull_handoff_context",
                    "arguments": {
                        "query": "JWT Token",
                    },
                },
            }
        )
        assert call_resp is not None
        self.assertFalse(call_resp["result"]["isError"])
        text_out = call_resp["result"]["content"][0]["text"]
        self.assertIn("JWT Token Parser & Refresh", text_out)

    def test_cross_platform_windows_linux_macos_paths_and_installer(self) -> None:
        from harness.installer import install_all
        from harness.platform_utils import is_pid_running

        # 1. Windows file:// URI decoding (file:///C:/Users/Alice/Repo -> C:/Users/Alice/Repo)
        win_norm = normalize_path("file:///C:/Users/Alice/My%20Repo").replace("\\", "/")
        self.assertEqual(win_norm, "C:/Users/Alice/My Repo")

        # 2. Case-insensitive and backslash/forward-slash Windows path matching
        self.assertTrue(paths_match("C:\\Users\\Alice\\My Repo", "c:/users/alice/my repo/src/main.py"))

        # 3. Cross-platform scratch / root CWD detection
        self.assertTrue(is_scratch_cwd("/"))
        self.assertTrue(is_scratch_cwd("C:\\"))
        self.assertTrue(is_scratch_cwd("C:/Windows/System32"))
        self.assertTrue(is_scratch_cwd("C:\\Users\\Alice\\Documents\\Codex\\2026-10-14\\hand"))

        # 4. Cross-platform PID check
        self.assertTrue(is_pid_running(os.getpid()))
        self.assertFalse(is_pid_running(-999))

        # 5. Test Windows-mode and POSIX-mode installer in isolated temp home directory
        win_home = os.path.join(self.root, "fake_win_home")
        res_win = install_all(home_dir=win_home, is_windows=True)
        self.assertTrue(any(p.endswith("harness.cmd") for p in res_win["cli_binaries"]))
        self.assertTrue(any(p.endswith("harness.ps1") for p in res_win["cli_binaries"]))
        self.assertTrue(any(p.endswith("one-agent-harness-mcp.cmd") for p in res_win["cli_binaries"]))

        # Verify Codex config.toml uses forward slashes (safe on Windows)
        toml_path = os.path.join(win_home, ".codex", "config.toml")
        with open(toml_path, "r", encoding="utf-8") as fp:
            toml_content = fp.read()
        self.assertIn("[mcp_servers.one-agent-harness]", toml_content)
        self.assertNotIn("\\U", toml_content)


if __name__ == "__main__":
    unittest.main()
