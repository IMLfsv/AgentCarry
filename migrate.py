"""Batch Codex projects/chats -> portable workspace or Antigravity desktop.

Run --help. All exports and migration plans are PRIVATE user data.
"""
from __future__ import annotations
import argparse
import json
import os
from filesystem import Path, sqlite_uri
import sqlite3
import sys
import uuid
import zipfile
import porter
import publication as files
import transfer
from adapters import antigravity

TARGETS = {'portable': None, antigravity.ADAPTER: antigravity}
PLAN_FORMAT = 'agent-project-porter/plan/1'


def make_plan(job_path, destination, run, target='portable', config=None,
              allow_lossy=False, activate_context=False, max_total=porter.MAX_TOTAL):
    if target not in TARGETS:
        raise porter.PorterError('Unknown target adapter')
    destination, run = destination.absolute(), run.absolute()
    for path in (destination, run):
        files.no_links(path)
    if run.exists() or porter.inside(run, destination) or porter.inside(destination, run):
        raise porter.PorterError('Plan directory must be new and separate from restored workspaces')
    parts = transfer.load_job(job_path, max_total)
    native = TARGETS[target]
    if native:
        if not config:
            raise porter.PorterError('Native target configuration is required')
        ledger = native.read_ledger(config)
        profile = native.paths(config['home'], config['ui'])
        for path in (run, destination):
            for protected in (profile['store'], profile['projects'], profile['ui_root']):
                if porter.inside(path, protected) or porter.inside(protected, path):
                    raise porter.PorterError('Work/staging directories must not overlap target storage')
    else:
        ledger_path = destination / 'porter-portable.json'
        ledger = porter.read_json(ledger_path) if ledger_path.exists() else {'projects': {}, 'conversations': {}}
    projects, blockers, warnings = [], [], []
    # Preflight every project before restoring any of them.
    for part in parts:
        root = destination / transfer.folder_name(part['name'], part['key'])
        workspace = root / 'project'
        previous = ledger['projects'].get(part['key'])
        if previous:
            if (previous['fingerprint'] != part['fingerprint'] or
                    Path(previous['workspace']) != workspace or
                    previous.get('activate_context', False) != activate_context):
                raise porter.PorterError('Previously imported project changed or destination differs; '
                                        'incremental merge is not supported: ' + part['name'])
            if not workspace.is_dir():
                raise porter.PorterError('Previously imported workspace is missing')
            if native:
                for thread in part['manifest']['threads']:
                    existing = ledger['conversations'].get(thread['id'])
                    if not existing or existing['project_key'] != part['key']:
                        raise porter.PorterError('Inconsistent existing conversation mapping')
                    cid = str(uuid.UUID(existing['target_id']))
                    if not (profile['conversations'] / (cid + '.db')).is_file():
                        raise porter.PorterError('Previously imported native conversation is missing')
                    from contextlib import closing
                    with closing(sqlite3.connect(sqlite_uri(profile['summary'], 'ro'), uri=True)) as con:
                        row = con.execute('SELECT project_id FROM conversation_summaries WHERE conversation_id=?', (cid,)).fetchone()
                        if row != (previous['target_project_id'],):
                            raise porter.PorterError('Previously imported conversation registration is missing or changed')
            projects.append({'key': part['key'], 'fingerprint': part['fingerprint'],
                'name': part['name'], 'workspace': str(workspace), 'reused': True,
                'target_project_id': previous.get('target_project_id'), 'conversations': [],
                'activate_context': activate_context})
            continue
        if root.exists():
            raise porter.PorterError('Restore destination exists without a matching receipt: ' + str(root))
        for thread in part['manifest']['threads']:
            if thread['id'] in ledger['conversations']:
                raise porter.PorterError('Conversation already imported under another project: ' + thread['id'])
        projects.append({'key': part['key'], 'fingerprint': part['fingerprint'], 'name': part['name'],
            'workspace': str(workspace), 'reused': False, 'target_project_id': str(uuid.uuid4()),
            'conversations': [], 'activate_context': activate_context})
    run.mkdir(parents=True)
    marker = run / '.porter-incomplete'
    marker.write_text('Plan unfinished; do not apply.\n')
    for part, project in zip(parts, projects):
        if project['reused']:
            continue
        root = Path(project['workspace']).parent
        suffixes = ['paths'] + ([f'context-{i:04d}' for i in range(1, len(part['contexts']) + 1)] if activate_context else [])
        if any(f'project/.agents/rules/porter-{part["key"][:12]}-{s}.md' in part['manifest']['entries'] for s in suffixes):
            blockers.append({'project': part['key'], 'code': 'migration_rule_name_conflict'})
        project['source_exclusions'] = part['manifest'].get('excluded', [])
        project['source_warnings'] = part['manifest'].get('warnings', [])
        project['source_scope_notes'] = part.get('scope_notes', [])
        for thread in part['manifest']['threads']:
            messages, issues = transfer.preview_messages(part['path'], thread)
            chat = {'source_id': thread['id'], 'title': thread['title'],
                    'target_id': str(uuid.uuid4()), 'trajectory_id': str(uuid.uuid4()),
                    'messages': messages, 'issues': issues,
                    'source_projection_omissions': thread.get('projection_omissions', {}),
                    'events_archive': str(root / thread['events']),
                    'original_title_preserved_in_archive': True}
            project['conversations'].append(chat)
            if native:
                if not messages or messages[0]['role'] != 'user':
                    blockers.append({'conversation': thread['id'], 'code': 'native_history_must_start_with_user_text'})
                if issues and not allow_lossy:
                    blockers.append({'conversation': thread['id'], 'code': 'projection_requires_allow_lossy',
                                     'issue_count': len(issues)})
        if project['source_exclusions']:
            warnings.append({'project': part['key'], 'code': 'source_files_excluded_by_export_policy',
                             'count': len(project['source_exclusions'])})
        if part['contexts'] and not activate_context:
            warnings.append({'project': part['key'], 'code': 'explicit_context_archived_not_activated'})
    if native:
        warnings += [{'code': 'native_title_may_be_replaced_by_app', 'source_title_preserved': True},
                     {'code': 'native_message_times_regenerated', 'original_times_preserved_in_archive': True},
                     {'code': 'runtime_validation_scope', 'value': 'one text conversation on Windows 2.32.2; batch format tests separate'}]
    if not blockers:
        for part, project in zip(parts, projects):
            if not project['reused']:
                project['restore'] = transfer.restore_project(part, Path(project['workspace']).parent, activate_context)
    plan = {'format': PLAN_FORMAT, 'created_at': porter.now(), 'target': target,
            'target_config': config if native else None, 'projects': projects, 'blockers': blockers,
            'warnings': warnings, 'can_apply': not blockers, 'allow_lossy': allow_lossy,
            'entries': [], 'privacy': 'PRIVATE: includes conversations and local paths'}
    if native and not blockers:
        plan['entries'] = native.stage(run, config, projects, ledger)
    elif not native and not blockers:
        for project in projects:
            if project['reused']:
                continue
            ledger['projects'][project['key']] = {k: project[k] for k in
                ('fingerprint', 'workspace', 'target_project_id', 'name', 'activate_context')}
            for chat in project['conversations']:
                ledger['conversations'][chat['source_id']] = {'project_key': project['key']}
        files.save_json(destination / 'porter-portable.json', ledger)
    files.save_json(run / 'plan.json', plan)
    report = {k: v for k, v in plan.items() if k not in ('entries', 'projects', 'target_config')}
    report['projects'] = [{**{k: v for k, v in p.items() if k != 'conversations'},
                           'conversations': [{**{k: v for k, v in c.items() if k != 'messages'},
                                              'native_text_messages': len(c['messages']) if native else 0}
                                             for c in p['conversations']]} for p in projects]
    report['files_to_publish'] = len(plan['entries'])
    report['native_runtime_tested_for_this_job'] = False
    report['status'] = 'blocked' if blockers else ('ready-to-apply' if native else 'portable-restored')
    files.save_json(run / 'report.json', report)
    marker.unlink()
    return {'status': report['status'], 'plan': str(run / 'plan.json'), 'report': str(run / 'report.json'),
            'projects': len(projects), 'reused_projects': sum(p['reused'] for p in projects),
            'new_conversations': sum(len(p['conversations']) for p in projects), 'blockers': blockers}


def load_plan(path):
    path = path.absolute()
    if (path.parent / '.porter-incomplete').exists():
        raise porter.PorterError('Incomplete plan')
    plan = porter.read_json(path)
    if plan.get('format') != PLAN_FORMAT or plan.get('target') not in TARGETS:
        raise porter.PorterError('Unsupported migration plan')
    return path.parent, plan


def verify_plan(path):
    run, plan = load_plan(path)
    findings = [{'file': e['target'], 'matches_installed_snapshot': files.digest(Path(e['target'])) == e['after']}
                for e in plan['entries']]
    return {'can_apply': plan['can_apply'], 'workspaces_present': all(Path(p['workspace']).is_dir() for p in plan['projects']),
            'publication_snapshot_matches': all(f['matches_installed_snapshot'] for f in findings) if findings else None,
            'files': findings,
            'note': 'Snapshot check only. App continuation can legitimately change native files; this is not a UI/context test.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    scan = sub.add_parser('scan', help='List source projects and chats, metadata only')
    scan.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')))
    export = sub.add_parser('export', help='Export selected projects/chats/context using a JSON spec')
    export.add_argument('--spec', type=Path, required=True)
    export.add_argument('--out', type=Path, required=True)
    verify = sub.add_parser('verify-job', help='Verify every bundle and the aggregate size limit')
    verify.add_argument('job', type=Path)
    plan = sub.add_parser('plan', help='Restore projects and prepare target writes; does not write native profile')
    plan.add_argument('--job', type=Path, required=True)
    plan.add_argument('--to', type=Path, required=True)
    plan.add_argument('--out', type=Path, required=True)
    plan.add_argument('--target', choices=list(TARGETS), default='portable')
    plan.add_argument('--target-home', type=Path, default=Path.home())
    plan.add_argument('--target-ui', type=Path)
    plan.add_argument('--app-root', type=Path)
    plan.add_argument('--sandbox', action='store_true', help='Only an initialized disposable test profile; not runtime validation')
    plan.add_argument('--allow-lossy', action='store_true', help='Accept reported archive-only nontext/tool events')
    plan.add_argument('--activate-context', action='store_true', help='Activate explicitly selected context as workspace rules')
    for name in ('apply', 'recover', 'verify-plan'):
        p = sub.add_parser(name)
        p.add_argument('plan', type=Path)
    sandbox = sub.add_parser('init-sandbox', help='Create a new disposable empty format-test profile')
    sandbox.add_argument('--home', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'scan':
            rows, projects = porter.catalog(args.codex_home.resolve())
            result = {'projects': projects, 'threads': rows, 'count': len(rows)}
        elif args.command == 'export':
            result = transfer.export_job(args.spec, args.out)
        elif args.command == 'verify-job':
            parts = transfer.load_job(args.job)
            result = {'verified': True, 'projects': len(parts),
                      'conversations': sum(len(p['manifest']['threads']) for p in parts)}
        elif args.command == 'init-sandbox':
            result = antigravity.initialize_sandbox(args.home)
        elif args.command == 'plan':
            config = None
            if args.target == antigravity.ADAPTER:
                if args.sandbox:
                    default_ui = args.target_home / 'ui'
                elif args.target_home.absolute() != Path.home().absolute():
                    default_ui = args.target_home / 'AppData/Roaming/Antigravity'
                else:
                    default_ui = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'Antigravity'
                ui = args.target_ui or default_ui
                app = args.app_root or Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/antigravity'
                config = {'home': str(args.target_home.absolute()), 'ui': str(ui.absolute()),
                          'app_root': str(app.absolute()), 'sandbox': args.sandbox}
            result = make_plan(args.job, args.to, args.out, args.target, config, args.allow_lossy, args.activate_context)
        elif args.command == 'verify-plan':
            result = verify_plan(args.plan)
        else:
            run, stored = load_plan(args.plan)
            adapter = TARGETS[stored['target']]
            if not adapter:
                raise porter.PorterError('Portable planning restores files directly; apply/recover are native operations')
            result = adapter.apply(run, stored) if args.command == 'apply' else adapter.recover(run, stored)
            # Avoid printing the private per-file journal wholesale.
            result = {k: v for k, v in result.items() if k != 'entries'}
        print(porter.dumps(result))
        return 2 if result.get('status') == 'blocked' else 0
    except (porter.PorterError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(porter.dumps({'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
