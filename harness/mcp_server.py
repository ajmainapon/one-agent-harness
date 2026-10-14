"""Model Context Protocol (MCP) STDIO Server for One Agent Harness."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

from harness import __version__
from harness.engine import HarnessEngine


def _safe_int(val: Any, default: int, min_val: int = 1, max_val: int = 200) -> int:
    if val is None:
        return default
    try:
        parsed = int(val)
        return max(min_val, min(parsed, max_val))
    except (ValueError, TypeError):
        return default


TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "name": "pull_handoff_context",
        "description": (
            "Pull the exact context (verbatim recent conversation turns, implementation plans, "
            "task checklists, modified/read files, terminal commands/errors, and git state) "
            "from the user's previous session in Claude Code, Antigravity, Codex, or Grok Build CLI. "
            "Also returns a snapshot of the latest active sessions across all other platforms. "
            "Call this whenever the user says '/handoff', '$handoff', 'continue from my last session', "
            "or switches from another agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional topic, project name (e.g. 'auth refactor', 'api-server', 'frontend'), session number ('1', '2'), or agent name ('claude', 'codex') to match.",
                },
                "source_agent": {
                    "type": "string",
                    "description": "Optional source platform ('claude', 'antigravity', 'codex', 'grok'). Omit to auto-detect the most recently active session.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional specific session ID prefix, index number ('1', '2'), or fuzzy query to pull.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional workspace path to match sessions for the current project.",
                },
                "exclude_agent": {
                    "type": "string",
                    "description": "Optional current agent name to exclude when auto-detecting the previous agent.",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Number of verbatim turns to include (default: 25).",
                    "default": 25,
                },
            },
        },
    },
    {
        "name": "list_recent_sessions",
        "description": (
            "List recent coding sessions across Claude Code, Antigravity, Codex, and Grok Build CLI "
            "with session IDs, titles, working directories, turn counts, and timestamps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Optional filter by platform ('claude', 'antigravity', 'codex', 'grok').",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional workspace directory filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum sessions to return (default: 15).",
                    "default": 15,
                },
            },
        },
    },
    {
        "name": "search_cross_agent_history",
        "description": (
            "Search across all 4 agents' conversation transcripts, plans, and modified file paths "
            "for a keyword, error message, or filename."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword, filename, or phrase to search across all agents.",
                },
                "agent": {
                    "type": "string",
                    "description": "Optional agent filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum matches to return (default: 10).",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "save_checkpoint_note",
        "description": (
            "Save an explicit checkpoint summary and next steps so the next agent you open "
            "receives it at the top of its handoff context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Summary of what was accomplished, architectural decisions, and current state.",
                },
                "next_steps": {
                    "type": "string",
                    "description": "Immediate next steps or open blockers for the next agent.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Workspace directory path.",
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "inject_session_to_agent",
        "description": (
            "Convert and inject the latest context bundle directly into a target agent's native "
            "session store ('claude', 'codex', 'antigravity', or 'grok') so it can be resumed natively."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to_agent": {
                    "type": "string",
                    "description": "Target agent ('claude', 'codex', 'antigravity', 'grok').",
                },
                "from_agent": {
                    "type": "string",
                    "description": "Optional source agent.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Optional source session ID.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional workspace directory.",
                },
            },
            "required": ["to_agent"],
        },
    },
]


class MCPServer:
    """JSON-RPC 2.0 STDIO MCP Server."""

    def __init__(self, engine: Optional[HarnessEngine] = None) -> None:
        self.engine = engine or HarnessEngine()

    def handle_request(self, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params") or {}

        if req_id is None:
            return None

        try:
            if method == "initialize":
                client_proto = params.get("protocolVersion") or "2024-11-05"
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": client_proto,
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "one-agent-harness",
                            "version": __version__,
                        },
                    },
                }
            if method in ("ping", "logging/setLevel"):
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}
            if method == "completion/complete":
                return {"jsonrpc": "2.0", "id": req_id, "result": {"completion": {"values": []}}}
            if method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": TOOLS_SCHEMA},
                }
            if method == "tools/call":
                tool_name = params.get("name", "")
                args = params.get("arguments") or {}
                text_out = self._call_tool(tool_name, args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": text_out}],
                        "isError": False,
                    },
                }
            if method in ("resources/list", "prompts/list", "resources/templates/list"):
                if "templates" in method:
                    key = "resourceTemplates"
                elif "resources" in method:
                    key = "resources"
                else:
                    key = "prompts"
                return {"jsonrpc": "2.0", "id": req_id, "result": {key: []}}

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:
            if method == "tools/call":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error executing tool: {exc}"}],
                        "isError": True,
                    },
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }

    def _call_tool(self, name: str, args: Dict[str, Any]) -> str:
        from harness.engine import is_scratch_cwd

        if name == "pull_handoff_context":
            source_agent = args.get("source_agent")
            session_id = args.get("session_id")
            query = args.get("query")
            raw_cwd = args.get("cwd")
            cwd = str(raw_cwd).strip() if (raw_cwd and not is_scratch_cwd(str(raw_cwd))) else None
            exclude_agent = args.get("exclude_agent")
            max_turns = _safe_int(args.get("max_turns"), default=25, min_val=1, max_val=200)

            bundle = self.engine.extract_bundle(
                source_agent=source_agent,
                session_id=session_id,
                cwd=cwd,
                exclude_agent=exclude_agent,
                max_turns=max_turns,
                query=query,
            )
            if not bundle:
                return "No sessions found across Claude Code, Antigravity, Codex, or Grok Build CLI."

            self.engine.sync_workspace_context(bundle, target_cwd=cwd or bundle.cwd, update_agent_md=False)
            return bundle.to_markdown(include_full_artifacts=True, max_turns=max_turns)

        if name == "list_recent_sessions":
            raw_cwd = args.get("cwd")
            cwd = str(raw_cwd).strip() if (raw_cwd and not is_scratch_cwd(str(raw_cwd))) else None
            sessions = self.engine.list_sessions(
                agent=args.get("agent"),
                cwd=cwd,
                limit=_safe_int(args.get("limit"), default=15, min_val=1, max_val=100),
            )
            if not sessions:
                return "No matching sessions found."
            lines = ["# Recent Sessions Across All Agents\n"]
            for idx, s in enumerate(sessions, 1):
                lines.append(
                    f"{idx}. **[{s.agent.upper()}]** `{s.session_id}` — **{s.title}**\n"
                    f"   - Updated: `{s.updated_at}` | Turns: `{s.turn_count}` | CWD: `{s.cwd or 'N/A'}`"
                )
            return "\n".join(lines)

        if name == "search_cross_agent_history":
            query = str(args.get("query") or "")
            matches = self.engine.search_history(
                query=query,
                agent=args.get("agent"),
                limit=_safe_int(args.get("limit") or 10, default=10, min_val=1, max_val=50),
            )
            if not matches:
                return f"No matches found for '{query}' across Claude Code, Antigravity, Codex, or Grok."
            lines = [f"# Cross-Agent Search Results for `{query}`\n"]
            for idx, m in enumerate(matches, 1):
                lines.append(
                    f"{idx}. **[{m['agent'].upper()}]** `{m['session_id']}` — **{m['title']}** ({m['updated_at']})\n"
                    f"   - CWD: `{m['cwd']}`\n"
                    f"   - Match ({m['match_type']}): {m['snippet']}"
                )
            return "\n".join(lines)

        if name == "save_checkpoint_note":
            entry = self.engine.save_checkpoint(
                summary=str(args.get("summary") or ""),
                next_steps=str(args.get("next_steps") or ""),
                cwd=args.get("cwd") or os.getcwd(),
            )
            return f"Checkpoint saved at {entry['timestamp']} for workspace `{entry['cwd']}`."

        if name == "inject_session_to_agent":
            res = self.engine.switch_session(
                to_agent=str(args.get("to_agent")),
                from_agent=args.get("from_agent"),
                session_id=args.get("session_id"),
                cwd=args.get("cwd"),
                query=args.get("query"),
            )
            cmd_str = " ".join(res["launch_cmd"])
            return (
                f"Successfully converted `{res['source_agent']}` session `{res['source_session_id']}` "
                f"into `{res['target_agent']}` native session `{res['injected_session_id']}`.\n"
                f"- Live Context File: `{res['live_context_path']}`\n"
                f"- Resume Command: `{cmd_str}`"
            )

        raise ValueError(f"Unknown tool: {name}")

    def serve_stdio(self) -> None:
        """Run the STDIO JSON-RPC loop supporting both line-delimited and Content-Length framing."""
        stdin = sys.stdin.buffer
        stdout = sys.stdout.buffer
        # Redirect sys.stdout to stderr so stray print() calls never corrupt STDIO JSON-RPC
        sys.stdout = sys.stderr

        while True:
            first_line = stdin.readline()
            if not first_line:
                break

            stripped = first_line.strip()
            if not stripped:
                continue

            use_content_length = False
            if stripped.lower().startswith(b"content-length:"):
                use_content_length = True
                try:
                    length = int(stripped.split(b":", 1)[1].strip())
                except ValueError:
                    continue
                while True:
                    hdr = stdin.readline()
                    if not hdr or hdr.strip() == b"":
                        break
                body = stdin.read(length)
                raw_text = body.decode("utf-8", errors="replace")
            else:
                raw_text = stripped.decode("utf-8", errors="replace")

            try:
                req = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            if not isinstance(req, dict):
                continue

            resp = self.handle_request(req)
            if resp is not None:
                payload_bytes = json.dumps(resp).encode("utf-8")
                if use_content_length:
                    header = f"Content-Length: {len(payload_bytes)}\r\n\r\n".encode("ascii")
                    stdout.write(header + payload_bytes)
                else:
                    stdout.write(payload_bytes + b"\n")
                stdout.flush()


def main() -> None:
    MCPServer().serve_stdio()


if __name__ == "__main__":
    main()
