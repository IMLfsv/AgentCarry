"""Hash-guarded file publication and recovery, independent of platform adapters."""
import hashlib
import os
from filesystem import Path
import tempfile
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


def apply_entries(entries, journal, guard, check_after=None, progress=lambda _: None):
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
        for index, entry in enumerate(entries, 1):
            progress({'stage': '写入目标客户端', 'detail': Path(entry['target']).name, 'completed': index - 1, 'total': len(entries), 'unit': '个文件'})
            publish(Path(entry['target']), Path(entry['source']).read_bytes(), entry['before'], guard)
            applied.append(entry)
            state['published_count'] = len(applied)
            save_json(journal, state)
            progress({'stage': '写入目标客户端', 'detail': Path(entry['target']).name, 'completed': index, 'total': len(entries), 'unit': '个文件'})
        progress({'stage': '核验写入结果', 'detail': '检查目标文件校验值'})
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
            progress({'stage': '导入异常，正在恢复', 'detail': '恢复本次已写入的文件'})
            rollback(applied, guard)
            state['status'] = 'rolled-back'
        except BaseException as recovery_error:
            state['status'] = 'recovery-required'
            state['recovery_error'] = str(recovery_error)
        save_json(journal, state)
        raise
    return state


