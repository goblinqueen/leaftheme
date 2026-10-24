"""Session-directory storage helpers.

* atomic_write — write a file via a temp file + rename, so a crash mid-write never leaves a
  half-written dictionary behind.
* The change log (ops.json): every local change to the dictionary since the last time the
  session matched Drive, as {uid, field, from, to, at} records. See leaftheme/sync.py for how
  the log is replayed on top of a fresh copy from Drive.
* The sync state (state.json): Drive file ids, the Drive version (md5Checksum) each local file
  was last synced with ("base"), and whether each file has local changes not yet on Drive.
"""
import json
import os
import tempfile

OPS_FILE_NAME = 'ops.json'
STATE_FILE_NAME = 'state.json'
EMPTY_LEAFTHEME = {"version": 1, "forward_srs": {}, "reverse_srs": {}}
DEFAULT_STATE = {
    'wt_file_id': None,     # the .wt on Drive
    'lt_file_id': None,     # leaftheme.json on Drive (None until first created)
    'folder_id': None,      # the WordTheme folder both live in
    'wt_base': None,        # md5Checksum of the .wt the local dictionary was last synced with
    'lt_base': None,        # md5Checksum of leaftheme.json last synced with
    'wt_dirty': False,      # local dictionary has changes not on Drive
    'lt_dirty': False,      # local leaftheme.json has changes not on Drive
}


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


def load_state(session_dir):
    state = dict(DEFAULT_STATE)
    path = os.path.join(session_dir, STATE_FILE_NAME)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            state.update(json.load(f))
    return state


def save_state(session_dir, state):
    atomic_write(os.path.join(session_dir, STATE_FILE_NAME), json.dumps(state))


def update_state(session_dir, **changes):
    state = load_state(session_dir)
    state.update(changes)
    save_state(session_dir, state)
    return state


def has_local_changes(session_dir):
    """True if anything in the session hasn't reached Drive yet."""
    state = load_state(session_dir)
    return state['wt_dirty'] or state['lt_dirty'] or bool(load_ops(session_dir))
