"""Create and round-trip a synthetic project. Does not read the real Codex home."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import porter


def record(ordinal, kind, payload):
    return {'timestamp': '2026-11-18T00:00:00Z', 'ordinal': ordinal,
            'type': kind, 'payload': payload}


def message(ordinal, role, text):
    return record(ordinal, 'event_msg', {'type': 'item_completed', 'item': {
        'type': 'UserMessage' if role == 'user' else 'AgentMessage',
        'id': f'demo-{ordinal}', 'content': [{'type': 'text', 'text': text}]}})


def write_log(path, records):
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records),
                    encoding='utf-8', newline='\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='New directory for synthetic demo files')
    root = parser.parse_args().out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    project, home = root / 'source-project', root / 'synthetic-codex-home'
    project.mkdir()
    (home / 'sessions').mkdir(parents=True)
    (project / 'app.py').write_text('print("ALPHA-237")\n', encoding='utf-8')
    (project / 'notes.txt').write_text('Synthetic project / 合成示例\n', encoding='utf-8')
    (project / '.env').write_text('SYNTHETIC_ONLY=excluded\n', encoding='utf-8')
    parent_id = '11111111-1111-4111-8111-111111111111'
    child_id = '22222222-2222-4222-8222-222222222222'
    parent = home / 'sessions' / f'rollout-demo-{parent_id}.jsonl'
    child = home / 'sessions' / f'rollout-demo-{child_id}.jsonl'
    parent_records = [record(0, 'session_meta', {'id': parent_id, 'cwd': str(project), 'history_mode': 'paginated'}),
                      message(1, 'user', 'Project marker is ALPHA-237. Please keep the Chinese notes.'),
                      message(2, 'assistant', 'The marker and Chinese notes are retained.')]
    write_log(parent, parent_records)
    prefix = parent.stat().st_size
    write_log(parent, parent_records + [message(3, 'user', 'PARENT_FUTURE_NOT_IN_BRANCH')])
    write_log(child, [record(3, 'session_meta', {'id': child_id, 'cwd': str(project), 'history_mode': 'paginated',
                     'history_base': {'thread_id': parent_id, 'end_ordinal_exclusive': 3, 'end_byte_offset': prefix}}),
                     message(4, 'user', 'Continue this branch with the saved marker.'),
                     message(5, 'assistant', 'Continuing with ALPHA-237.')])
    with sqlite3.connect(home / 'state_5.sqlite') as con:
        con.execute('CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT,title TEXT,history_mode TEXT,archived INTEGER)')
        con.executemany('INSERT INTO threads VALUES(?,?,?,?,?,?)', [
            (parent_id, str(project), str(parent), 'Synthetic parent', 'paginated', 0),
            (child_id, str(project), str(child), 'Synthetic branch', 'paginated', 0)])
    bundle = root / 'synthetic-bundle.zip'
    exported = porter.export_bundle(home, project, bundle, ids=[child_id])
    verified = porter.verify_bundle(bundle)
    restored = root / 'restored'
    porter.restore_bundle(bundle, restored)
    manifest = porter.read_json(restored / 'manifest.json')
    transcript = (restored / manifest['threads'][0]['readable']).read_text(encoding='utf-8')
    checks = {
        'project_bytes_match': all((project / n).read_bytes() == (restored / 'project' / n).read_bytes()
                                   for n in ('app.py', 'notes.txt')),
        'branch_includes_parent_context': 'ALPHA-237' in transcript,
        'branch_excludes_parent_future': 'PARENT_FUTURE_NOT_IN_BRANCH' not in transcript,
        'four_visible_messages': manifest['threads'][0]['counts']['message'] == 4,
        'credential_filename_excluded': not (restored / 'project/.env').exists(),
        'raw_child_bytes_match': (restored / manifest['threads'][0]['raw']).read_bytes() == child.read_bytes(),
        'bundle_verified': verified['verified'],
        'restore_completed': not (restored / '.porter-incomplete').exists(),
    }
    report = {'synthetic_only': True, 'checks': checks, 'all_passed': all(checks.values()),
              'bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest(),
              'exported_files': exported['files'], 'native_antigravity_import_tested': False}
    (root / 'demo-report.json').write_text(porter.dumps(report), encoding='utf-8')
    print(porter.dumps(report))
    return 0 if report['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
