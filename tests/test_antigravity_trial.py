import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import antigravity_trial as trial
import porter


class NativeTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = [str(uuid.uuid4()) for _ in range(3)]

    def test_text_database_and_linkage_round_trip(self):
        db = self.root / 'trial.db'
        messages = [{'role': 'user', 'text': '中文校验词 SYNT-923'},
                    {'role': 'assistant', 'text': '收到。'}]
        trial.create_native_db(db, messages, *self.ids, 'file:///C:/synthetic', 1234567890)
        self.assertEqual(trial.decode_messages(db), messages)
        with closing(sqlite3.connect(db)) as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(con.execute('PRAGMA page_size').fetchone()[0], 32768)
            self.assertEqual({r[1] for r in con.execute('PRAGMA index_list(steps)')},
                             {'idx_steps_status', 'idx_steps_step_type'})
            self.assertEqual(con.execute('SELECT source FROM trajectory_meta').fetchone()[0], 1)
            meta = con.execute('SELECT metadata FROM steps WHERE idx=1').fetchone()[0]
            ref = trial.one(meta, 20, 2)
            self.assertEqual(trial.one(ref, 1, 2).decode(), self.ids[1])
            self.assertEqual(trial.one(ref, 2, 0), 1)
            self.assertEqual(trial.one(ref, 4, 2).decode(), self.ids[0])
            body = trial.one(con.execute('SELECT step_payload FROM steps WHERE idx=1').fetchone()[0], 20, 2)
            self.assertNotIn(14, [f for f, _, _, _ in trial.fields(body)])

    def test_cache_append_preserves_original_bytes(self):
        old_id = str(uuid.uuid4())
        old = trial.blob(1, trial.blob(1, old_id) + trial.blob(2, b'old')) + trial.num(99, 42)
        added = trial.append_cache(old, self.ids[0], b'new')
        self.assertTrue(added.startswith(old))
        self.assertEqual(len(trial.fields(added)), 3)
        with self.assertRaises(porter.PorterError):
            trial.append_cache(added, self.ids[0], b'new')

    def test_project_order_preserves_string_and_other_preferences(self):
        storage = {'projectsOrder': '["existing"]', 'keep': {'value': 7}}
        result = trial.project_order(storage, self.ids[0])
        self.assertEqual(json.loads(result['projectsOrder']), ['existing', self.ids[0]])
        self.assertEqual(result['keep'], storage['keep'])
        self.assertEqual(storage['projectsOrder'], '["existing"]')

    def test_cache_repair_replaces_only_the_selected_entry(self):
        first = trial.blob(1, trial.blob(1, self.ids[0]) + trial.blob(2, b'old'))
        second = trial.blob(1, trial.blob(1, self.ids[1]) + trial.blob(2, b'control'))
        unknown = trial.num(101, 5)
        repaired = trial.replace_cache_entry(first + unknown + second, self.ids[0], b'fixed')
        self.assertTrue(repaired.endswith(unknown + second))
        self.assertEqual(trial.one(trial.fields(repaired)[0][2], 2, 2), b'fixed')
        with self.assertRaises(porter.PorterError):
            trial.replace_cache_entry(first + first, self.ids[0], b'fixed')
        with self.assertRaises(porter.PorterError):
            trial.replace_cache_entry(second, self.ids[0], b'fixed')

    def test_project_order_rejects_malformed_or_duplicate(self):
        for storage in [{'projectsOrder': 'null'}, {'projectsOrder': '[4]'}, {'projectsOrder': [self.ids[0]]}]:
            with self.subTest(storage=storage), self.assertRaises(porter.PorterError):
                trial.project_order(storage, self.ids[0])

    def test_wire_rejects_truncation_unknown_wire_and_oversize(self):
        for data in [b'\x80', b'\x0a\x05x', b'\x0b', b'\x00', b'\x08' + b'\xff' * 10]:
            with self.subTest(data=data), self.assertRaises(porter.PorterError):
                trial.fields(data)

    def test_native_db_refuses_overwrite(self):
        db = self.root / 'existing.db'
        db.write_bytes(b'original')
        with self.assertRaises(porter.PorterError):
            trial.create_native_db(db, [{'role': 'user', 'text': 'x'}], *self.ids, 'file:///test', 1)
        self.assertEqual(db.read_bytes(), b'original')

    def test_summary_ids_and_last_user_index(self):
        messages = [{'role': role, 'text': role} for role in ['user', 'assistant', 'user', 'assistant']]
        row, raw = trial.make_summary(messages, *self.ids, 'file:///project', 1234567890, 'Synthetic')
        self.assertEqual(row['last_user_input_step_index'], 2)
        self.assertEqual(trial.one(raw, 16, 0), 2)
        metadata = trial.one(raw, 17, 2)
        self.assertEqual(trial.one(metadata, 18, 2).decode(), self.ids[2])
        self.assertEqual(trial.one(metadata, 6, 2).decode(), self.ids[0])

    def test_unmarked_profile_refused(self):
        profile = self.root / 'profile'
        profile.mkdir()
        with self.assertRaises(porter.PorterError):
            trial.stage_profile(self.root / 'not-needed', profile)
        self.assertEqual(list(profile.iterdir()), [])

    def test_complete_synthetic_source_round_trip(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'test.txt').write_text('中文文件', encoding='utf-8')
        report = trial.prepare(source, self.root / 'trial', self.root / 'restored')
        expected = porter.read_json(self.root / 'trial/expected.json')
        db = self.root / 'trial/candidate' / (report['conversation_id'] + '.db')
        self.assertEqual(trial.decode_messages(db), expected['messages'])
        self.assertEqual((self.root / 'restored/project/test.txt').read_bytes(), (source / 'test.txt').read_bytes())
        self.assertEqual(report['native_ui'], 'NOT RUN')
        self.assertNotIn(expected['marker'].encode(), (self.root / 'restored/project/test.txt').read_bytes())


if __name__ == '__main__':
    unittest.main()
