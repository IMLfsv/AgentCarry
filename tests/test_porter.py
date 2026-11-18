import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import porter


def record(ordinal, kind, payload):
    return {'ordinal': ordinal, 'timestamp': '2026-11-18T00:00:00Z', 'type': kind, 'payload': payload}


def message(ordinal, role, text, extra=None):
    item = {'type': 'UserMessage' if role == 'user' else 'AgentMessage',
            'id': str(uuid.uuid4()), 'content': [{'type': 'text', 'text': text}]}
    if extra:
        item['content'].append(extra)
    return record(ordinal, 'event_msg', {'type': 'item_completed', 'item': item})


def write_log(path, records):
    data = b''.join((json.dumps(r, ensure_ascii=False) + '\n').encode() for r in records)
    path.write_bytes(data)
    return len(data)


class PorterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project with spaces'
        self.project.mkdir()
        (self.project / 'demo.py').write_text('print("portable")\n')
        self.home = self.root / 'codex'
        (self.home / 'sessions').mkdir(parents=True)
        (self.home / 'archived_sessions').mkdir()
        (self.home / 'attachments').mkdir()
        self.db = sqlite3.connect(self.home / 'state_5.sqlite')
        self.db.execute('CREATE TABLE threads(id TEXT, cwd TEXT, rollout_path TEXT,title TEXT,history_mode TEXT,archived INTEGER)')
        self.addCleanup(self.db.close)
        self.id = str(uuid.uuid4())
        self.log = self.home / 'sessions' / ('rollout-2026-' + self.id + '.jsonl')
        self.meta = record(0, 'session_meta', {'id': self.id, 'cwd': str(self.project), 'history_mode': 'paginated'})
        self.items = [self.meta, message(1, 'user', 'Remember project marker ALPHA-237.'),
                      message(2, 'assistant', 'Recorded ALPHA-237.')]
        write_log(self.log, self.items)
        self.add_thread(self.id, self.log)
        self.bundle = self.root / 'private.zip'

    def add_thread(self, ident, log, cwd=None):
        self.db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?)',
                        (ident, str(cwd or self.project), str(log), 'Synthetic test', 'paginated', 0))
        self.db.commit()

    def export(self, **kwargs):
        return porter.export_bundle(self.home, self.project, self.bundle, **kwargs)

    def test_round_trip_preserves_project_raw_and_messages(self):
        (self.project / 'paper.txt').write_text('中文科研资料\n', encoding='utf-8')
        original = self.log.read_bytes()
        self.export()
        self.assertTrue(porter.verify_bundle(self.bundle)['verified'])
        out = self.root / 'restored'
        porter.restore_bundle(self.bundle, out)
        self.assertEqual((out / 'project/paper.txt').read_bytes(), (self.project / 'paper.txt').read_bytes())
        self.assertEqual((out / 'history/raw/00001.jsonl').read_bytes(), original)
        self.assertEqual(self.log.read_bytes(), original)
        manifest = porter.read_json(out / 'manifest.json')
        self.assertEqual(manifest['threads'][0]['counts']['message'], 2)
        self.assertFalse(manifest['native_antigravity_import'])
        self.assertFalse((out / '.porter-incomplete').exists())

    def test_fork_includes_only_required_parent_prefix(self):
        prefix = self.log.stat().st_size
        write_log(self.log, self.items + [message(3, 'user', 'PARENT_FUTURE_MUST_NOT_LEAK')])
        child_id = str(uuid.uuid4())
        child = self.home / 'sessions' / ('rollout-2026-' + child_id + '.jsonl')
        head = record(3, 'session_meta', {'id': child_id, 'history_mode': 'paginated',
                    'history_base': {'thread_id': self.id, 'end_byte_offset': prefix, 'end_ordinal_exclusive': 3}})
        write_log(child, [head, message(4, 'user', 'Continue from the branch.'), message(5, 'assistant', 'Branch answer.')])
        self.add_thread(child_id, child)
        self.export(ids=[child_id])
        with zipfile.ZipFile(self.bundle) as z:
            m = json.loads(z.read('manifest.json'))
            transcript = z.read(m['threads'][0]['readable']).decode()
            self.assertIn('ALPHA-237', transcript)
            self.assertIn('Branch answer.', transcript)
            self.assertEqual(m['threads'][0]['counts']['message'], 4)
            for name in z.namelist():
                self.assertNotIn(b'PARENT_FUTURE_MUST_NOT_LEAK', z.read(name))

    def test_missing_dependency_fails_without_published_bundle(self):
        self.meta['payload']['history_base'] = {'thread_id': str(uuid.uuid4()), 'end_byte_offset': 100, 'end_ordinal_exclusive': 2}
        write_log(self.log, self.items)
        with self.assertRaises(porter.PorterError):
            self.export()
        self.assertFalse(self.bundle.exists())

    def test_subagent_segment_explicitly_marked(self):
        self.meta['payload']['subagent_history_start_ordinal'] = 42
        write_log(self.log, self.items)
        self.export()
        with zipfile.ZipFile(self.bundle) as z:
            m = json.loads(z.read('manifest.json'))
        self.assertEqual(m['threads'][0]['coverage'], 'local-segment')
        self.assertEqual(m['warnings'][0]['code'], 'subagent_local_segment_only')

    def test_credentials_and_git_exclusions_are_reported(self):
        (self.project / '.env').write_text('TOKEN=synthetic-secret')
        (self.project / '.env.example').write_text('TOKEN=')
        (self.project / '.git').mkdir()
        (self.project / '.git/config').write_text('synthetic credential')
        self.export()
        with zipfile.ZipFile(self.bundle) as z:
            m = json.loads(z.read('manifest.json'))
            self.assertNotIn('project/.env', z.namelist())
            self.assertIn('project/.env.example', z.namelist())
            self.assertEqual({x['path'] for x in m['excluded']}, {'.git', '.env'})

    def test_structured_asset_inside_allowlist_is_bundled(self):
        asset = self.home / 'attachments/synthetic.png'
        asset.write_bytes(b'fake image fixture')
        write_log(self.log, [self.meta, message(1, 'user', 'Inspect attached', {'type': 'localImage', 'path': str(asset)})])
        self.export()
        with zipfile.ZipFile(self.bundle) as z:
            amap = json.loads(z.read('history/asset-map.json'))
            self.assertEqual(z.read(amap[str(asset)]['bundle_path']), asset.read_bytes())

    def test_external_asset_not_copied_without_allowlist(self):
        asset = self.root / 'unselected.txt'
        asset.write_text('NOT_SELECTED')
        write_log(self.log, [self.meta, message(1, 'user', 'Reference', {'type': 'file', 'path': str(asset)})])
        self.export()
        with zipfile.ZipFile(self.bundle) as z:
            amap = json.loads(z.read('history/asset-map.json'))
            self.assertEqual(amap[str(asset)]['status'], 'outside-allowed-roots')
            self.assertFalse(any(n.startswith('assets/') for n in z.namelist()))

    def test_partial_record_rejected(self):
        with self.log.open('ab') as f:
            f.write(b'{"partial":')
        with self.assertRaises(porter.PorterError):
            self.export()
        self.assertFalse(self.bundle.exists())

    def test_ordinal_gap_rejected(self):
        self.items[2]['ordinal'] = 4
        write_log(self.log, self.items)
        with self.assertRaises(porter.PorterError):
            self.export()

    def test_legacy_response_items_supported(self):
        self.meta['payload']['history_mode'] = 'legacy'
        write_log(self.log, [self.meta, record(1, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Legacy hello'}]})])
        self.export()
        with zipfile.ZipFile(self.bundle) as z:
            m = json.loads(z.read('manifest.json'))
            self.assertIn(b'Legacy hello', z.read(m['threads'][0]['readable']))

    def test_source_changed_mid_export_is_not_published(self):
        original = porter.BundleBuilder.project_thread
        def change(builder, row, number):
            (self.project / 'demo.py').write_text('modified while exporting')
            return original(builder, row, number)
        with patch.object(porter.BundleBuilder, 'project_thread', change):
            with self.assertRaises(porter.PorterError):
                self.export()
        self.assertFalse(self.bundle.exists())

    def test_new_file_mid_export_is_not_silently_missed(self):
        original = porter.BundleBuilder.project_thread
        def change(builder, row, number):
            (self.project / 'new-file.txt').write_text('arrived while exporting')
            return original(builder, row, number)
        with patch.object(porter.BundleBuilder, 'project_thread', change):
            with self.assertRaises(porter.PorterError):
                self.export()
        self.assertFalse(self.bundle.exists())

    def test_dependency_cycle_rejected(self):
        self.meta['payload']['history_base'] = {'thread_id': self.id, 'end_byte_offset': 10, 'end_ordinal_exclusive': 1}
        write_log(self.log, self.items)
        with self.assertRaises(porter.PorterError):
            self.export()

    def test_dependency_byte_boundary_rejected(self):
        child_id = str(uuid.uuid4())
        child = self.home / 'sessions' / ('rollout-2026-' + child_id + '.jsonl')
        head = record(3, 'session_meta', {'id': child_id, 'history_mode': 'paginated',
                    'history_base': {'thread_id': self.id, 'end_byte_offset': self.log.stat().st_size - 3,
                                     'end_ordinal_exclusive': 3}})
        write_log(child, [head, message(4, 'user', 'Child')])
        self.add_thread(child_id, child)
        with self.assertRaises(porter.PorterError):
            self.export(ids=[child_id])

    def test_archive_symlink_rejected(self):
        bad = self.root / 'link.zip'
        info = zipfile.ZipInfo('fake-link')
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr(info, '../outside')
        with self.assertRaises(porter.PorterError):
            porter.verify_bundle(bad)

    def test_unselected_thread_rejected(self):
        with self.assertRaises(porter.PorterError):
            self.export(ids=[str(uuid.uuid4())])

    def test_duplicate_zip_entry_rejected(self):
        import warnings
        bad = self.root / 'duplicates.zip'
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with zipfile.ZipFile(bad, 'w') as z:
                z.writestr('same.txt', 'one')
                z.writestr('same.txt', 'two')
        with self.assertRaises(porter.PorterError):
            porter.verify_bundle(bad)

    def test_refuses_output_inside_project(self):
        self.bundle = self.project / 'recursive.zip'
        with self.assertRaises(porter.PorterError):
            self.export()

    def test_refuses_overwrite_on_export_and_restore(self):
        self.export()
        before = self.bundle.read_bytes()
        with self.assertRaises(porter.PorterError):
            self.export()
        self.assertEqual(self.bundle.read_bytes(), before)
        dest = self.root / 'existing'
        dest.mkdir()
        (dest / 'keep').write_text('original')
        with self.assertRaises(porter.PorterError):
            porter.restore_bundle(self.bundle, dest)
        self.assertEqual((dest / 'keep').read_text(), 'original')

    def test_tampered_content_rejected_before_restore(self):
        self.export()
        tampered = self.root / 'tampered.zip'
        with zipfile.ZipFile(self.bundle) as src, zipfile.ZipFile(tampered, 'w') as dst:
            for name in src.namelist():
                data = src.read(name)
                if name == 'project/demo.py':
                    data = b'X' * len(data)
                dst.writestr(name, data)
        dest = self.root / 'bad-restore'
        with self.assertRaises(porter.PorterError):
            porter.restore_bundle(tampered, dest)
        self.assertFalse(dest.exists())

    def test_zip_slip_and_case_collisions_rejected(self):
        for names in [['../escape'], ['A.txt', 'a.txt'], ['C:/absolute'], ['a\\b'], ['CON.txt'], ['trailing.']]:
            with self.subTest(names=names):
                bad = self.root / 'bad.zip'
                with zipfile.ZipFile(bad, 'w') as z:
                    for name in names:
                        z.writestr(name, b'bad')
                with self.assertRaises(porter.PorterError):
                    porter.verify_bundle(bad)

    def test_size_limit(self):
        self.export()
        with self.assertRaises(porter.PorterError):
            porter.verify_bundle(self.bundle, max_total=1)

    def test_malformed_manifest_types_rejected(self):
        values = [[], {'format': porter.FORMAT, 'entries': {}, 'threads': None},
                  {'format': porter.FORMAT, 'entries': {'x': {'size': 1, 'sha256': 123}}, 'threads': []}]
        for i, value in enumerate(values):
            with self.subTest(i=i):
                bad = self.root / ('malformed-' + str(i) + '.zip')
                with zipfile.ZipFile(bad, 'w') as z:
                    z.writestr('manifest.json', json.dumps(value))
                    if i == 2:
                        z.writestr('x', 'x')
                with self.assertRaises(porter.PorterError):
                    porter.verify_bundle(bad)

    def test_credential_exclusions_are_case_insensitive(self):
        (self.project / '.ENV').write_text('SYNTHETIC=example')
        (self.project / 'AUTH.JSON').write_text('{}')
        self.export()
        with zipfile.ZipFile(self.bundle) as z:
            self.assertNotIn('project/.ENV', z.namelist())
            self.assertNotIn('project/AUTH.JSON', z.namelist())

    def test_malformed_completed_item_rejected(self):
        write_log(self.log, self.items + [record(3, 'event_msg', {'type': 'item_completed', 'item': []})])
        with self.assertRaises(porter.PorterError):
            self.export()
        self.assertFalse(self.bundle.exists())

    def test_boundary_matching_does_not_select_sibling(self):
        sibling = self.root / (self.project.name + '-other')
        sibling.mkdir()
        self.add_thread(str(uuid.uuid4()), self.log, sibling)
        self.assertEqual(len(porter.selection(self.home, self.project)), 1)

    def test_multi_megabyte_unicode_stream_round_trip(self):
        big = self.project / 'large.txt'
        data = ('数据\n' * 400000).encode()
        big.write_bytes(data)
        self.export()
        out = self.root / 'large-restored'
        porter.restore_bundle(self.bundle, out)
        self.assertEqual(hashlib.sha256((out / 'project/large.txt').read_bytes()).digest(), hashlib.sha256(data).digest())


if __name__ == '__main__':
    unittest.main()
