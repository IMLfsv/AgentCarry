"""UI-independent workflow; no user data is read until an action requests it."""
from __future__ import annotations
from collections import Counter
from datetime import datetime
import os
from filesystem import Path, sqlite_uri
import uuid
import migrate
import porter
import publication
import transfer
from adapters import antigravity

VERSION = '0.4.5-experimental'


def defaults(home=None, env=None):
    home = Path(home) if home is not None else Path.home()
    env = dict(os.environ) if env is None else env
    return {'codex_home': str(Path(env.get('CODEX_HOME', home / '.codex'))),
            'output': str(home / 'Documents/AgentCarry'),
            'home': str(home), 'ui': str(Path(env.get('APPDATA', home / 'AppData/Roaming')) / 'Antigravity'),
            'app_root': str(Path(env.get('LOCALAPPDATA', home / 'AppData/Local')) / 'Programs/antigravity')}


def source_projects(home, include_internal=False, include_archived=False):
    rows, registrations = porter.catalog(Path(home).expanduser().resolve())
    rows = [r for r in rows if (include_internal or r['task_kind'] == 'conversation')
            and (include_archived or not r.get('archived'))]
    names = {Path(r['path']).resolve(): r['name'] for r in registrations}
    candidates = set(names)
    candidates.update(Path(row['cwd']).resolve() for row in rows
                      if not any(porter.inside(Path(row['cwd']).resolve(), p) for p in names))
    roots = []
    for path in sorted(candidates, key=lambda p: (len(p.parts), str(p).casefold())):
        if not any(porter.inside(path, root) for root in roots):
            roots.append(path)
    return [{'path': str(root), 'name': names.get(root) or root.name or str(root),
             'available': root.is_dir(),
             'threads': [{'id': r['id'], 'title': r.get('title') or '(未命名会话)', 'cwd': r['cwd'],
                          'archived': bool(r.get('archived')), 'task_kind': r['task_kind'],
                          'available': bool(r.get('rollout_path')) and porter.resolve_rollout(home, r['rollout_path']).is_file(),
                          'rollout_path': str(porter.resolve_rollout(home, r['rollout_path'])) if r.get('rollout_path') else '',
                          'compressed': bool(r.get('rollout_path')) and porter.resolve_rollout(home, r['rollout_path']).suffix == '.zst'} for r in rows
                         if porter.inside(Path(r['cwd']).resolve(), root)]}
            for root in roots]


def selection_spec(home, projects, selected_roots, selected_threads, contexts=()):
    selected, unavailable = [], []
    for project in projects:
        if project['path'] not in selected_roots:
            continue
        if not project['available']:
            raise porter.PorterError('项目目录不存在，请取消选择或恢复原目录：' + project['path'])
        missing = [t for t in project['threads'] if not t.get('available', True)
                   or (t.get('rollout_path') and not porter.resolve_rollout(home, t['rollout_path']).is_file())]
        missing_ids = {t['id'] for t in missing}
        unavailable.extend({'project': project['name'], 'id': t['id'], 'title': t['title'],
                            'path': t.get('rollout_path', '')} for t in missing)
        selected.append({'path': project['path'], 'name': project['name'],
                         'threads': [t['id'] for t in project['threads'] if t['id'] in selected_threads and t['id'] not in missing_ids],
                         'context_files': list(contexts)})
    if not selected:
        raise porter.PorterError('请至少选择一个项目。')
    return {'codex_home': str(Path(home).expanduser().absolute()), 'projects': selected, 'unavailable_chats': unavailable}


def prepare(output, target, config=None, selection=None, existing_job=None,
            allow_lossy=False, activate_context=False, progress=lambda _: None):
    output = Path(output).expanduser().absolute()
    publication.no_links(output)
    output = output.resolve()
    # Refuse mixed/missing inputs, and check compatibility before exporting data.
    if (selection is None) == (existing_job is None):
        raise porter.PorterError('请选择本机项目，或打开一个迁移包。')
    if target == antigravity.ADAPTER:
        progress('正在检查目标版本和退出状态…')
        antigravity.read_ledger(config)
    if selection:
        for item in selection['projects']:
            root = Path(item['path']).expanduser().resolve()
            if porter.inside(output, root) or porter.inside(root, output):
                raise porter.PorterError('迁移输出目录不能与源项目重叠：' + str(root))
        if porter.inside(output, Path(selection['codex_home']).resolve()):
            raise porter.PorterError('迁移输出目录不能位于 Codex 数据目录内。')
    run = output / 'runs' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    if selection:
        publication.save_json(run / 'selection.json', selection)
        progress('正在打包所选项目和会话…')
        transfer.export_job(run / 'selection.json', run / 'private-job', progress)
        job = run / 'private-job/job.json'
    else:
        job = Path(existing_job).absolute()
    progress('正在校验迁移内容并准备恢复…')
    result = migrate.make_plan(job, output / 'workspaces', run / 'plan', target, config,
                               allow_lossy, activate_context, progress=progress)
    result['job'] = str(job)
    result['session'] = str(run)
    result['summary'] = summary(Path(result['plan']))
    if selection and selection.get('unavailable_chats'):
        result['summary'] += '\n以下会话因源文件缺失未迁移；项目文件和其余所选会话不受影响：\n' + '\n'.join(
            f"- {t['project']} / {t['title']}（{t['id']}）\n  {t['path']}" for t in selection['unavailable_chats']) + '\n'
        publication.save_json(run / 'unavailable-chats.json', selection['unavailable_chats'])
    publication.save_json(run / 'session.json', {k: v for k, v in result.items() if k != 'summary'})
    (run / '迁移报告.txt').write_text(result['summary'], encoding='utf-8')
    return result


def apply(plan_path, progress=lambda _: None):
    progress({'stage': '检查导入环境与备份', 'detail': '确认目标客户端已退出，验证暂存文件与备份'})
    run, plan = migrate.load_plan(Path(plan_path))
    if plan['target'] != antigravity.ADAPTER:
        raise porter.PorterError('可移植模式已经恢复文件，无需原生导入。')
    result = antigravity.apply(run, plan, progress)
    verification = migrate.verify_plan(Path(plan_path))
    if verification['publication_snapshot_matches'] is False:
        raise porter.PorterError('导入后的文件核验失败，请查看 journal.json。')
    publication.save_json(run / 'verification.json', verification)
    return {'status': result['status'], 'verification': verification}


def recover(plan_path):
    run, plan = migrate.load_plan(Path(plan_path))
    if plan['target'] != antigravity.ADAPTER:
        raise porter.PorterError('只支持恢复原生导入；已恢复的项目目录会保留。')
    return antigravity.recover(run, plan)


def checkpoint(config, output):
    """Explicit maintenance action, separate from read-only native planning."""
    from contextlib import closing
    import hashlib
    import sqlite3
    import time
    layout = antigravity.guard(config, check_journals=False)
    root = Path(output).expanduser().absolute()
    publication.no_links(root)
    if any(porter.inside(root, layout[k]) or porter.inside(layout[k], root)
           for k in ('store', 'projects', 'ui_root')):
        raise porter.PorterError('备份目录不能与 App 数据目录重叠。')
    backup = root / 'maintenance' / uuid.uuid4().hex
    backup.mkdir(parents=True)
    db = layout['summary']
    saved = {}
    for suffix in ('', '-wal', '-shm', '-journal'):
        path = Path(str(db) + suffix)
        if path.exists():
            before = publication.digest(path)
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != before or publication.digest(path) != before:
                raise porter.PorterError('数据库正在变化，已停止整理。')
            (backup / path.name).write_bytes(data)
            saved[path.name] = before
    deadline = time.monotonic() + 15
    def bounded(*_):
        if time.monotonic() > deadline:
            raise porter.PorterError('数据库备份超时，已停止整理。')
    with closing(sqlite3.connect(sqlite_uri(db, 'ro'), uri=True, timeout=2)) as source, \
            closing(sqlite3.connect(backup / 'consistent-snapshot.sqlite')) as copy:
        source.backup(copy, pages=128, progress=bounded, sleep=.05)
        if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise porter.PorterError('数据库备份未通过校验。')
        rows = copy.execute('SELECT * FROM conversation_summaries ORDER BY conversation_id').fetchall()
    antigravity.guard(config, check_journals=False)
    with closing(sqlite3.connect(sqlite_uri(db, 'rw'), uri=True, timeout=2)) as con:
        result = con.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if result[0] or con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or \
                con.execute('SELECT * FROM conversation_summaries ORDER BY conversation_id').fetchall() != rows:
            raise porter.PorterError('数据库整理后的校验未通过，备份已保留：' + str(backup))
    antigravity.guard(config)
    report = {'status': 'checkpointed', 'backup': str(backup), 'raw_backup_hashes': saved,
              'summary_rows_preserved': True, 'summary_row_count': len(rows)}
    publication.save_json(backup / 'checkpoint.json', report)
    return report


def summary(plan_path):
    _, plan = migrate.load_plan(Path(plan_path))
    native = plan['target'] == antigravity.ADAPTER
    chats = [c for p in plan['projects'] for c in p['conversations']]
    issues = Counter()
    for chat in chats:
        for issue in chat['issues']:
            issues[issue['code']] += issue.get('count', 1)
    lines = ['迁移报告', '', '状态：' + ('需要处理以下问题' if plan['blockers'] else
                                     '可以执行原生导入' if native else '项目文件与对话档案已恢复'),
             f'项目：{len(plan["projects"])}；本次新会话：{len(chats)}；复用项目：{sum(p["reused"] for p in plan["projects"])}',
             f'文字消息：{sum(len(c["messages"]) for c in chats)}；原生转换差异：{sum(issues.values())}', '']
    labels = {'tool_or_event_archived_only': '工具调用/其他事件仅存档',
              'nontext_content_archived_only': '图片或非文本内容仅存档',
              'partial_source_history': '源端只有部分历史',
              'unsupported_response_item_archived_only': '未知源记录仅存档',
              'empty_text_not_inserted': '无文字的消息未写入原生会话'}
    lines += [f'- {labels.get(k, k)}：{v}' for k, v in issues.items()]
    for blocker in plan['blockers']:
        if blocker['code'] == 'projection_requires_allow_lossy':
            lines.append('需处理：如接受上述内容仅保留在档案中，请勾选“接受转换差异”后重新准备。')
        else:
            lines.append('需处理：' + blocker['code'])
    for project in plan['projects']:
        lines += ['', project['name'], '恢复目录：' + project['workspace'],
                  '状态：' + ('复用已有映射' if project['reused'] else '待恢复' if plan['blockers'] else '已恢复')]
        if project.get('source_exclusions'):
            lines.append(f'按策略排除 {len(project["source_exclusions"])} 项，详情见 report.json。')
        for chat in project['conversations'][:30]:
            lines.append('  • ' + str(chat['title']) + (' → ' + chat['target_id'] if native else ''))
        if len(project['conversations']) > 30:
            lines.append('  其余会话见 report.json。')
    if native:
        lines += ['', '当前原生适配：Windows Antigravity App 2.32.2。',
                  '原标题保存在档案中，但客户端可能改用首条消息显示；原生消息时间重新生成。',
                  '导入后请检查：能续聊、找到新项目文件、重启仍保留。此报告不代表这些客户端检查已通过。']
    lines += ['', '完整差异报告：' + str(Path(plan_path).parent / 'report.json'),
              '恢复操作记录：' + str(plan_path)]
    return '\n'.join(lines) + '\n'


def explain_error(error):
    text = str(error)
    if isinstance(error, FileNotFoundError) and str(error.filename or '').lower().endswith('.jsonl'):
        return ('Codex 索引仍有这条会话，但聊天文件不存在，可能已移动、归档或删除。\n'
                '请返回“选择内容”重新读取项目，查看标为“聊天文件缺失”的会话。取消该会话后可迁移其余内容；如需保留它，请先从备份恢复文件。\n'
                '重新启动 Antigravity 无法恢复缺失的 Codex 文件。\n\n缺失文件：' + str(error.filename))
    guidance = [('Unsupported installed build', '当前 Antigravity 版本尚未适配。请使用可移植模式，或等待此版本的原生适配。'),
                ('Close Antigravity', '请完全退出 Antigravity 和后台进程，再重试。'),
                ('SQLite sidecars', '请完全退出 Antigravity；若仍提示此问题，可使用“整理数据库”并保留自动备份。'),
                ('not initialized', '请先正常启动 Antigravity 一次，建立数据目录后再完全退出。'),
                ('No Codex state_', '没有找到 Codex 聊天目录。请确认 Codex 数据目录，或打开已有迁移包。'),
                ('Unsupported Codex', '该 Codex 数据格式尚未支持，请保留原数据并提交不含私密内容的版本信息。'),
                ('incremental merge', '该项目已经迁入，但源内容或目标位置改变。当前版本不支持覆盖或合并后续工作。'),
                ('Target changed', '目标 App 数据在准备后发生变化，请重新准备。'),
                ('Recovery refused', '目标端已有后续修改，已拒绝覆盖。原有内容和备份均保留。')]
    for fragment, message in guidance:
        if fragment in text:
            return message + '\n\n详细信息：' + text
    return text


def app_action(config, action):
    """Operate only processes from the selected installation in this session."""
    import subprocess
    if action not in ('close', 'open', 'restart') or os.name != 'nt':
        raise porter.PorterError('此操作仅支持 Windows 的退出、打开或重启。')
    root = Path(config['app_root']).expanduser().resolve()
    exe = root / 'Antigravity.exe'
    publication.no_links(exe)
    if not exe.is_file():
        raise porter.PorterError('找不到 Antigravity.exe，请在迁移位置中选择正确的安装目录。')
    # Literal quoting: installation paths never become executable PowerShell code.
    quoted = str(root).rstrip('\\').replace("'", "''") + '\\'
    script = "$ErrorActionPreference='Stop'; $root='" + quoted + "'; " + r"""
$session=(Get-Process -Id $PID).SessionId
function Targets {
  @(Get-Process | Where-Object {
    $_.SessionId -eq $session -and $_.Path -and
    $_.Path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
  })
}
"""
    if action in ('close', 'restart'):
        script += r"""
foreach($p in (Targets)) { if($p.MainWindowHandle -ne 0) { [void]$p.CloseMainWindow() } }
$until=(Get-Date).AddSeconds(8)
while(@(Targets).Count -gt 0 -and (Get-Date) -lt $until) { Start-Sleep -Milliseconds 250 }
foreach($p in (Targets)) { $p.Kill() }
$until=(Get-Date).AddSeconds(5)
while(@(Targets).Count -gt 0 -and (Get-Date) -lt $until) { Start-Sleep -Milliseconds 250 }
if(@(Targets).Count -gt 0) { throw '仍有进程未退出，请检查权限后重试。' }
"""
    else:
        script += "if(@(Targets).Count -gt 0) { throw 'Antigravity 已在运行。需要重新打开时请使用重启。' }; "
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise porter.PorterError('Antigravity 操作未完成：' + result.stderr.strip())
    if action in ('open', 'restart'):
        subprocess.Popen([str(exe)], cwd=str(root))
    return {'status': action}
