"""Shipped self-test: generates its own inputs; never scans real profiles."""
from contextlib import closing
import json
from filesystem import Path, extended
import sqlite3
import tempfile
import uuid
import desktop_service as service
import porter
import publication
from adapters import antigravity
from adapters import antigravity_codec as codec


def fixture(root):
    home = root / 'source account' / 'codex'
    (home / 'sessions').mkdir(parents=True)
    expected, projects = {}, []
    with closing(sqlite3.connect(home / 'state_7.sqlite')) as db:
        db.execute('CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT,title TEXT,history_mode TEXT)')
        for pi in range(2):
            project = root / f'项目 {pi + 1}'
            project.mkdir()
            (project / 'settings.json').write_text('{"value": 123}\n')
            ids = []
            for ci in range(2):
                cid = str(uuid.uuid4())
                ids.append(cid)
                messages = [{'role': 'user', 'text': '测试记忆：' + uuid.uuid4().hex},
                            {'role': 'assistant', 'text': '已记录。'}]
                records = [{'type': 'session_meta', 'payload': {'id': cid, 'cwd': str(project), 'history_mode': 'legacy'}}]
                records += [{'type': 'response_item', 'payload': {'type': 'message', 'role': m['role'],
                            'content': [{'type': 'text', 'text': m['text']}]}} for m in messages]
                log = home / 'sessions' / (cid + '.jsonl')
                log.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
                db.execute('INSERT INTO threads VALUES(?,?,?,?,?)', (cid, str(project), str(log), f'会话 {pi + 1}-{ci + 1}', 'legacy'))
                expected[cid] = messages
            projects.append({'path': str(project), 'name': project.name, 'threads': ids})
        db.commit()
    return {'codex_home': str(home), 'projects': projects}, expected


def run():
    with tempfile.TemporaryDirectory(prefix='porter-selftest-', dir=extended(tempfile.gettempdir(), force=True)) as temporary:
        root = Path(temporary)
        selection, expected = fixture(root)
        target = antigravity.initialize_sandbox(root / 'different target account')
        layout = antigravity.paths(Path(target['home']), Path(target['ui']))
        initial = {k: publication.digest(layout[k]) for k in ('summary', 'cache', 'ui')}
        source = {str(p): publication.digest(p) for p in root.glob('项目 */settings.json')}
        prepared = service.prepare(root / 'migration output', antigravity.ADAPTER, target, selection=selection)
        assert prepared['status'] == 'ready-to-apply'
        assert initial == {k: publication.digest(layout[k]) for k in initial}
        result = service.apply(Path(prepared['plan']))
        assert result['status'] == 'installed' and result['verification']['publication_snapshot_matches']
        plan = porter.read_json(Path(prepared['plan']))
        for project in plan['projects']:
            assert (Path(project['workspace']) / 'settings.json').read_text() == '{"value": 123}\n'
            for chat in project['conversations']:
                assert codec.decode_messages(layout['conversations'] / (chat['target_id'] + '.db')) == expected[chat['source_id']]
        again = service.prepare(root / 'migration output', antigravity.ADAPTER, target, existing_job=prepared['job'])
        assert again['reused_projects'] == 2 and again['new_conversations'] == 0
        service.recover(Path(prepared['plan']))
        assert initial == {k: publication.digest(layout[k]) for k in initial}
        assert all(publication.digest(Path(path)) == value for path, value in source.items())
        return {'passed': True, 'version': service.VERSION, 'projects': 2, 'conversations': 4,
                'checks': ['export', 'cross-account-paths', 'native-format', 'project-files',
                           'plan-does-not-write-profile', 'apply', 'deduplicate', 'recover', 'source-unchanged'],
                'real_profiles_read': False, 'native_client_started': False,
                'runtime_acceptance': 'not part of this isolated format test'}
