"""Experimental Windows App 2.32.2 text-only fixture writer. OFFLINE ONLY.

One synthetic native continuation/restart case passed; general imports and
title fidelity remain unverified. This module only prepares/stages files;
the separate guarded installer writes live state. Staging needs an owner marker.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
import zipfile

import porter

from adapters.antigravity_codec import (APP_BUILD, SCHEMA, varint, num, blob, fields, one, timestamp, make_metadata, create_native_db, decode_messages, make_summary, append_cache, replace_cache_entry, project_order)


def prepare(source_project, trial, restored):
    source_project, trial, restored = source_project.resolve(), trial.resolve(), restored.resolve()
    if trial.exists() or restored.exists():
        raise porter.PorterError('Trial and restored directories must be new')
    if porter.inside(trial, source_project) or porter.inside(restored, source_project):
        raise porter.PorterError('Keep generated directories outside the source project')
    trial.mkdir(parents=True)
    home = trial / 'synthetic-codex'
    (home / 'sessions').mkdir(parents=True)
    source_id, conversation_id, trajectory_id, project_id = [str(uuid.uuid4()) for _ in range(4)]
    marker = 'MIG-CODEX-' + uuid.uuid4().hex[:10].upper()
    source_messages = [
        {'role': 'user', 'text': '这是合成 Codex 迁移测试。对话校验词是 ' + marker + '。请保留在对话上下文中，不要写入文件。'},
        {'role': 'assistant', 'text': '已记住本次对话的校验词，未写入项目文件。'},
        {'role': 'user', 'text': '项目包含 inputs/settings.json。之后需要读取它并计算 base 加 increment，等我下次明确要求再执行。'},
        {'role': 'assistant', 'text': '收到，等待下一次指令后再读取文件并计算。'},
    ]
    log = home / 'sessions' / ('rollout-synthetic-' + source_id + '.jsonl')
    records = [{'ordinal': 0, 'type': 'session_meta', 'payload':
                {'id': source_id, 'cwd': str(source_project), 'history_mode': 'paginated'}}]
    for i, message in enumerate(source_messages, 1):
        records.append({'ordinal': i, 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
            'type': 'UserMessage' if message['role'] == 'user' else 'AgentMessage', 'id': str(uuid.uuid4()),
            'content': [{'type': 'text', 'text': message['text']}]}}})
    log.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8', newline='\n')
    with closing(sqlite3.connect(home / 'state_5.sqlite')) as con:
        con.execute('CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT,title TEXT,history_mode TEXT,archived INTEGER)')
        con.execute('INSERT INTO threads VALUES(?,?,?,?,?,0)',
                    (source_id, str(source_project), str(log), 'Synthetic Codex native trial', 'paginated'))
        con.commit()
    bundle = trial / 'synthetic-source.zip'
    porter.export_bundle(home, source_project, bundle)
    porter.restore_bundle(bundle, restored)
    with zipfile.ZipFile(bundle) as z:
        manifest = json.loads(z.read('manifest.json'))
        events = [json.loads(line) for line in z.read(manifest['threads'][0]['events']).splitlines()]
    if any(event['kind'] != 'message' for event in events):
        raise porter.PorterError('This trial only supports text messages')
    messages = [{'role': e['role'], 'text': e['text']} for e in events]
    if messages != source_messages:
        raise porter.PorterError('Codex projection does not match the synthetic source')
    project = restored / 'project'
    if any(marker.encode() in p.read_bytes() for p in project.rglob('*') if p.is_file()):
        raise porter.PorterError('Conversation marker leaked into project files')
    uri = project.as_uri()
    seconds = int(time.time())
    candidate = trial / 'candidate'
    candidate.mkdir()
    db = candidate / (conversation_id + '.db')
    create_native_db(db, messages, conversation_id, trajectory_id, project_id, uri, seconds)
    if decode_messages(db) != messages:
        raise porter.PorterError('Candidate round-trip mismatch')
    title = 'Codex Migration Trial ' + conversation_id[:8]
    row, raw = make_summary(messages, conversation_id, trajectory_id, project_id, uri, seconds, title)
    (candidate / 'summary.json').write_text(porter.dumps(row), encoding='utf-8')
    (candidate / 'raw-summary.pb').write_bytes(raw)
    config = {'id': project_id, 'name': 'Codex migrated test project',
              'projectResources': {'resources': [{'folderUri': uri}]},
              'permissionGrants': {'v2Migrated': True}, 'settings': {}, 'isWorkspaceOnly': False}
    (candidate / 'project.json').write_text(porter.dumps(config), encoding='utf-8')
    report = {'format': 'antigravity-desktop-trial/0.1', 'synthetic_only': True, 'app_build': APP_BUILD,
              'source_thread_id': source_id, 'conversation_id': conversation_id, 'project_id': project_id,
              'title': title, 'restored_project': str(project), 'message_count': len(messages),
              'candidate_sha256': hashlib.sha256(db.read_bytes()).hexdigest(),
              'native_ui': 'NOT RUN', 'native_replay': 'NOT RUN', 'restart_persistence': 'NOT RUN',
              'calibration_conversation_reused': False, 'conversation_marker_absent_from_project': True,
              'limits': ['synthetic text-only trial', 'no native installation', 'no tools, media, private reasoning or runtime state']}
    (trial / 'trial.json').write_text(porter.dumps(report), encoding='utf-8')
    # Evaluator material stays outside the directory opened as an App project.
    (trial / 'expected.json').write_text(porter.dumps({'marker': marker, 'messages': messages, 'expected_result': '343'}), encoding='utf-8')
    return report


def stage_profile(trial, profile):
    """Operate on an explicitly marked offline profile clone, never live state."""
    trial, profile = trial.resolve(), profile.resolve()
    if profile == Path.home().resolve() or not (profile / '.porter-isolated-profile').is_file():
        raise porter.PorterError('Explicit offline profile ownership marker is required')
    if (profile / 'stage-receipt.json').exists():
        raise porter.PorterError('Profile has already been staged')
    report = porter.read_json(trial / 'trial.json')
    if report.get('format') != 'antigravity-desktop-trial/0.1' or report.get('synthetic_only') is not True:
        raise porter.PorterError('Only the generated synthetic fixture can be staged')
    candidate = trial / 'candidate'
    conversation_id, project_id = report['conversation_id'], report['project_id']
    for ident in (conversation_id, project_id):
        if str(uuid.UUID(ident)) != ident:
            raise porter.PorterError('Non-canonical fixture ID')
    source_db = candidate / (conversation_id + '.db')
    if hashlib.sha256(source_db.read_bytes()).hexdigest() != report['candidate_sha256']:
        raise porter.PorterError('Candidate database changed')
    store = profile / '.gemini/antigravity'
    config_dir = profile / '.gemini/config/projects'
    ui_path = profile / 'AppData/Roaming/Antigravity/app_storage.json'
    summary_db = store / 'conversation_summaries.db'
    cache = store / 'agyhub_summaries_proto.pb'
    target_db = store / 'conversations' / source_db.name
    project_config = config_dir / (project_id + '.json')
    if target_db.exists() or project_config.exists():
        raise porter.PorterError('Native fixture collision')
    if any(p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 0x400
           for p in profile.rglob('*')):
        raise porter.PorterError('Links/reparse points are forbidden in the offline profile')
    raw = (candidate / 'raw-summary.pb').read_bytes()
    new_cache = append_cache(cache.read_bytes(), conversation_id, raw)
    storage = project_order(porter.read_json(ui_path), project_id)
    row = porter.read_json(candidate / 'summary.json')
    row['raw_summary'] = raw
    con = sqlite3.connect(summary_db.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        con.execute('BEGIN IMMEDIATE')
        if con.execute('SELECT count(*) FROM conversation_summaries WHERE conversation_id=?', (conversation_id,)).fetchone()[0]:
            raise porter.PorterError('Summary collision')
        before = con.execute('SELECT count(*) FROM conversation_summaries').fetchone()[0]
        names = list(row)
        con.execute('INSERT INTO conversation_summaries (' + ','.join(names) + ') VALUES (' + ','.join('?' for _ in names) + ')', list(row.values()))
        con.commit()
        after = con.execute('SELECT count(*) FROM conversation_summaries').fetchone()[0]
    finally:
        con.close()
    # This is intentionally an offline rehearsal, not a live multi-file transaction.
    target_db.parent.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    with target_db.open('xb') as writer:
        writer.write(source_db.read_bytes())
    with project_config.open('xb') as writer:
        writer.write((candidate / 'project.json').read_bytes())
    cache.write_bytes(new_cache)
    ui_path.write_text(porter.dumps(storage), encoding='utf-8')
    receipt = {'offline_rehearsal': True, 'summary_rows_before': before, 'summary_rows_after': after,
               'conversation_id': conversation_id, 'project_id': project_id, 'native_ui': 'NOT RUN'}
    (profile / 'stage-receipt.json').write_text(porter.dumps(receipt), encoding='utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--source-project', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--restored-to', type=Path, required=True)
    p = sub.add_parser('stage-offline')
    p.add_argument('--trial', type=Path, required=True)
    p.add_argument('--profile', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.source_project, args.out, args.restored_to) if args.command == 'prepare' else stage_profile(args.trial, args.profile)
        print(porter.dumps(result))
        return 0
    except (porter.PorterError, OSError, ValueError, sqlite3.Error) as exc:
        print(porter.dumps({'error': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
