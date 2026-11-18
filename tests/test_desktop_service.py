from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import desktop_service as service
import porter
import self_test
import migrate
from filesystem import Path as LongPath, extended
from adapters import antigravity


class DesktopWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec, self.expected = self_test.fixture(self.root)
        self.config = antigravity.initialize_sandbox(self.root / 'target')
        self.output = self.root / 'output'

    def test_catalog_recognizes_compressed_rollout_without_reading_content(self):
        ident = self.spec['projects'][0]['threads'][0]
        log = Path(self.spec['codex_home']) / 'sessions' / (ident + '.jsonl')
        compressed = Path(str(log) + '.zst')
        compressed.write_bytes(b'format checked only on export')
        log.unlink()
        projects = service.source_projects(self.spec['codex_home'])
        chat = next(t for p in projects for t in p['threads'] if t['id'] == ident)
        self.assertTrue(chat['available'])
        self.assertTrue(chat['compressed'])
        spec = service.selection_spec(self.spec['codex_home'], projects, {p['path'] for p in projects}, {ident})
        self.assertEqual(sum(len(p['threads']) for p in spec['projects']), 1)
        self.assertFalse(spec['unavailable_chats'])

    def test_missing_chat_is_visible_and_cannot_be_exported(self):
        ident = self.spec['projects'][0]['threads'][0]
        log = Path(self.spec['codex_home']) / 'sessions' / (ident + '.jsonl')
        log.unlink()
        projects = service.source_projects(self.spec['codex_home'])
        chat = next(t for p in projects for t in p['threads'] if t['id'] == ident)
        self.assertFalse(chat['available'])
        self.assertEqual(Path(chat['rollout_path']).resolve(), log.resolve())
        roots = {p['path'] for p in projects}
        files_only = service.selection_spec(self.spec['codex_home'], projects, roots, {ident})
        self.assertEqual(sum(len(p['threads']) for p in files_only['projects']), 0)
        self.assertEqual(files_only['unavailable_chats'][0]['id'], ident)
        good = {t['id'] for p in projects for t in p['threads'] if t['available']}
        spec = service.selection_spec(self.spec['codex_home'], projects, roots, good)
        self.assertEqual(sum(len(p['threads']) for p in spec['projects']), 3)
        result = service.prepare(self.output, 'portable', selection=spec)
        self.assertEqual(result['status'], 'portable-restored')
        self.assertIn(ident, result['summary'])
        plan = porter.read_json(Path(result['plan']))
        self.assertTrue(all((Path(p['workspace']) / 'settings.json').is_file() for p in plan['projects']))
        self.assertEqual(sum(len(p['conversations']) for p in plan['projects']), 3)
        error = FileNotFoundError(2, 'not found', str(log))
        self.assertIn('重新启动 Antigravity 无法恢复', service.explain_error(error))

    def test_app_controls_scope_processes_and_only_launch_after_success(self):
        from types import SimpleNamespace
        install = self.root / "App's folder"
        install.mkdir()
        (install / 'Antigravity.exe').write_bytes(b'fixture only')
        config = {'app_root': str(install)}
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stderr='')) as run, patch('subprocess.Popen') as launch:
            service.app_action(config, 'close')
            launch.assert_not_called()
            script = run.call_args.args[0][-1]
            self.assertIn("App''s folder", script)
            self.assertIn('SessionId -eq $session', script)
            self.assertIn('StartsWith($root', script)
            self.assertIn('CloseMainWindow', script)
            service.app_action(config, 'restart')
            launch.assert_called_once()
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=1, stderr='denied')), patch('subprocess.Popen') as launch:
            with self.assertRaises(porter.PorterError):
                service.app_action(config, 'restart')
            launch.assert_not_called()

    def test_defaults_follow_each_users_profile_and_custom_environment(self):
        for user in ('alice', '另一个用户'):
            home = self.root / user
            settings = service.defaults(home, {})
            self.assertEqual(Path(settings['home']), home)
            self.assertTrue(all(Path(value).is_relative_to(home) for value in settings.values()))
        overrides = service.defaults(self.root, {'CODEX_HOME': str(self.root / 'custom-codex'),
                                                'APPDATA': str(self.root / 'custom-appdata')})
        self.assertEqual(overrides['codex_home'], str(self.root / 'custom-codex'))
        self.assertEqual(Path(overrides['ui']), self.root / 'custom-appdata/Antigravity')

    def test_selected_conversation_subset_and_files_only_project(self):
        projects = service.source_projects(self.spec['codex_home'])
        roots = {p['path'] for p in projects}
        one = projects[0]['threads'][0]['id']
        selection = service.selection_spec(self.spec['codex_home'], projects, roots, {one})
        result = service.prepare(self.output, 'portable', selection=selection)
        plan = porter.read_json(Path(result['plan']))
        self.assertEqual([c['source_id'] for p in plan['projects'] for c in p['conversations']], [one])
        self.assertEqual(len(plan['projects']), 2)
        self.assertTrue(all((Path(p['workspace']) / 'settings.json').is_file() for p in plan['projects']))

    def test_blocked_native_preview_can_retry_same_output_after_acceptance(self):
        first = self.spec['projects'][0]['threads'][0]
        log = Path(self.spec['codex_home']) / 'sessions' / (first + '.jsonl')
        with log.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'test'}}) + '\n')
        result = service.prepare(self.output, antigravity.ADAPTER, self.config, selection=self.spec)
        self.assertEqual(result['status'], 'blocked')
        self.assertFalse((self.output / 'workspaces').exists())
        retry = service.prepare(self.output, antigravity.ADAPTER, self.config, existing_job=result['job'], allow_lossy=True)
        self.assertEqual(retry['status'], 'ready-to-apply')
        self.assertEqual(service.apply(Path(retry['plan']))['status'], 'installed')
        self.assertIn('仅存档', retry['summary'])

    def test_output_cannot_overlap_source_project(self):
        project = Path(self.spec['projects'][0]['path'])
        with self.assertRaisesRegex(porter.PorterError, '重叠'):
            service.prepare(project / 'output', 'portable', selection=self.spec)
        self.assertFalse((project / 'output').exists())

    def test_missing_or_ambiguous_inputs_rejected(self):
        for inputs in ({}, {'selection': self.spec, 'existing_job': self.root / 'fake.json'}):
            with self.assertRaises(porter.PorterError):
                service.prepare(self.output, 'portable', **inputs)
        self.assertFalse(self.output.exists())

    def test_profile_catalog_grouping_does_not_duplicate_nested_cwds(self):
        home = Path(self.spec['codex_home'])
        parent = Path(self.spec['projects'][0]['path'])
        child = parent / 'src'
        child.mkdir()
        ident = self.spec['projects'][0]['threads'][0]
        with closing(sqlite3.connect(home / 'state_7.sqlite')) as con:
            con.execute('UPDATE threads SET cwd=? WHERE id=?', (str(child), ident))
            con.commit()
        groups = service.source_projects(home)
        self.assertEqual(len(groups), 2)
        self.assertEqual(sum(len(p['threads']) for p in groups), 4)

    def test_checkpoint_preserves_committed_wal_rows_and_backups(self):
        layout = antigravity.paths(Path(self.config['home']), Path(self.config['ui']))
        script = "import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL'); c.execute('INSERT INTO conversation_summaries(conversation_id,title) VALUES(?,?)',('wal-case','keep')); c.commit(); os._exit(0)"
        subprocess.run([sys.executable, '-c', script, str(layout['summary'])], check=True)
        self.assertTrue(Path(str(layout['summary']) + '-wal').exists())
        result = service.checkpoint(self.config, self.output)
        self.assertTrue(result['summary_rows_preserved'])
        self.assertEqual(result['summary_row_count'], 1)
        self.assertTrue((Path(result['backup']) / 'conversation_summaries.db-wal').is_file())
        antigravity.guard(self.config)
        with closing(sqlite3.connect(layout['summary'])) as con:
            self.assertEqual(con.execute('SELECT title FROM conversation_summaries').fetchall(), [('keep',)])

    def test_user_facing_errors_explain_action(self):
        self.assertIn('可移植模式', service.explain_error(RuntimeError('Unsupported installed build')))
        self.assertIn('退出', service.explain_error(RuntimeError('Close Antigravity')))

    def test_cli_report_can_be_used_without_console(self):
        report = self.root / 'version.txt'
        self.assertEqual(app.main(['--version', '--report', str(report)]), 0)
        self.assertEqual(report.read_text(encoding='utf-8').strip(), service.VERSION)
        with self.assertRaises(FileExistsError):
            app.main(['--version', '--report', str(report)])

    def test_cli_custom_home_does_not_mix_current_users_ui_profile(self):
        other = self.root / 'other-account'
        with patch.object(migrate, 'make_plan', return_value={'status': 'ready-to-apply'}) as planned:
            code = migrate.main(['plan', '--job', str(self.root / 'unused.json'), '--to', str(self.output),
                                 '--out', str(self.root / 'run'), '--target', antigravity.ADAPTER,
                                 '--target-home', str(other)])
        self.assertEqual(code, 0)
        self.assertEqual(Path(planned.call_args.args[4]['ui']), other / 'AppData/Roaming/Antigravity')

    def test_shipped_self_test_uses_generated_data(self):
        result = self_test.run()
        self.assertTrue(result['passed'])
        self.assertEqual(result['projects'], 2)
        self.assertEqual(result['conversations'], 4)
        self.assertFalse(result['real_profiles_read'])
        self.assertFalse(result['native_client_started'])

    @unittest.skipUnless(os.name == 'nt', 'Windows extended path behavior')
    def test_deep_paths_work_without_global_windows_long_path_setting(self):
        with tempfile.TemporaryDirectory(prefix='carry-long-', dir=extended(tempfile.gettempdir(), force=True)) as temporary:
            root = LongPath(temporary) / ('a' * 70) / ('b' * 70) / ('c' * 70)
            root.mkdir(parents=True)
            spec, expected = self_test.fixture(root)
            target = antigravity.initialize_sandbox(root / 'target')
            result = service.prepare(root / 'output', antigravity.ADAPTER, target, selection=spec)
            self.assertEqual(service.apply(LongPath(result['plan']))['status'], 'installed')
            plan = porter.read_json(LongPath(result['plan']))
            self.assertTrue(all(len(p['workspace']) > 260 for p in plan['projects']))
            self.assertTrue(all(not p['workspace'].startswith('\\\\?\\') for p in plan['projects']))
            self.assertTrue(all('/%3F/' not in LongPath(p['workspace']).as_uri() for p in plan['projects']))
            self.assertEqual(service.recover(LongPath(result['plan']))['status'], 'rolled-back')


if __name__ == '__main__':
    unittest.main()
