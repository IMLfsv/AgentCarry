"""Antigravity desktop storage codec, independent of fixtures and user profiles."""
from contextlib import closing
import json
from filesystem import Path, sqlite_uri
import sqlite3
import uuid
import porter

APP_BUILD = {'version': '2.32.2',
             'exe_sha256': '67ef7a1b02904cd8ace5942e339910767070ac2f63f0d8d97b6ee1e163f7dbb0',
             'asar_sha256': '056ba838faf9b75c1382a6a0ae003c4b051a3c3e50060a9e5b34987313492d77'}
SCHEMA = [
    'CREATE TABLE trajectory_meta (trajectory_id TEXT PRIMARY KEY,cascade_id TEXT,trajectory_type INTEGER,source INTEGER)',
    'CREATE TABLE steps (idx INTEGER PRIMARY KEY,step_type INTEGER NOT NULL DEFAULT 0,status INTEGER NOT NULL DEFAULT 0,has_subtrajectory NUMERIC NOT NULL DEFAULT false,metadata BLOB,error_details BLOB,permissions BLOB,task_details BLOB,render_info BLOB,step_payload BLOB,step_format INTEGER NOT NULL DEFAULT 0)',
    'CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY,data BLOB,size INTEGER NOT NULL DEFAULT 0)',
    'CREATE TABLE executor_metadata (idx INTEGER PRIMARY KEY,data BLOB)',
    'CREATE TABLE parent_references (idx INTEGER PRIMARY KEY,data BLOB)',
    'CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY DEFAULT "main",data BLOB)',
    'CREATE TABLE battle_mode_infos (idx INTEGER PRIMARY KEY,data BLOB)',
    'CREATE INDEX idx_steps_status ON steps(status)',
    'CREATE INDEX idx_steps_step_type ON steps(step_type)',
]


def varint(value):
    if type(value) is not int or not 0 <= value < 2**64:
        raise porter.PorterError('Invalid unsigned varint')
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out) + bytes([value])


def num(field, value):
    return varint(field << 3) + varint(value)


def blob(field, value):
    value = value.encode('utf-8') if isinstance(value, str) else value
    return varint((field << 3) | 2) + varint(len(value)) + value


def fields(data):
    """Bounded wire reader. No descriptor or executable vendor code is used."""
    if not isinstance(data, bytes) or len(data) > 32 * 1024 * 1024:
        raise porter.PorterError('Invalid protobuf size')
    offset, result = 0, []

    def read_varint():
        nonlocal offset
        value = 0
        for i in range(10):
            if offset >= len(data):
                raise porter.PorterError('Truncated varint')
            byte = data[offset]
            offset += 1
            if i == 9 and byte > 1:
                raise porter.PorterError('Oversized varint')
            value |= (byte & 127) << (7 * i)
            if byte < 128:
                return value
        raise porter.PorterError('Invalid varint')

    while offset < len(data):
        start = offset
        tag = read_varint()
        field, wire = tag >> 3, tag & 7
        if not 0 < field < 2**29 or len(result) >= 100000:
            raise porter.PorterError('Invalid protobuf field')
        if wire == 0:
            value = read_varint()
        elif wire in (1, 2, 5):
            size = read_varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data) - offset:
                raise porter.PorterError('Truncated protobuf value')
            value = data[offset:offset + size]
            offset += size
        else:
            raise porter.PorterError('Unsupported wire type')
        result.append((field, wire, value, data[start:offset]))
    return result


def one(data, number, wire):
    found = [v for n, w, v, _ in fields(data) if n == number and w == wire]
    if len(found) != 1:
        raise porter.PorterError('Required unique field missing: ' + str(number))
    return found[0]


def timestamp(seconds):
    return num(1, seconds)


def make_metadata(conversation_id, project_id, workspace_uri, seconds):
    return (blob(1, blob(1, workspace_uri)) + blob(2, timestamp(seconds)) +
            blob(6, conversation_id) + blob(7, workspace_uri) + blob(18, project_id))


def create_native_db(path, messages, conversation_id, trajectory_id, project_id, workspace_uri, seconds):
    if path.exists():
        raise porter.PorterError('Native candidate already exists')
    if not messages or messages[0]['role'] != 'user':
        raise porter.PorterError('Text fixture must start with a user message')
    con = sqlite3.connect(path)
    try:
        # Version zero triggers the desktop backend's legacy migration, which
        # discards the modern root metadata and project binding on cold start.
        con.execute('PRAGMA page_size=32768')
        con.execute('PRAGMA user_version=1')
        for statement in SCHEMA:
            con.execute(statement)
        # Desktop App sample source=1; CLI source=17 must not be substituted.
        con.execute('INSERT INTO trajectory_meta VALUES(?,?,4,1)', (trajectory_id, conversation_id))
        for idx, message in enumerate(messages):
            if message['role'] not in ('user', 'assistant') or not isinstance(message['text'], str):
                raise porter.PorterError('Only visible text messages are supported')
            user = message['role'] == 'user'
            typ = 14 if user else 15
            reference = blob(1, trajectory_id) + (num(2, idx) if idx else b'') + blob(4, conversation_id)
            metadata = blob(1, timestamp(seconds + idx)) + num(3, 4 if user else 2) + blob(20, reference)
            if user:
                body = blob(2, message['text']) + blob(3, blob(1, message['text']))
            else:
                body = blob(1, message['text']) + blob(6, 'msg_' + str(uuid.uuid4())) + blob(8, message['text'])
            step = num(1, typ) + num(4, 3) + blob(5, metadata) + blob(19 if user else 20, body)
            con.execute('INSERT INTO steps(idx,step_type,status,metadata,step_payload) VALUES(?,?,3,?,?)',
                        (idx, typ, metadata, step))
        meta = make_metadata(conversation_id, project_id, workspace_uri, seconds)
        con.execute('INSERT INTO trajectory_metadata_blob VALUES(?,?)', ('main', meta))
        con.commit()
    finally:
        con.close()
    return meta


def decode_messages(path):
    con = sqlite3.connect(sqlite_uri(path.resolve(), 'ro'), uri=True)
    try:
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise porter.PorterError('Candidate database failed integrity check')
        result = []
        for idx, typ, status, data in con.execute('SELECT idx,step_type,status,step_payload FROM steps ORDER BY idx'):
            if idx != len(result) or typ not in (14, 15) or status != 3:
                raise porter.PorterError('Unexpected native step')
            if one(data, 1, 0) != typ or one(data, 4, 0) != status:
                raise porter.PorterError('Row/payload mismatch')
            body = one(data, 19 if typ == 14 else 20, 2)
            result.append({'role': 'user' if typ == 14 else 'assistant',
                           'text': one(body, 2 if typ == 14 else 1, 2).decode('utf-8')})
        return result
    finally:
        con.close()


def make_summary(messages, conversation_id, trajectory_id, project_id, workspace_uri, seconds, title):
    last_user = max(i for i, message in enumerate(messages) if message['role'] == 'user')
    meta = make_metadata(conversation_id, project_id, workspace_uri, seconds)
    raw = (blob(1, title) + num(2, len(messages)) + blob(3, timestamp(seconds + len(messages) - 1)) +
           blob(4, trajectory_id) + num(5, 1) + blob(7, timestamp(seconds)) + blob(9, blob(1, workspace_uri)) +
           blob(10, timestamp(seconds + last_user)) + blob(15, blob(1, title)) + num(16, last_user) +
           blob(17, meta) + num(22, 4) + num(28, 4))
    from datetime import datetime, timezone
    date = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
    row = {'conversation_id': conversation_id, 'title': title, 'preview': title, 'step_count': len(messages),
           'last_modified_time': date(seconds + len(messages) - 1), 'workspace_uris': json.dumps([workspace_uri]),
           'status': 'CASCADE_RUN_STATUS_IDLE', 'source': '', 'project_id': project_id,
           'last_user_input_time': date(seconds + last_user), 'last_user_input_step_index': last_user,
           'app_data_dir': 'antigravity'}
    return row, raw


def append_cache(existing, conversation_id, raw_summary):
    for field, wire, value, _ in fields(existing):
        if field == 1:
            if wire != 2:
                raise porter.PorterError('Unexpected summary cache shape')
            if one(value, 1, 2).decode() == conversation_id:
                raise porter.PorterError('Conversation already registered in cache')
    return existing + blob(1, blob(1, conversation_id) + blob(2, raw_summary))


def replace_cache_entry(existing, conversation_id, raw_summary):
    """Repair exactly one entry; preserve other entries/unknown bytes verbatim."""
    parts, matches = [], 0
    for field, wire, value, original in fields(existing):
        if field == 1 and wire == 2 and one(value, 1, 2).decode() == conversation_id:
            matches += 1
            parts.append(blob(1, blob(1, conversation_id) + blob(2, raw_summary)))
        else:
            parts.append(original)
    if matches != 1:
        raise porter.PorterError('Repair requires exactly one existing cache entry')
    return b''.join(parts)


def project_order(storage, project_id):
    """Preserve the observed double-encoded list and other UI settings."""
    current = storage.get('projectsOrder', '[]')
    decoded = json.loads(current) if isinstance(current, str) else current
    if not isinstance(decoded, list) or not all(isinstance(x, str) for x in decoded):
        raise porter.PorterError('Unsupported project order')
    if project_id in decoded:
        raise porter.PorterError('Project already registered')
    copied = dict(storage)
    updated = decoded + [project_id]
    copied['projectsOrder'] = json.dumps(updated) if isinstance(current, str) else updated
    return copied


