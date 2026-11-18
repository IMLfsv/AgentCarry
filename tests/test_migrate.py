"""Batch behavior tests using fresh IDs, accounts and paths on every run."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate
import porter
import publication
import transfer
from adapters import antigravity as target
from adapters import antigravity_codec as codec


class BatchMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='porter-batch-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / ('account-' + uuid.uuid4().hex) / 'codex'
        (self.home / 'sessions').mkdir(parents=True)
        self.source_roots, self.ids, self.expected = [], [], {}
        self.catalog = self.home / 'state_12.sqlite'  # Not pinned to the original sample's state_5.
        with closing(sqlite3.connect(self.catalog)) as db:
            db.execute('CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT,title TEXT,history_mode TEXT)')
            for pi in range(3):
                project = self.root / f'source-{pi}' / '同名 project'
                project.mkdir(parents=True)
                (project / '代码.txt').write_text('源项目 ' + str(pi), encoding='utf-8')
                (project / 'AGENTS.md').write_text('Use project conventions.\n')
                self.source_roots.append(project)
                for ci in range(0 if pi == 2 else 2):
                    cid = str(uuid.uuid4())
                    mode = 'legacy' if pi == 0 else 'paginated'
                    path = self.home / 'sessions' / ('rollout-' + cid + '.jsonl')
                    records = [{'type': 'session_meta', 'ordinal': 0,
                                'payload': {'id': cid, 'cwd': str(project), 'history_mode': mode}}]
                    messages = [{'role': 'user', 'text': '任务 ' + uuid.uuid4().hex},
                                {'role': 'assistant', 'text': ('完成：α 中文\n' * (ci + 1))}]
                    for i, m in enumerate(messages, 1):
                        if mode == 'legacy':
                            records.append({'type': 'response_item', 'payload':
                                            {'type': 'message', **m, 'content': [{'type': 'input_text', 'text': m['text']}]}})
                        else:
                            records.append({'type': 'event_msg', 'ordinal': i, 'payload':
                                {'type': 'item_completed', 'item': {'type': 'UserMessage' if m['role'] == 'user' else 'AgentMessage',
                                    'id': str(uuid.uuid4()), 'content': [{'type': 'text', 'text': m['text']}]}}})
                    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
                    db.execute('INSERT INTO threads VALUES(?,?,?,?,?)',
                               (cid, str(project), str(path), f'标题 {pi}/{ci}', mode))
                    self.ids.append(cid)
                    self.expected[cid] = messages
            db.commit()
        self.memory = self.root / 'explicit-memory.md'
        self.memory.write_text('Prefer Chinese explanations.\n')
        self.spec = self.root / 'selection.json'
        self.spec.write_text(porter.dumps({'codex_home': str(self.home), 'projects': [
            {'path': str(p), 'name': '同名项目', 'context_files': [str(self.memory)]} for p in self.source_roots]}), encoding='utf-8')
        self.job_dir = self.root / 'private-job'
        self.job = self.job_dir / 'job.json'
        self.destination = self.root / 'different-account' / 'workspaces'
        self.run = self.root / 'plan'
        self.profile = self.root / 'native-profile'
        self.config = target.initialize_sandbox(self.profile)
        self.layout = target.paths(self.profile, Path(self.config['ui']))
        # Existing unrelated conversation, cache bytes and preferences must survive.
        self.control = str(uuid.uuid4())
        self.layout['cache'].write_bytes(codec.blob(1, codec.blob(1, self.control) + codec.blob(2, b'control')))
        with closing(sqlite3.connect(self.layout['summary'])) as db:
            db.execute('INSERT INTO conversation_summaries(conversation_id,title) VALUES(?,?)', (self.control, 'keep'))
            db.commit()
        publication.save_json(self.layout['ui'], {'projectsOrder': '["existing-project"]', 'untouched': 19})

    def export(self):
        return transfer.export_job(self.spec, self.job_dir)

    def plan(self, **kwargs):
        return migrate.make_plan(self.job, self.destination, self.run, target.ADAPTER, self.config, **kwargs)

    def hashes(self):
        return {str(p.relative_to(self.profile)): publication.digest(p)
                for p in self.profile.rglob('*') if p.is_file()}

    def test_multiple_projects_modes_empty_project_and_cross_account_paths(self):
        self.export()
        before = self.hashes()
        report = self.plan(activate_context=True)
        self.assertEqual(report['projects'], 3)
        self.assertEqual(report['new_conversations'], 4)
        self.assertEqual(report['status'], 'ready-to-apply')
        self.assertEqual(self.hashes(), before, 'Planning must not modify the target profile')
        plan = porter.read_json(self.run / 'plan.json')
        self.assertEqual(len({p['workspace'] for p in plan['projects']}), 3)
        target.apply(self.run, plan)
        self.assertTrue(migrate.verify_plan(self.run / 'plan.json')['publication_snapshot_matches'])
        for index, project in enumerate(plan['projects']):
            workspace = Path(project['workspace'])
            self.assertEqual((workspace / '代码.txt').read_bytes(), (self.source_roots[index] / '代码.txt').read_bytes())
            self.assertEqual((workspace / 'AGENTS.md').read_text(), 'Use project conventions.\n')
            rules = list((workspace / '.agents/rules').glob('*.md'))
            self.assertEqual(len(rules), 2)
            self.assertTrue(any('Prefer Chinese explanations.' in p.read_text() for p in rules))
            for chat in project['conversations']:
                self.assertEqual(codec.decode_messages(self.layout['conversations'] / (chat['target_id'] + '.db')),
                                 self.expected[chat['source_id']])
        with closing(sqlite3.connect(self.layout['summary'])) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM conversation_summaries').fetchone()[0], 5)
            self.assertEqual(db.execute('SELECT title FROM conversation_summaries WHERE conversation_id=?', (self.control,)).fetchone()[0], 'keep')
        self.assertEqual(porter.read_json(self.layout['ui'])['untouched'], 19)
        self.assertEqual(len(json.loads(porter.read_json(self.layout['ui'])['projectsOrder'])), 4)

    def test_same_content_reexport_is_idempotent_preserves_user_edits(self):
        self.export()
        self.plan()
        plan = porter.read_json(self.run / 'plan.json')
        target.apply(self.run, plan)
        project_file = Path(plan['projects'][0]['workspace']) / '代码.txt'
        project_file.write_text('user edit', encoding='utf-8')
        before = self.hashes()
        second = self.root / 'second-export'
        spec = porter.read_json(self.spec)
        spec['projects'].reverse()  # Reordering batch entries must not change source identity.
        publication.save_json(self.spec, spec)
        transfer.export_job(self.spec, second)
        report = migrate.make_plan(second / 'job.json', self.destination, self.root / 'second-plan', target.ADAPTER, self.config)
        self.assertEqual(report['reused_projects'], 3)
        self.assertEqual(report['new_conversations'], 0)
        second_plan = porter.read_json(self.root / 'second-plan/plan.json')
        self.assertEqual(second_plan['entries'], [])
        self.assertEqual(self.hashes(), before)
        self.assertEqual(project_file.read_text(), 'user edit')

    def test_changed_source_refuses_incremental_overwrite(self):
        self.export()
        self.plan()
        target.apply(self.run, porter.read_json(self.run / 'plan.json'))
        (self.source_roots[0] / '代码.txt').write_text('changed')
        second = self.root / 'second-export'
        transfer.export_job(self.spec, second)
        before = self.hashes()
        with self.assertRaisesRegex(porter.PorterError, 'incremental merge'):
            migrate.make_plan(second / 'job.json', self.destination, self.root / 'second-plan', target.ADAPTER, self.config)
        self.assertEqual(self.hashes(), before)

    def test_report_blocks_tools_until_explicit_loss_acceptance(self):
        path = self.home / 'sessions' / ('rollout-' + self.ids[0] + '.jsonl')
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'read_file', 'arguments': '{}'}}) + '\n')
        self.export()
        before = self.hashes()
        self.assertEqual(self.plan()['status'], 'blocked')
        blocked = porter.read_json(self.run / 'plan.json')
        self.assertFalse(blocked['can_apply'])
        self.assertEqual(blocked['entries'], [])
        with self.assertRaises(porter.PorterError):
            target.apply(self.run, blocked)
        self.assertEqual(self.hashes(), before)
        report = migrate.make_plan(self.job, self.root / 'lossy-workspaces', self.root / 'lossy-plan', target.ADAPTER,
                                   self.config, allow_lossy=True)
        self.assertEqual(report['status'], 'ready-to-apply')
        lossy = porter.read_json(self.root / 'lossy-plan/plan.json')
        chat = next(c for p in lossy['projects'] for c in p['conversations'] if c['source_id'] == self.ids[0])
        self.assertEqual(chat['issues'][0]['code'], 'tool_or_event_archived_only')
        self.assertIn('read_file', Path(chat['events_archive']).read_text(encoding='utf-8'))
        self.assertFalse(any('read_file' in m['text'] for m in chat['messages']))

    def test_portable_target_and_context_archive_only(self):
        self.export()
        report = migrate.make_plan(self.job, self.destination, self.run)
        self.assertEqual(report['status'], 'portable-restored')
        plan = porter.read_json(self.run / 'plan.json')
        for project in plan['projects']:
            self.assertEqual(project['restore']['explicit_context'], 'archived-only')
            self.assertEqual(len(project['restore']['added_rules']), 1)
            self.assertTrue((Path(project['workspace']).parent / 'context/0001.md').is_file())

    def test_batch_recovery_restores_unrelated_profile_and_keeps_workspaces(self):
        self.export()
        before = self.hashes()
        self.plan()
        plan = porter.read_json(self.run / 'plan.json')
        target.apply(self.run, plan)
        target.recover(self.run, plan)
        self.assertEqual(self.hashes(), before)
        self.assertTrue(all(Path(p['workspace']).is_dir() for p in plan['projects']))

    def test_apply_failure_rolls_back_entire_batch(self):
        self.export()
        before = self.hashes()
        self.plan()
        plan = porter.read_json(self.run / 'plan.json')
        publish = publication.publish
        count = 0
        def interrupted(*args):
            nonlocal count
            count += 1
            if count == 5:
                raise OSError('simulated full disk')
            return publish(*args)
        with patch.object(publication, 'publish', side_effect=interrupted), self.assertRaises(OSError):
            target.apply(self.run, plan)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(porter.read_json(self.run / 'journal.json')['status'], 'rolled-back')

    def test_recovery_refuses_later_target_edits(self):
        self.export()
        self.plan()
        plan = porter.read_json(self.run / 'plan.json')
        target.apply(self.run, plan)
        self.layout['ui'].write_text('{"later-user-edit": true}')
        before = self.hashes()
        with self.assertRaisesRegex(porter.PorterError, 'changed after'):
            target.recover(self.run, plan)
        self.assertEqual(self.hashes(), before)

    def test_tampered_target_allowlist_and_stage_are_refused(self):
        self.export()
        self.plan()
        plan = porter.read_json(self.run / 'plan.json')
        before = self.hashes()
        altered = json.loads(json.dumps(plan))
        altered['entries'][0]['target'] = str(self.profile / 'auth.json')
        with self.assertRaisesRegex(porter.PorterError, 'unapproved'):
            target.apply(self.run, altered)
        Path(plan['entries'][0]['source']).write_bytes(b'tampered')
        with self.assertRaisesRegex(porter.PorterError, 'Staged content changed'):
            target.apply(self.run, plan)
        self.assertEqual(self.hashes(), before)

    def test_changed_bundle_and_context_rejected_before_restoring(self):
        self.export()
        context = next((self.job_dir / 'context').glob('*.md'))
        context.write_text('changed')
        with self.assertRaisesRegex(porter.PorterError, 'Changed or escaped context'):
            self.plan()
        self.assertFalse(self.destination.exists())

    def test_aggregate_size_and_duplicate_project_rejected(self):
        self.export()
        with self.assertRaisesRegex(porter.PorterError, 'size limit'):
            transfer.load_job(self.job, max_total=100)
        job = porter.read_json(self.job)
        job['projects'].append(job['projects'][0])
        publication.save_json(self.job, job)
        with self.assertRaisesRegex(porter.PorterError, 'Duplicate'):
            transfer.load_job(self.job)

    def test_unknown_newer_catalog_fails_instead_of_using_stale_catalog(self):
        with closing(sqlite3.connect(self.home / 'state_99.sqlite')) as db:
            db.execute('CREATE TABLE different_schema(value TEXT)')
        with self.assertRaisesRegex(porter.PorterError, 'Unsupported Codex'):
            self.export()
        self.assertFalse(self.job.exists())

    def test_registered_multi_root_project_is_exported_by_selected_physical_roots(self):
        with closing(sqlite3.connect(self.catalog)) as db:
            db.execute('ALTER TABLE threads ADD COLUMN project_id TEXT')
            db.execute('UPDATE threads SET project_id=?', ('logical-project',))
            db.execute('CREATE TABLE projects(id TEXT,name TEXT)')
            db.execute('CREATE TABLE project_roots(project_id TEXT,path TEXT)')
            db.execute('INSERT INTO projects VALUES(?,?)', ('logical-project', 'two roots'))
            db.executemany('INSERT INTO project_roots VALUES(?,?)',
                           [('logical-project', str(p)) for p in self.source_roots[:2]])
            db.commit()
        self.export()
        parts = transfer.load_job(self.job)
        self.assertEqual([len(p['manifest']['threads']) for p in parts], [2, 2, 0])
        self.assertEqual(len(parts[0]['scope_notes']), 2)
        self.assertEqual(len(parts[1]['scope_notes']), 2)
        self.assertEqual({t['source_project_id'] for p in parts for t in p['manifest']['threads']}, {'logical-project'})

    def test_unsupported_legacy_item_is_reported_and_blocks_native_apply(self):
        path = self.home / 'sessions' / ('rollout-' + self.ids[0] + '.jsonl')
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'response_item', 'payload': {'type': 'future_tool_type', 'value': 'keep'}}) + '\n')
        self.export()
        self.assertEqual(self.plan()['status'], 'blocked')
        plan = porter.read_json(self.run / 'plan.json')
        chat = next(c for p in plan['projects'] for c in p['conversations'] if c['source_id'] == self.ids[0])
        self.assertIn({'code': 'unsupported_response_item_archived_only', 'count': 1}, chat['issues'])

    def test_unmarked_profile_or_unknown_build_cannot_bypass_guard(self):
        (self.profile / '.porter-disposable-profile').unlink()
        with self.assertRaisesRegex(porter.PorterError, 'owned disposable'):
            target.read_ledger(self.config)
        live = {**self.config, 'sandbox': False, 'app_root': str(self.root / 'unknown-app')}
        with self.assertRaises(porter.PorterError):
            target.read_ledger(live)


if __name__ == '__main__':
    unittest.main()
