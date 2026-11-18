"""Guarded installation of the single synthetic Antigravity desktop trial.

No login data is read or copied. Shared state is prepared from fresh, stopped-App
snapshots. Publication uses per-file atomic updates and a recoverable journal;
this is NOT a cross-file OS transaction. Do not start the App while applying.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

from adapters import antigravity_codec as trial_api
import porter


def digest(path):
    if not path.exists():
        return None
    porter.regular(path)
    h = hashlib.sha256()
    with path.open('rb') as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def no_links(path):
    for part in (path, *path.parents):
        if part.exists() and (part.is_symlink() or getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise porter.PorterError('Refusing a reparse/link path: ' + str(part))


def stopped():
    if os.name != 'nt':
        raise porter.PorterError('This runtime gate is Windows-only')
    script = "$ErrorActionPreference='Stop'; $p=@(Get-Process | Where-Object { $_.ProcessName -in @('Antigravity','language_server') } | Select-Object ProcessName,Id); ConvertTo-Json -InputObject $p -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise porter.PorterError('Could not verify that the App is stopped')
    active = json.loads(result.stdout or '[]')
    if active:
        raise porter.PorterError('Antigravity/backend still running: ' + json.dumps(active))


def no_journals(db):
    if any(Path(str(db) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise porter.PorterError('SQLite sidecar still present; require a clean shutdown: ' + str(db))


def checkpoint_summary(run):
    """Back up a stopped store including WAL, then checkpoint via SQLite itself."""
    import time
    stopped()
    if run.exists():
        raise porter.PorterError('Checkpoint backup directory must be new')
    db = Path.home().resolve() / '.gemini/antigravity/conversation_summaries.db'
    no_links(db)
    run.mkdir(parents=True)
    raw_backups = {}
    for suffix in ('', '-wal', '-shm', '-journal'):
        path = Path(str(db) + suffix)
        if path.exists():
            before = digest(path)
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != before or digest(path) != before:
                raise porter.PorterError('SQLite files changed while backing up')
            (run / path.name).write_bytes(data)
            raw_backups[str(path)] = before
    snapshot = run / 'consistent-snapshot.sqlite'
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2)) as source, closing(sqlite3.connect(snapshot)) as target:
        deadline = time.monotonic() + 15
        def bounded(*args):
            if time.monotonic() > deadline:
                raise porter.PorterError('SQLite backup timed out')
        source.backup(target, pages=128, progress=bounded, sleep=.05)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise porter.PorterError('Pre-checkpoint snapshot integrity failed')
        original_rows = target.execute('SELECT * FROM conversation_summaries ORDER BY conversation_id').fetchall()
    stopped()
    with closing(sqlite3.connect(db.as_uri() + '?mode=rw', uri=True, timeout=2)) as con:
        result = con.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if result[0] != 0:
            raise porter.PorterError('Database became busy; checkpoint was not completed')
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or con.execute('SELECT * FROM conversation_summaries ORDER BY conversation_id').fetchall() != original_rows:
            raise porter.PorterError('Checkpoint validation failed; backup retained')
    stopped()
    no_journals(db)
    report = {'status': 'checkpointed', 'raw_backups': raw_backups,
              'consistent_snapshot': str(snapshot), 'summary_rows_preserved': True,
              'integrity': 'ok', 'checkpoint_result': result, 'completed_at': porter.now()}
    save_json(run / 'checkpoint.json', report)
    return {'status': 'checkpointed', 'backup_directory': str(run), 'summary_rows_preserved': True}


def save_json(path, value):
    # Work-directory journal updates are atomically replaced too.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.porter-journal-', dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(porter.dumps(value).encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def publish(target, data, expected, guard):
    no_links(target)
    if not target.parent.is_dir():
        raise porter.PorterError('Target parent does not exist')
    guard()
    if digest(target) != expected:
        raise porter.PorterError('Target changed since backup: ' + str(target))
    fd, temporary = tempfile.mkstemp(prefix='.porter-publish-', dir=target.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        guard()
        if digest(target) != expected:
            raise porter.PorterError('Concurrent target change: ' + str(target))
        if expected is None:
            os.link(temp_path, target)  # Atomic create-if-absent on NTFS.
        else:
            os.replace(temp_path, target)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def rollback(entries, guard):
    guard()
    # Refuse all recovery before modifying anything if unrelated later edits exist.
    for entry in entries:
        current = digest(Path(entry['target']))
        if current not in (entry['before'], entry['after']):
            raise porter.PorterError('Recovery refused: target changed after this install: ' + entry['target'])
        if entry['before'] is not None and digest(Path(entry['backup'])) != entry['before']:
            raise porter.PorterError('Recovery backup is missing or changed')
    for entry in reversed(entries):
        target = Path(entry['target'])
        if digest(target) == entry['before']:
            continue
        if entry['before'] is None:
            guard()
            no_links(target)
            if digest(target) != entry['after']:
                raise porter.PorterError('Concurrent change during recovery')
            target.unlink()  # Exactly the new file, verified by content hash.
        else:
            publish(target, Path(entry['backup']).read_bytes(), entry['after'], guard)


def apply_entries(entries, journal, guard, check_after=None):
    """Small testable publisher; never accepts unverified backup/content hashes."""
    if journal.exists():
        raise porter.PorterError('Publication journal already exists')
    for entry in entries:
        no_links(Path(entry['target']))
        if digest(Path(entry['source'])) != entry['after']:
            raise porter.PorterError('Staged content changed')
        if entry['before'] is not None and digest(Path(entry['backup'])) != entry['before']:
            raise porter.PorterError('Original backup changed')
        if digest(Path(entry['target'])) != entry['before']:
            raise porter.PorterError('Target changed before installation')
    guard()
    state = {'status': 'prepared', 'entries': entries, 'created_at': porter.now()}
    save_json(journal, state)
    applied = []
    try:
        state['status'] = 'applying'
        save_json(journal, state)
        for entry in entries:
            publish(Path(entry['target']), Path(entry['source']).read_bytes(), entry['before'], guard)
            applied.append(entry)
            state['published_count'] = len(applied)
            save_json(journal, state)
        guard()
        if any(digest(Path(e['target'])) != e['after'] for e in entries):
            raise porter.PorterError('Post-install file hash mismatch')
        if check_after:
            check_after()
        state['status'] = 'installed'
        state['completed_at'] = porter.now()
        save_json(journal, state)
    except BaseException as exc:
        state['error'] = str(exc)
        try:
            rollback(applied, guard)
            state['status'] = 'rolled-back'
        except BaseException as recovery_error:
            state['status'] = 'recovery-required'
            state['recovery_error'] = str(recovery_error)
        save_json(journal, state)
        raise
    return state


def layout(profile, ui):
    store = profile / '.gemini/antigravity'
    return {'store': store, 'summary': store / 'conversation_summaries.db',
            'cache': store / 'agyhub_summaries_proto.pb',
            'projects': profile / '.gemini/config/projects', 'ui': ui / 'app_storage.json'}


def prepare(trial, run):
    stopped()
    if run.exists():
        raise porter.PorterError('Installation run directory must be new')
    report = porter.read_json(trial / 'trial.json')
    if report.get('format') != 'antigravity-desktop-trial/0.1' or report.get('synthetic_only') is not True:
        raise porter.PorterError('Only the prepared synthetic trial is allowed')
    binary_root = Path(os.environ['LOCALAPPDATA']) / 'Programs/antigravity'
    for name, key in [('Antigravity.exe', 'exe_sha256'), ('resources/app.asar', 'asar_sha256')]:
        if digest(binary_root / name) != trial_api.APP_BUILD[key]:
            raise porter.PorterError('Installed application build differs from the observed sample')
    profile = Path.home().resolve()
    ui = Path(os.environ['APPDATA']) / 'Antigravity'
    live = layout(profile, ui)
    for path in live.values():
        no_links(path)
    no_journals(live['summary'])
    new_id, new_project = report['conversation_id'], report['project_id']
    new_db = live['store'] / 'conversations' / (new_id + '.db')
    new_config = live['projects'] / (new_project + '.json')
    if new_db.exists() or new_config.exists():
        raise porter.PorterError('Test conversation/project already exists')
    run.mkdir(parents=True)
    backup = run / 'backup'
    backup.mkdir()
    clone = run / 'staged-profile'
    clone.mkdir()
    (clone / '.porter-isolated-profile').write_text('Offline staging only.\n')
    staged = layout(clone, clone / 'AppData/Roaming/Antigravity')
    sources = [live['summary'], live['cache'], live['ui']]
    # Additional reference-only backups; these are not publication targets.
    sources += sorted(live['projects'].glob('*.json'))
    sources += sorted((live['store'] / 'conversations').glob('*.db'))
    backup_index = {}
    for i, source in enumerate(sources):
        if source.suffix == '.db':
            no_journals(source)
        no_links(source)
        initial = digest(source)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != initial or digest(source) != initial:
            raise porter.PorterError('Source changed during backup')
        saved = backup / (str(i).zfill(3) + source.suffix)
        with saved.open('xb') as f:
            f.write(data)
        backup_index[str(source)] = {'sha256': initial, 'backup': str(saved)}
    for name in ['summary', 'cache', 'ui']:
        staged[name].parent.mkdir(parents=True, exist_ok=True)
        staged[name].write_bytes(Path(backup_index[str(live[name])]['backup']).read_bytes())
    # The stage helper adds only the candidate config/db, never copies old ones back.
    trial_api.stage_profile(trial, clone)
    with closing(sqlite3.connect(staged['summary'])) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise porter.PorterError('Staged summary database is invalid')
    for source, spec in backup_index.items():
        if digest(Path(source)) != spec['sha256']:
            raise porter.PorterError('Source changed while staging')
    stopped()
    targets = [(new_db, staged['store'] / 'conversations' / (new_id + '.db')),
               (new_config, staged['projects'] / (new_project + '.json')),
               (live['summary'], staged['summary']), (live['cache'], staged['cache']), (live['ui'], staged['ui'])]
    entries = []
    for target, source in targets:
        original = backup_index.get(str(target))
        entries.append({'target': str(target), 'source': str(source), 'after': digest(source),
                        'before': original['sha256'] if original else None,
                        'backup': original['backup'] if original else None})
    plan = {'trial': str(trial), 'app_build': trial_api.APP_BUILD, 'conversation_id': new_id,
            'project_id': new_project, 'title': report['title'], 'entries': entries,
            'backup_index': backup_index, 'prepared_at': porter.now(), 'status': 'ready-for-install'}
    save_json(run / 'install-plan.json', plan)
    return {'plan': str(run / 'install-plan.json'), 'backed_up_files': len(backup_index),
            'writes': [{'path': e['target'], 'action': 'create' if e['before'] is None else 'add-registration'} for e in entries]}


def validate_live_targets(plan):
    import uuid
    for key in ('conversation_id', 'project_id'):
        if str(uuid.UUID(plan[key])) != plan[key]:
            raise porter.PorterError('Invalid fixture identifier')
    live = layout(Path.home().resolve(), Path(os.environ['APPDATA']) / 'Antigravity')
    expected = {live['store'] / 'conversations' / (plan['conversation_id'] + '.db'), live['summary'], live['cache']}
    if plan.get('operation') != 'repair-synthetic':
        expected.update({live['projects'] / (plan['project_id'] + '.json'), live['ui']})
    actual = [Path(e['target']) for e in plan['entries']]
    if len(actual) != len(expected) or set(actual) != expected:
        raise porter.PorterError('Plan contains unexpected live write targets')
    return live


def prepare_repair(trial, run):
    """Repair the existing synthetic conversation; never overwrite new turns."""
    stopped()
    if run.exists():
        raise porter.PorterError('Repair run directory must be new')
    report = porter.read_json(trial / 'trial.json')
    if report.get('format') != 'antigravity-desktop-trial/0.1' or report.get('synthetic_only') is not True:
        raise porter.PorterError('Only the synthetic trial may be repaired')
    candidate = trial / 'candidate'
    conversation_id, project_id = report['conversation_id'], report['project_id']
    source_db = candidate / (conversation_id + '.db')
    if digest(source_db) != report['candidate_sha256']:
        raise porter.PorterError('Fixed candidate changed')
    with closing(sqlite3.connect(source_db)) as c:
        if c.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise porter.PorterError('Repair candidate is missing desktop format version')
    live = layout(Path.home().resolve(), Path(os.environ['APPDATA']) / 'Antigravity')
    target_db = live['store'] / 'conversations' / (conversation_id + '.db')
    targets = [target_db, live['summary'], live['cache']]
    for target in targets:
        no_links(target)
        if target.suffix == '.db':
            no_journals(target)
    if not (live['projects'] / (project_id + '.json')).exists():
        raise porter.PorterError('Expected test project is missing')
    run.mkdir(parents=True)
    backup, staged = run / 'backup', run / 'staged'
    backup.mkdir(); staged.mkdir()
    originals = {}
    protected = sorted(live['projects'].glob('*.json')) + sorted((live['store'] / 'conversations').glob('*.db')) + [live['ui']]
    sources = list(dict.fromkeys(targets + protected))
    for i, source in enumerate(sources):
        no_links(source)
        if source.suffix == '.db':
            no_journals(source)
        before = digest(source)
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != before or digest(source) != before:
            raise porter.PorterError('Source changed during repair backup')
        saved = backup / (str(i).zfill(3) + source.suffix)
        saved.write_bytes(data)
        originals[str(source)] = {'sha256': before, 'backup': str(saved)}
    expected = porter.read_json(trial / 'expected.json')['messages']
    old_db = Path(originals[str(target_db)]['backup'])
    if trial_api.decode_messages(old_db) != expected or trial_api.decode_messages(source_db) != expected:
        raise porter.PorterError('The test conversation has new/changed turns; repair refused')
    (staged / target_db.name).write_bytes(source_db.read_bytes())
    new_summary = staged / 'conversation_summaries.db'
    new_summary.write_bytes(Path(originals[str(live['summary'])]['backup']).read_bytes())
    row = porter.read_json(candidate / 'summary.json')
    raw = (candidate / 'raw-summary.pb').read_bytes()
    if row['conversation_id'] != conversation_id or row['project_id'] != project_id:
        raise porter.PorterError('Repair identity mismatch')
    row['raw_summary'] = raw
    with closing(sqlite3.connect(new_summary)) as c:
        before_others = c.execute('SELECT * FROM conversation_summaries WHERE conversation_id<>? ORDER BY conversation_id', (conversation_id,)).fetchall()
        if c.execute('SELECT count(*) FROM conversation_summaries WHERE conversation_id=?', (conversation_id,)).fetchone()[0] != 1:
            raise porter.PorterError('Expected synthetic summary is missing')
        columns = [name for name in row if name != 'conversation_id']
        c.execute('UPDATE conversation_summaries SET ' + ','.join(name + '=?' for name in columns) + ' WHERE conversation_id=?', [row[name] for name in columns] + [conversation_id])
        c.commit()
        if c.execute('SELECT * FROM conversation_summaries WHERE conversation_id<>? ORDER BY conversation_id', (conversation_id,)).fetchall() != before_others:
            raise porter.PorterError('Other summary rows changed')
        if c.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise porter.PorterError('Repaired index integrity check failed')
    new_cache = staged / 'agyhub_summaries_proto.pb'
    new_cache.write_bytes(trial_api.replace_cache_entry(Path(originals[str(live['cache'])]['backup']).read_bytes(), conversation_id, raw))
    entries = []
    for target, source in zip(targets, [staged / target_db.name, new_summary, new_cache]):
        original = originals[str(target)]
        entries.append({'target': str(target), 'source': str(source), 'after': digest(source),
                        'before': original['sha256'], 'backup': original['backup']})
    stopped()
    if any(digest(Path(path)) != data['sha256'] for path, data in originals.items()):
        raise porter.PorterError('Source changed during repair preparation')
    plan = {'operation': 'repair-synthetic', 'trial': str(trial), 'conversation_id': conversation_id,
            'project_id': project_id, 'title': report['title'], 'app_build': trial_api.APP_BUILD,
            'entries': entries, 'backup_index': originals, 'prepared_at': porter.now(), 'status': 'ready-for-repair'}
    validate_live_targets(plan)
    save_json(run / 'install-plan.json', plan)
    return {'plan': str(run / 'install-plan.json'), 'operation': 'repair-synthetic',
            'writes': [str(target) for target in targets], 'backed_up_files': len(originals)}


def apply(run):
    plan = porter.read_json(run / 'install-plan.json')
    live = validate_live_targets(plan)
    stopped()
    no_journals(live['summary'])
    binary_root = Path(os.environ['LOCALAPPDATA']) / 'Programs/antigravity'
    for name, key in [('Antigravity.exe', 'exe_sha256'), ('resources/app.asar', 'asar_sha256')]:
        if digest(binary_root / name) != trial_api.APP_BUILD[key]:
            raise porter.PorterError('Application build changed')
    changed = {e['target'] for e in plan['entries']}
    def check_after():
        for name, spec in plan['backup_index'].items():
            if name not in changed and digest(Path(name)) != spec['sha256']:
                raise porter.PorterError('An existing conversation/project changed')
    state = apply_entries(plan['entries'], run / 'installation-journal.json', stopped, check_after)
    return {'status': state['status'], 'conversation_id': plan['conversation_id'], 'title': plan['title'],
            'backup_directory': str(run / 'backup'), 'native_ui': 'NOT RUN', 'native_replay': 'NOT RUN'}


def recover(run):
    plan = porter.read_json(run / 'install-plan.json')
    validate_live_targets(plan)
    journal = porter.read_json(run / 'installation-journal.json')
    if journal['entries'] != plan['entries']:
        raise porter.PorterError('Plan and journal differ')
    rollback(journal['entries'], stopped)
    journal['status'] = 'rolled-back'
    save_json(run / 'installation-journal.json', journal)
    return {'status': 'rolled-back'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['prepare', 'prepare-repair', 'checkpoint', 'apply', 'recover'])
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--trial', type=Path)
    args = p.parse_args()
    try:
        if args.command in ('prepare', 'prepare-repair') and args.trial is None:
            raise porter.PorterError('--trial is required for prepare')
        if args.command == 'prepare':
            result = prepare(args.trial.resolve(), args.run.resolve())
        elif args.command == 'prepare-repair':
            result = prepare_repair(args.trial.resolve(), args.run.resolve())
        elif args.command == 'checkpoint':
            result = checkpoint_summary(args.run.resolve())
        elif args.command == 'apply':
            result = apply(args.run.resolve())
        else:
            result = recover(args.run.resolve())
        print(porter.dumps(result))
        return 0
    except (porter.PorterError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as e:
        print(porter.dumps({'error': str(e)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
