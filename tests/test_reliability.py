"""Regression tests for source selection, lossless prompts, and safe handoffs."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_harness as fixtures
from harness.cli import main
from harness.installer import install_all
from harness.mcp_server import MCPServer
from harness.models import ContextBundle, Turn
from harness.platform_utils import atomic_write_text
from harness.watcher import ContextWatcher


class TestHandoffReliability(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TestOneAgentHarness()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.engine = self.fixture.engine
        self.root = Path(self.fixture.root)
        self.workspace = Path(self.fixture.workspace)

    def cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch('harness.cli.HarnessEngine', return_value=self.engine), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def pull(self, **args):
        return MCPServer(self.engine).handle_request({
            'id': 1, 'method': 'tools/call',
            'params': {'name': 'pull_handoff_context', 'arguments': args},
        })['result']

    def seed_other_project(self, index, mtime=None):
        workspace = self.root / f'project-{index}'
        workspace.mkdir(exist_ok=True)
        path = Path(self.fixture.grok_home) / 'sessions' / f'other-{index}.jsonl'
        path.write_text(json.dumps({'role': 'user', 'content': f'Build project {index}', 'cwd': str(workspace)}) + '\n')
        stamp = mtime or time.time() + index + 10
        os.utime(path, (stamp, stamp))
        return workspace, path

    def test_failed_query_never_falls_back_to_latest_session(self):
        for args in [
            {'source_agent': 'claude', 'query': 'nonexistent-project-xyz'},
            {'source_agent': 'claude', 'session_id': 'missing-id'},
            {'source_agent': 'claude', 'query': '999999999'},
            {'source_agent': 'claude', 'query': '0'},
            {'source_agent': 'claude', 'query': 'nonexistent-project-xyz',
             'session_id': '11111111-2222-3333-4444-555555555555'},
        ]:
            with self.subTest(args=args):
                self.assertIsNone(self.engine.extract_bundle(**args))
        result = self.pull(source_agent='claude', query='nonexistent-project-xyz')
        self.assertTrue(result['isError'])
        self.assertIn('list_recent_sessions', result['content'][0]['text'])
        with patch('harness.cli._copy_to_clipboard') as clipboard:
            code, out, err = self.cli('handoff', 'nonexistent-project-xyz', '--from', 'claude')
        self.assertEqual(code, 1)
        self.assertIn('harness status', err)
        self.assertFalse((self.workspace / '.harness').exists())
        clipboard.assert_not_called()

    def test_numbers_and_keywords_use_displayed_scope(self):
        other, _ = self.seed_other_project(1)
        for scope in [{'cwd': str(self.workspace)}, {'agent': 'claude'}, {'cwd': str(self.workspace), 'agent': 'grok'}]:
            listed = self.engine.list_sessions(**scope)
            for index, session in enumerate(listed, 1):
                selected = self.engine.resolve_session(str(index), cwd=scope.get('cwd'), source_agent=scope.get('agent'))
                self.assertEqual((selected.agent, selected.session_id), (session.agent, session.session_id))
        self.assertIsNone(self.engine.extract_bundle(query='Build project 1', cwd=str(self.workspace)))
        self.assertIsNone(self.engine.extract_bundle(source_agent='claude', cwd=str(other)))
        self.assertIsNone(self.engine.extract_bundle(query='1', cwd=str(self.root / 'missing')))

    def test_workspace_write_failure_falls_back_and_reports_path(self):
        actual_write = atomic_write_text
        def deny_workspace(path, content):
            if str(path).startswith(str(self.workspace / '.harness')):
                raise PermissionError('workspace is read-only')
            actual_write(path, content)
        with patch('harness.engine.atomic_write_text', side_effect=deny_workspace):
            code, out, err = self.cli('handoff', '--from', 'claude', '--no-copy')
        self.assertEqual(code, 0)
        fallback = Path(self.fixture.harness_home) / 'LIVE_CONTEXT.md'
        self.assertTrue(fallback.is_file())
        self.assertIn(str(fallback), out)
        self.assertIn('read-only', err)
        self.assertIn('JWT', fallback.read_text())

    def test_complete_write_failure_does_not_block_cli_or_mcp_retrieval(self):
        with patch('harness.engine.atomic_write_text', side_effect=PermissionError('disk unavailable')):
            result = self.pull(source_agent='claude')
            code, out, err = self.cli('handoff', '--from', 'claude', '--no-copy')
        self.assertFalse(result['isError'])
        self.assertIn('could not be saved', result['content'][0]['text'])
        self.assertIn('JWT', result['content'][0]['text'])
        self.assertEqual(code, 0)
        self.assertIn('JWT', out)
        self.assertIn('could not be saved', err)

    def test_global_copy_failure_does_not_invalidate_workspace_save(self):
        actual_write = atomic_write_text
        def deny_global(path, content):
            if str(path).startswith(self.fixture.harness_home):
                raise PermissionError('global directory unavailable')
            actual_write(path, content)
        bundle = self.engine.extract_bundle(source_agent='claude')
        with patch('harness.engine.atomic_write_text', side_effect=deny_global):
            path = self.engine.sync_workspace_context(bundle)
        self.assertTrue(Path(path).is_file())
        self.assertIn('global copy', self.engine.sync_warnings[0])

    def test_read_only_mode_leaves_files_and_clipboard_untouched(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('harness.cli._copy_to_clipboard') as clipboard:
            code, out, err = self.cli('handoff', '--from', 'claude', '--read-only')
        after = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(code, 0)
        self.assertIn('JWT', out)
        self.assertEqual(before, after)
        clipboard.assert_not_called()

    def test_dry_run_does_not_write_or_copy(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('harness.cli._copy_to_clipboard') as clipboard:
            code, out, err = self.cli('codex', '--from', 'claude', '--dry-run')
        after = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(code, 0, err)
        self.assertIn('Preview', out)
        self.assertNotIn('Resume Command', out)
        self.assertEqual(before, after)
        clipboard.assert_not_called()

    def test_watcher_updates_all_workspaces_and_only_changed_ones(self):
        projects = [self.seed_other_project(i) for i in range(1, 8)]
        watcher = ContextWatcher(self.engine)
        paths = watcher.sync_changed_workspaces()
        self.assertEqual(len(paths), 8)  # Original workspace plus seven others.
        for workspace, _ in projects:
            self.assertTrue((workspace / '.harness' / 'LIVE_CONTEXT.md').is_file())
        self.assertTrue((self.workspace / '.harness' / 'LIVE_CONTEXT.md').is_file())
        self.assertEqual(watcher.sync_changed_workspaces(), [])
        global_path = Path(self.fixture.harness_home) / 'LIVE_CONTEXT.md'
        global_before = global_path.read_text()
        workspace, source = projects[0]
        stamp = source.stat().st_mtime + .25  # Changed, but still not globally newest.
        with source.open('a') as fp:
            fp.write(json.dumps({'role': 'assistant', 'content': 'New progress sentinel'}) + '\n')
        os.utime(source, (stamp, stamp))
        paths = watcher.sync_changed_workspaces()
        self.assertEqual(paths, [str(workspace / '.harness' / 'LIVE_CONTEXT.md')])
        self.assertIn('New progress sentinel', Path(paths[0]).read_text())
        self.assertEqual(global_before, global_path.read_text())

    def test_watcher_continues_after_one_workspace_fails(self):
        bad, _ = self.seed_other_project(1)
        good, _ = self.seed_other_project(2)
        original = self.engine.sync_workspace_context
        def fail_one(bundle, **kwargs):
            if kwargs['target_cwd'] == str(bad):
                raise PermissionError('temporarily unavailable')
            return original(bundle, **kwargs)
        watcher = ContextWatcher(self.engine)
        with patch.object(self.engine, 'sync_workspace_context', side_effect=fail_one), redirect_stderr(io.StringIO()):
            watcher.sync_changed_workspaces()
        self.assertTrue((good / '.harness' / 'LIVE_CONTEXT.md').exists())
        self.assertNotIn(str(bad), watcher.last_seen)
        self.assertIn(str(bad / '.harness' / 'LIVE_CONTEXT.md'), watcher.sync_changed_workspaces())

    def test_older_workspace_fallback_does_not_replace_latest_global_context(self):
        old, _ = self.seed_other_project(1)
        self.seed_other_project(2)
        watcher = ContextWatcher(self.engine)
        watcher.sync_changed_workspaces()
        global_path = Path(self.fixture.harness_home) / 'LIVE_CONTEXT.md'
        original = global_path.read_text()
        watcher.last_seen.pop(str(old))
        actual_write = atomic_write_text
        def deny_old(path, content):
            if str(path).startswith(str(old)):
                raise PermissionError('workspace unavailable')
            actual_write(path, content)
        with patch('harness.engine.atomic_write_text', side_effect=deny_old), redirect_stderr(io.StringIO()):
            paths = watcher.sync_changed_workspaces()
        self.assertEqual(len(paths), 1)
        self.assertIn('workspaces', paths[0])
        self.assertEqual(original, global_path.read_text())
        self.assertNotIn(str(old), watcher.last_seen)

    def test_only_session_referenced_claude_plans_are_attached(self):
        adapter = self.engine.adapters['claude']
        bundle = adapter.extract_bundle()
        self.assertEqual(bundle.artifacts_and_plans, [])  # Even the same-day fixture plan isn't referenced.
        plan_dir = Path(self.fixture.claude_home) / 'plans'
        referenced = plan_dir / 'old-referenced-plan.md'
        referenced.write_text('This is the actual plan for this session.')
        os.utime(referenced, (1, 1))
        for i in range(3):
            (plan_dir / f'unrelated-{i}.md').write_text('Unrelated project')
        with Path(bundle.source_path).open('a') as fp:
            fp.write(json.dumps({'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': str(referenced)}}
            ]}}) + '\n')
        result = adapter.extract_bundle(session_id=bundle.session_id)
        self.assertEqual([a.path for a in result.artifacts_and_plans], [str(referenced)])

    def test_long_user_requirements_survive_render_and_native_handoffs(self):
        prompt = 'a' * 2000 + 'CRITICAL_REQUIREMENT_SENTINEL' + 'b' * 5000
        bundle = ContextBundle(source_agent='grok', session_id='test', title='Long instructions',
                               cwd=str(self.workspace), updated_at='', turns=[Turn(role='user', content=prompt)],
                               initial_goal=prompt, latest_user_request=prompt)
        self.assertIn(prompt, bundle.to_markdown())
        for agent in ['claude', 'codex', 'grok']:
            adapter = self.engine.adapters[agent]
            session_id = adapter.inject_session(bundle)
            imported = adapter.extract_bundle(session_id=session_id)
            self.assertIn(prompt, imported.turns[0].content)
        self.engine.adapters['antigravity'].inject_session(bundle)
        self.assertIn(prompt, (self.workspace / '.harness' / 'LIVE_CONTEXT.md').read_text())

    def test_installer_rejects_malformed_configs_without_changing_anything(self):
        for name in ['.claude.json', '.gemini/config/mcp_config.json', '.grok/mcp.json']:
            with self.subTest(config=name):
                home = self.root / ('install-' + name.replace('/', '-'))
                config = home / name
                config.parent.mkdir(parents=True)
                original = '{"mcpServers":{"existing":{"command":"tool"}},}'
                config.write_text(original)
                with self.assertRaisesRegex(ValueError, 'left unchanged'):
                    install_all(home_dir=str(home))
                self.assertEqual(config.read_text(), original)
                self.assertFalse((home / '.local').exists())
                self.assertFalse((home / '.harness').exists())

    def test_installer_preserves_and_backs_up_grok_config(self):
        home = self.root / 'install-home'
        config = home / '.grok' / 'mcp.json'
        config.parent.mkdir(parents=True)
        original = '{"setting": 42, "mcpServers": {"existing": {"command": "tool"}}}'
        config.write_text(original)
        result = install_all(home_dir=str(home))
        updated = json.loads(config.read_text())
        self.assertEqual(updated['setting'], 42)
        self.assertEqual(updated['mcpServers']['existing'], {'command': 'tool'})
        self.assertIn('one-agent-harness', updated['mcpServers'])
        backups = [Path(p) for p in result['backups'] if p.startswith(str(config))]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        again = install_all(home_dir=str(home))
        self.assertTrue(set(result['backups']).isdisjoint(again['backups']))

    def test_clipboard_failure_is_not_reported_as_success(self):
        with patch('harness.cli._copy_to_clipboard', return_value=False):
            code, out, err = self.cli('codex', '--from', 'claude', '--no-launch')
        self.assertEqual(code, 0)
        self.assertNotIn('Copied context', out)
        self.assertIn('Clipboard unavailable', err)

    def test_cli_validates_numeric_options(self):
        for args in [('watch', '--interval', '0'), ('watch', '--interval', 'nan'),
                     ('status', '--limit', '-1'), ('handoff', '--max-turns', '0')]:
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                self.cli(*args)
            self.assertEqual(caught.exception.code, 2)

    def test_no_update_agent_md_flag_preserves_instructions(self):
        instructions = self.workspace / 'AGENTS.md'
        original = instructions.read_bytes()
        code, out, err = self.cli('handoff', '--from', 'claude', '--no-copy', '--no-update-agent-md')
        self.assertEqual(code, 0, err)
        self.assertEqual(instructions.read_bytes(), original)

    def test_cli_and_mcp_process_entrypoints(self):
        env = os.environ.copy()
        env.update({
            'HARNESS_CLAUDE_HOME': self.fixture.claude_home,
            'HARNESS_AGY_HOME': self.fixture.agy_home,
            'HARNESS_CODEX_HOME': self.fixture.codex_home,
            'HARNESS_GROK_HOME': self.fixture.grok_home,
            'HARNESS_HOME': self.fixture.harness_home,
        })
        repo = str(Path(__file__).resolve().parents[1])
        for args in [['--version'], ['handoff', '--help'],
                     ['handoff', '--from', 'claude', '--read-only']]:
            result = subprocess.run([sys.executable, '-m', 'harness'] + args,
                                    cwd=repo, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip())
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'pull_handoff_context', 'arguments': {'source_agent': 'claude'}}}
        result = subprocess.run([sys.executable, '-m', 'harness.mcp_server'], cwd=repo, env=env,
                                input=json.dumps(request) + '\n', capture_output=True, text=True, timeout=20)
        response = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(response['result']['isError'])
        self.assertIn('JWT', response['result']['content'][0]['text'])

    def test_atomic_write_failure_preserves_existing_file(self):
        target = self.root / 'existing.txt'
        target.write_text('original')
        with patch('harness.platform_utils.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                atomic_write_text(str(target), 'replacement')
        self.assertEqual(target.read_text(), 'original')
        self.assertEqual(list(self.root.glob('.harness-*')), [])

    def test_unchanged_session_files_are_cached_and_fallback_workspaces_do_not_loop(self):
        # 1. Verify unchanged files are not re-opened on subsequent list_sessions() calls
        first = self.engine.list_sessions(limit=None)
        self.assertTrue(first)
        with patch('builtins.open', side_effect=AssertionError('Unchanged files must not be re-opened')):
            second = self.engine.list_sessions(limit=None)
        self.assertEqual([s.session_id for s in first], [s.session_id for s in second])

        # 2. Verify unavailable workspace only writes fallback once until folder reappears
        missing_ws = self.root / 'unmounted-drive-project'
        path = Path(self.fixture.grok_home) / 'sessions' / 'unmounted.jsonl'
        path.write_text(json.dumps({'role': 'user', 'content': 'Work on external drive', 'cwd': str(missing_ws)}) + '\n')
        os.utime(path, (100, 100))
        watcher = ContextWatcher(self.engine)
        with redirect_stderr(io.StringIO()):
            first_sync = watcher.sync_changed_workspaces()
            second_sync = watcher.sync_changed_workspaces()
        self.assertTrue(any('workspaces' in p for p in first_sync))
        self.assertEqual(second_sync, [])

        # When the drive is mounted again, watcher automatically syncs to the real workspace
        missing_ws.mkdir(exist_ok=True)
        third_sync = watcher.sync_changed_workspaces()
        self.assertEqual(third_sync, [str(missing_ws / '.harness' / 'LIVE_CONTEXT.md')])


if __name__ == '__main__':
    unittest.main()
