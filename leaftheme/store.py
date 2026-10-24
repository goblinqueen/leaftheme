"""Session-directory storage helpers.

* atomic_write — write a file via a temp file + rename, so a crash mid-write never leaves a
  half-written dictionary behind.
* The change log (ops.json): every local change to the dictionary since the last time the
  session matched Drive, as {uid, field, from, to, at} records. See leaftheme/sync.py for how
  the log is replayed on top of a fresh copy from Drive.
"""
import json
import os
import tempfile

OPS_FILE_NAME = 'ops.json'


def atomic_write(path, data):
    """Write bytes or str to path atomically."""
    if isinstance(data, str):
        data = data.encode('utf-8')
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def load_ops(session_dir):
    path = os.path.join(session_dir, OPS_FILE_NAME)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def append_ops(session_dir, ops):
    if not ops:
        return
    atomic_write(os.path.join(session_dir, OPS_FILE_NAME),
                 json.dumps(load_ops(session_dir) + list(ops), ensure_ascii=False))


def clear_ops(session_dir):
    path = os.path.join(session_dir, OPS_FILE_NAME)
    if os.path.exists(path):
        os.remove(path)
