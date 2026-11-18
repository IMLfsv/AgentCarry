"""Antigravity desktop adapter. Known Windows build; arbitrary projects/chats.

Storage is unofficial. Unsupported builds fail closed. Sandbox mode exercises
the format and publisher in an explicitly marked disposable profile only.
"""
from __future__ import annotations
from contextlib import closing
import json
import os
from filesystem import Path
import sqlite3
import subprocess
import time
import uuid
import porter
import publication as files
from adapters import antigravity_codec as codec

ADAPTER = 'antigravity-desktop'
LEDGER_FORMAT = 'agent-project-porter/antigravity-ledger/1'


def paths(home, ui):
    home, ui = Path(home).absolute(), Path(ui).absolute()
    store = home / '.gemini/antigravity'
    return {'home': home, 'ui_root': ui, 'store': store,
            'summary': store / 'conversation_summaries.db',
            'cache': store / 'agyhub_summaries_proto.pb',
            'ui': ui / 'app_storage.json', 'projects': home / '.gemini/config/projects',
            'conversations': store / 'conversations', 'ledger': store / 'porter-imports.json'}


def no_journals(db):
    if any(Path(str(db) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise porter.PorterError('SQLite sidecars present; finish a clean App shutdown before planning: ' + str(db))


def guard(config, check_journals=True):
    layout = paths(config['home'], config['ui'])
    for path in layout.values():
        files.no_links(path)
    if config.get('sandbox'):
        marker = layout['home'] / '.porter-disposable-profile'
        if (layout['home'].resolve() == Path.home().resolve() or
                not porter.inside(layout['ui_root'], layout['home']) or
                not marker.is_file() or marker.read_text() != 'format-tests-only\n'):
            raise porter.PorterError('Sandbox requires an owned disposable profile with contained UI storage')
    else:
        if os.name != 'nt':
            raise porter.PorterError('Native writes currently validated only on Windows; portable bundles remain available')
        root = Path(config['app_root'])
        for rel, key in [('Antigravity.exe', 'exe_sha256'), ('resources/app.asar', 'asar_sha256')]:
            files.no_links(root / rel)
            if files.digest(root / rel) != codec.APP_BUILD[key]:
                raise porter.PorterError('Unsupported installed build; native import needs a validated adapter update')
        script = "$ErrorActionPreference='Stop'; $p=@(Get-Process | Where-Object { $_.ProcessName -in @('Antigravity','language_server') }); $p.Count"
        powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        check = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],
                               capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if check.returncode or check.stdout.strip() != '0':
            raise porter.PorterError('Close Antigravity and its backend before native planning/apply/recovery')
    if check_journals:
        no_journals(layout['summary'])
    return layout


def read_ledger(config):
    layout = guard(config)
    required = [layout[k] for k in ('summary', 'cache', 'ui')]
    if not all(p.is_file() for p in required):
        raise porter.PorterError('Target profile is not initialized; open the App once, then close it')
    ledger = (porter.read_json(layout['ledger']) if layout['ledger'].exists() else
              {'format': LEDGER_FORMAT, 'projects': {}, 'conversations': {}})
    if ledger.get('format') != LEDGER_FORMAT or not isinstance(ledger.get('projects'), dict) or not isinstance(ledger.get('conversations'), dict):
        raise porter.PorterError('Unsupported migration ledger')
    return ledger


def initialize_sandbox(home):
    home = home.absolute()
    files.no_links(home)
    if home.exists() or home.resolve() == Path.home().resolve():
        raise porter.PorterError('Sandbox requires a new directory')
    home.mkdir(parents=True)
    (home / '.porter-disposable-profile').write_text('format-tests-only\n')
    layout = paths(home, home / 'ui')
    for key in ('store', 'projects', 'conversations', 'ui_root'):
        layout[key].mkdir(parents=True, exist_ok=True)
    # Minimal schema used only in disposable format tests; never initializes a live profile.
    sample, _ = codec.make_summary([{'role': 'user', 'text': 'schema'}],
                                  *[str(uuid.uuid4()) for _ in range(3)], 'file:///schema', 1, 'schema')
    columns = ['conversation_id TEXT PRIMARY KEY'] + [
        name + (' INTEGER' if isinstance(value, int) else ' TEXT')
        for name, value in sample.items() if name != 'conversation_id'] + ['raw_summary BLOB']
    with closing(sqlite3.connect(layout['summary'])) as con:
        con.execute('CREATE TABLE conversation_summaries (' + ','.join(columns) + ')')
        con.commit()
    layout['cache'].write_bytes(b'')
    files.save_json(layout['ui'], {'projectsOrder': '[]'})
    return {'home': str(home), 'ui': str(layout['ui_root']), 'sandbox': True,
            'runtime_validation': False}


def stage(run, config, projects, ledger):
    """Stage one batch and its backups without modifying target storage."""
    layout = guard(config)
    stage_dir, backup = run / 'staged', run / 'backup'
    stage_dir.mkdir()
    backup.mkdir()
    entries = []

    def original(target):
        files.no_links(target)
        if target.suffix == '.db':
            no_journals(target)
        before = files.digest(target)
        if before is None:
            return None, None, None
        data = target.read_bytes()
        import hashlib
        if hashlib.sha256(data).hexdigest() != before or files.digest(target) != before:
            raise porter.PorterError('Target changed while preparing the plan')
        path = backup / f'{len(list(backup.iterdir())):05d}.bin'
        path.write_bytes(data)
        return before, str(path), data

    def append(target, source, old=None):
        before, copy, _ = original(target) if old is None else old
        entries.append({'target': str(target), 'source': str(source),
                        'before': before, 'after': files.digest(source), 'backup': copy})

    fresh = [p for p in projects if not p['reused']]
    if not fresh:
        return []
    summary_old = original(layout['summary'])
    cache_old = original(layout['cache'])
    ui_old = original(layout['ui'])
    ledger_old = original(layout['ledger'])
    if ledger_old[2] is not None and json.loads(ledger_old[2]) != ledger:
        raise porter.PorterError('Import ledger changed while preparing')
    summary = stage_dir / 'summary.db'
    summary.write_bytes(summary_old[2])
    cache = cache_old[2]
    ui = json.loads(ui_old[2])
    updated = json.loads(json.dumps(ledger))
    with closing(sqlite3.connect(summary)) as con:
        con.execute('PRAGMA journal_mode=DELETE')
        before_rows = con.execute('SELECT count(*) FROM conversation_summaries').fetchone()[0]
        added = 0
        for project in fresh:
            pid = project['target_project_id']
            workspace = Path(project['workspace'])
            uri = workspace.as_uri()
            project_file = stage_dir / (pid + '.json')
            files.save_json(project_file, {'id': pid, 'name': project['name'],
                'projectResources': {'resources': [{'folderUri': uri}]},
                'permissionGrants': {'v2Migrated': True}, 'settings': {}, 'isWorkspaceOnly': False})
            target = layout['projects'] / project_file.name
            if target.exists():
                raise porter.PorterError('Target project identifier collision')
            append(target, project_file)
            ui = codec.project_order(ui, pid)
            for chat in project['conversations']:
                cid, tid = chat['target_id'], chat['trajectory_id']
                messages = chat['messages']
                seconds = int(time.time()) - len(messages)
                db = stage_dir / (cid + '.db')
                codec.create_native_db(db, messages, cid, tid, pid, uri, seconds)
                if codec.decode_messages(db) != messages:
                    raise porter.PorterError('Native text round-trip verification failed')
                target = layout['conversations'] / db.name
                if target.exists():
                    raise porter.PorterError('Target conversation identifier collision')
                append(target, db)
                row, raw = codec.make_summary(messages, cid, tid, pid, uri, seconds, chat['title'])
                row['raw_summary'] = raw
                con.execute('INSERT INTO conversation_summaries (' + ','.join(row) + ') VALUES (' +
                            ','.join('?' for _ in row) + ')', list(row.values()))
                cache = codec.append_cache(cache, cid, raw)
                updated['conversations'][chat['source_id']] = {'target_id': cid, 'project_key': project['key'],
                    'source_title': chat['title'], 'imported_messages': len(messages)}
                added += 1
            updated['projects'][project['key']] = {k: project[k] for k in
                ('fingerprint', 'workspace', 'target_project_id', 'name', 'activate_context')}
        con.commit()
        if (con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or
                con.execute('SELECT count(*) FROM conversation_summaries').fetchone()[0] != before_rows + added):
            raise porter.PorterError('Staged summary integrity/count failure')
    cache_file, ui_file, ledger_file = [stage_dir / n for n in ('cache.pb', 'ui.json', 'ledger.json')]
    cache_file.write_bytes(cache)
    files.save_json(ui_file, ui)
    files.save_json(ledger_file, updated)
    for key, source, old in [('summary', summary, summary_old), ('cache', cache_file, cache_old),
                              ('ui', ui_file, ui_old), ('ledger', ledger_file, ledger_old)]:
        append(layout[key], source, old)
    guard(config)
    if any(files.digest(Path(e['target'])) != e['before'] for e in entries):
        raise porter.PorterError('Target changed before plan completion')
    return entries


def validate_plan(run, plan):
    if plan.get('format') != 'agent-project-porter/plan/1' or plan.get('target') != ADAPTER:
        raise porter.PorterError('Not an Antigravity migration plan')
    layout = guard(plan['target_config'])
    allowed = set()
    fresh = [p for p in plan['projects'] if not p['reused']]
    if fresh:
        allowed.update(str(layout[k]) for k in ('summary', 'cache', 'ui', 'ledger'))
    for project in fresh:
        pid = str(uuid.UUID(project['target_project_id']))
        allowed.add(str(layout['projects'] / (pid + '.json')))
        if not Path(project['workspace']).is_dir():
            raise porter.PorterError('Restored project is missing')
        for chat in project['conversations']:
            cid = str(uuid.UUID(chat['target_id']))
            allowed.add(str(layout['conversations'] / (cid + '.db')))
    entries = plan['entries']
    actual = [e['target'] for e in entries]
    if len(actual) != len(set(actual)) or set(actual) != allowed:
        raise porter.PorterError('Plan contains missing, duplicate, or unapproved target files')
    for entry in entries:
        if (Path(entry['target']).parent in (layout['projects'], layout['conversations']) and
                (entry['before'] is not None or entry['backup'] is not None)):
            raise porter.PorterError('Imported conversations and projects must be new; replacement is not allowed')
        for key, folder in [('source', 'staged'), ('backup', 'backup')]:
            if entry[key] is not None:
                path = Path(entry[key])
                files.no_links(path)
                if not porter.inside(path.resolve(), (run / folder).resolve()):
                    raise porter.PorterError('Plan file escaped staging/backup directory')
        if (entry['before'] is None) != (entry['backup'] is None):
            raise porter.PorterError('Invalid backup state')
    return layout


def apply(run, plan):
    if not plan.get('can_apply'):
        raise porter.PorterError('Plan has blockers; inspect report.json before preparing a corrected plan')
    layout = validate_plan(run, plan)
    for key in ('conversations', 'projects'):
        layout[key].mkdir(parents=True, exist_ok=True)
    return files.apply_entries(plan['entries'], run / 'journal.json', lambda: guard(plan['target_config']))


def recover(run, plan):
    validate_plan(run, plan)
    if not (run / 'journal.json').is_file():
        raise porter.PorterError('No publication journal; this plan has not been applied')
    journal = porter.read_json(run / 'journal.json')
    if journal['entries'] != plan['entries']:
        raise porter.PorterError('Plan differs from publication journal')
    files.rollback(plan['entries'], lambda: guard(plan['target_config']))
    journal['status'] = 'rolled-back'
    files.save_json(run / 'journal.json', journal)
    return {'status': 'rolled-back', 'restored_workspaces_retained': True}
