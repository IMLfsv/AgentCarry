"""Canonical display paths with extended Windows paths only at the I/O boundary.

No registry change or administrator permission is required. URIs and stored
source/target mappings retain ordinary paths for other applications.
"""
import os
import sys
from pathlib import Path as StandardPath
from urllib.parse import quote


def ordinary(value):
    value = os.fspath(value)
    if os.name == 'nt':
        if value.startswith('\\\\?\\UNC\\'):
            return '\\\\' + value[8:]
        if value.startswith('\\\\?\\'):
            return value[4:]
    return value


def extended(value, force=False):
    value = ordinary(value)
    if os.name == 'nt' and os.path.isabs(value) and (force or len(value.encode('utf-16-le')) // 2 >= 240):
        value = os.path.abspath(value)
        return '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return value


def sqlite_uri(path, mode='ro'):
    if mode not in ('ro', 'rw'):
        raise ValueError('Only existing read-only/read-write database URIs are allowed')
    # SQLite's URI parser decodes the extended filename before Windows I/O.
    return 'file:' + quote(extended(str(Path(path).absolute()), force=True), safe='/:') + '?mode=' + mode


class Path(type(StandardPath())):
    def __new__(cls, *args):
        return super().__new__(cls, *(ordinary(arg) for arg in args))

    def __init__(self, *args):
        if sys.version_info >= (3, 12):
            super().__init__(*(ordinary(arg) for arg in args))

    def __fspath__(self):
        return extended(str(self))

    def absolute(self):
        return type(self)(os.path.abspath(str(self)))
