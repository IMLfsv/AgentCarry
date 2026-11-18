import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_install as installer
import antigravity_trial as native
import porter


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.journal = self.root / 'journal.json'
        self.guard = lambda: None

    def entry(self, name, original=None, replacement=b'new'):
        target = self.root / name
        backup = self.root / (name + '.backup')
        source = self.root / (name + '.stage')
        if original is not None:
            target.write_bytes(original)
            backup.write_bytes(original)
        source.write_bytes(replacement)
        return {'target': str(target), 'source': str(source), 'after': installer.digest(source),
                'before': installer.digest(target), 'backup': str(backup) if original is not None else None}

    def test_install_and_recover_existing_and_new_files(self):
        entries = [self.entry('new.db'), self.entry('cache.pb', b'old')]
        result = installer.apply_entries(entries, self.journal, self.guard)
        self.assertEqual(result['status'], 'installed')
        self.assertEqual((self.root / 'cache.pb').read_bytes(), b'new')
        installer.rollback(entries, self.guard)
        self.assertFalse((self.root / 'new.db').exists())
        self.assertEqual((self.root / 'cache.pb').read_bytes(), b'old')

    def test_failure_mid_install_rolls_back_without_overwriting_control(self):
        entries = [self.entry('cache.pb', b'old'), self.entry('new.db')]
        original_publish = installer.publish
        calls = 0
        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('simulated disk failure')
            return original_publish(*args)
        with patch.object(installer, 'publish', side_effect=fail_second), self.assertRaises(OSError):
            installer.apply_entries(entries, self.journal, self.guard)
        self.assertEqual((self.root / 'cache.pb').read_bytes(), b'old')
        self.assertFalse((self.root / 'new.db').exists())
        self.assertEqual(porter.read_json(self.journal)['status'], 'rolled-back')

    def test_post_install_failure_removes_only_created_file(self):
        entries = [self.entry('new.db'), self.entry('cache.pb', b'old')]
        (self.root / 'unrelated.txt').write_text('keep')
        def fail():
            raise ValueError('post-check failure')
        with self.assertRaises(ValueError):
            installer.apply_entries(entries, self.journal, self.guard, fail)
        self.assertFalse((self.root / 'new.db').exists())
        self.assertEqual((self.root / 'cache.pb').read_bytes(), b'old')
        self.assertEqual((self.root / 'unrelated.txt').read_text(), 'keep')

    def test_target_edit_after_backup_refuses_all_writes(self):
        entries = [self.entry('new.db'), self.entry('cache.pb', b'old')]
        (self.root / 'cache.pb').write_bytes(b'unrelated change')
        with self.assertRaises(porter.PorterError):
            installer.apply_entries(entries, self.journal, self.guard)
        self.assertFalse((self.root / 'new.db').exists())
        self.assertEqual((self.root / 'cache.pb').read_bytes(), b'unrelated change')

    def test_changed_backup_refused(self):
        entry = self.entry('cache.pb', b'old')
        Path(entry['backup']).write_bytes(b'wrong')
        with self.assertRaises(porter.PorterError):
            installer.apply_entries([entry], self.journal, self.guard)
        self.assertEqual((self.root / 'cache.pb').read_bytes(), b'old')

    def test_changed_staged_content_refused(self):
        entry = self.entry('cache.pb', b'old')
        Path(entry['source']).write_bytes(b'wrong')
        with self.assertRaises(porter.PorterError):
            installer.apply_entries([entry], self.journal, self.guard)

    def test_recovery_does_not_erase_later_app_changes(self):
        entries = [self.entry('new.db'), self.entry('cache.pb', b'old')]
        installer.apply_entries(entries, self.journal, self.guard)
        (self.root / 'cache.pb').write_bytes(b'new user conversation')
        with self.assertRaises(porter.PorterError):
            installer.rollback(entries, self.guard)
        self.assertTrue((self.root / 'new.db').exists())
        self.assertEqual((self.root / 'cache.pb').read_bytes(), b'new user conversation')

    def test_running_app_guard_refuses_before_publication(self):
        entries = [self.entry('cache.pb', b'old')]
        def running():
            raise porter.PorterError('app running')
        with self.assertRaises(porter.PorterError):
            installer.apply_entries(entries, self.journal, running)
        self.assertFalse(self.journal.exists())
        self.assertEqual((self.root / 'cache.pb').read_bytes(), b'old')

    def test_cannot_repeat_an_install_journal(self):
        entries = [self.entry('new.db')]
        installer.apply_entries(entries, self.journal, self.guard)
        with self.assertRaises(porter.PorterError):
            installer.apply_entries(entries, self.journal, self.guard)
        self.assertEqual((self.root / 'new.db').read_bytes(), b'new')

    def repair_fixture(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'file.txt').write_text('synthetic')
        trial = self.root / 'trial'
        info = native.prepare(source, trial, self.root / 'restored')
        profile = self.root / 'offline-profile'
        store = profile / '.gemini/antigravity'
        (store / 'conversations').mkdir(parents=True)
        configs = profile / '.gemini/config/projects'
        configs.mkdir(parents=True)
        appdata = profile / 'AppData/Roaming'
        (appdata / 'Antigravity').mkdir(parents=True)
        (appdata / 'Antigravity/app_storage.json').write_text('{}')
        candidate = trial / 'candidate'
        target_db = store / 'conversations' / (info['conversation_id'] + '.db')
        target_db.write_bytes((candidate / target_db.name).read_bytes())
        (configs / (info['project_id'] + '.json')).write_bytes((candidate / 'project.json').read_bytes())
        row = porter.read_json(candidate / 'summary.json')
        row['raw_summary'] = b''
        columns = ['conversation_id TEXT PRIMARY KEY'] + [name + (' INTEGER' if isinstance(value, int) else ' BLOB' if isinstance(value, bytes) else ' TEXT') for name, value in row.items() if name != 'conversation_id']
        with closing(sqlite3.connect(store / 'conversation_summaries.db')) as c:
            c.execute('CREATE TABLE conversation_summaries (' + ','.join(columns) + ')')
            names = list(row)
            row['title'] = ''; row['project_id'] = ''; row['workspace_uris'] = ''
            c.execute('INSERT INTO conversation_summaries (' + ','.join(names) + ') VALUES (' + ','.join('?' for _ in names) + ')', list(row.values()))
            c.commit()
        (store / 'agyhub_summaries_proto.pb').write_bytes(native.blob(1, native.blob(1, info['conversation_id']) + native.blob(2, b'')))
        return trial, profile, appdata, store, info

    def test_repair_plan_restores_binding_without_creating_another_chat(self):
        trial, profile, appdata, store, info = self.repair_fixture()
        original = installer.digest(store / 'conversation_summaries.db')
        run = self.root / 'repair-run'
        with patch.object(installer, 'stopped', self.guard), patch.object(Path, 'home', return_value=profile), patch.dict(os.environ, {'APPDATA': str(appdata)}):
            installer.prepare_repair(trial, run)
        self.assertEqual(installer.digest(store / 'conversation_summaries.db'), original)
        plan = porter.read_json(run / 'install-plan.json')
        self.assertEqual(len(plan['entries']), 3)
        self.assertTrue(all(Path(e['target']).is_relative_to(profile) for e in plan['entries']))
        installer.apply_entries(plan['entries'], run / 'journal.json', self.guard)
        with closing(sqlite3.connect(store / 'conversation_summaries.db')) as c:
            self.assertEqual(c.execute('SELECT project_id,title FROM conversation_summaries').fetchall(), [(info['project_id'], info['title'])])

    def test_repair_refuses_to_replace_a_conversation_with_an_added_turn(self):
        trial, profile, appdata, store, info = self.repair_fixture()
        db = store / 'conversations' / (info['conversation_id'] + '.db')
        with closing(sqlite3.connect(db)) as c:
            row = c.execute('SELECT step_type,status,metadata,step_payload FROM steps WHERE idx=3').fetchone()
            c.execute('INSERT INTO steps(idx,step_type,status,metadata,step_payload) VALUES(4,?,?,?,?)', row)
            c.commit()
        original = installer.digest(db)
        with patch.object(installer, 'stopped', self.guard), patch.object(Path, 'home', return_value=profile), patch.dict(os.environ, {'APPDATA': str(appdata)}), self.assertRaises(porter.PorterError):
            installer.prepare_repair(trial, self.root / 'repair-run')
        self.assertEqual(installer.digest(db), original)

    def test_checkpoint_keeps_committed_rows_from_unclean_shutdown_wal(self):
        profile = self.root / 'profile'
        db = profile / '.gemini/antigravity/conversation_summaries.db'
        db.parent.mkdir(parents=True)
        script = "import sqlite3,sys,os; c=sqlite3.connect(sys.argv[1]); c.execute('PRAGMA journal_mode=WAL'); c.execute('CREATE TABLE conversation_summaries(conversation_id TEXT PRIMARY KEY,title TEXT)'); c.execute('INSERT INTO conversation_summaries VALUES(?,?)',('synthetic','committed in WAL')); c.commit(); os._exit(0)"
        subprocess.run([sys.executable, '-c', script, str(db)], check=True)
        self.assertTrue(Path(str(db) + '-wal').exists())
        with patch.object(installer, 'stopped', self.guard), patch.object(Path, 'home', return_value=profile):
            result = installer.checkpoint_summary(self.root / 'checkpoint')
        self.assertTrue(result['summary_rows_preserved'])
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute('SELECT * FROM conversation_summaries').fetchall(), [('synthetic', 'committed in WAL')])
        self.assertTrue((self.root / 'checkpoint/consistent-snapshot.sqlite').is_file())
        self.assertTrue((self.root / 'checkpoint/conversation_summaries.db-wal').is_file())


if __name__ == '__main__':
    unittest.main()
