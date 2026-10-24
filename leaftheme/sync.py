"""Replay the local change log on top of a fresh copy of the dictionary from Drive.

Each change is {uid, field, from, to}. For every change, compare what Drive has now (G):

* G == to   — already there (e.g. a retried sync); counts as applied, nothing to do.
* G == from — Drive hasn't touched it; apply the change.
* otherwise — someone else changed it (usually the phone); skip it and report it.

Changes are applied in order, so a skipped change makes later changes to the same field
skip too (their `from` is the skipped change's `to`, which never reached G).

Extra rules: Finnish spellings stay unique (an add or spelling edit that would clash with
another word on Drive is skipped), and a change pointing at a theme that no longer exists is
skipped. Applied changes go through the Dictionary mutators, so modified dates are bumped and
the Android app sees them as newer than its own copy.
"""
import json
import os
from collections import namedtuple

from . import drive, store
from .dictionary import Dictionary, _now_iso

ReplayResult = namedtuple('ReplayResult', 'applied skipped removed_uids')
Skipped = namedtuple('Skipped', 'op reason drive_value')
# wt / lt: what happened to the dictionary / leaftheme.json —
# 'unchanged', 'pulled' (Drive → local), 'pushed' (local → Drive) or 'merged' (replayed, then pushed)
SyncResult = namedtuple('SyncResult', 'wt lt skipped')

MAX_ATTEMPTS = 3


def sync(d, session_dir):
    """Bring the session dir and Drive in line. d is a drive.Drive.

    Dictionary (.wt):
      * Drive unchanged since the last sync, local changes → push the local copy.
      * Drive changed, no local changes (or no local copy) → pull.
      * Both changed → download, replay the change log on top, push the result.
    leaftheme.json (reviews): local wins if it has unsynced reviews, otherwise take Drive's.

    State is saved after each file, so a failure halfway keeps what already succeeded.
    Raises drive.NeedsLogin / drive.DriveFailure.
    """
    os.makedirs(session_dir, exist_ok=True)
    state = store.load_state(session_dir)
    location = d.locate(state['wt_file_id'])
    state.update(wt_file_id=location.wt_id, folder_id=location.folder_id)

    wt_action, skipped = _sync_dictionary(d, session_dir, state, location)
    store.save_state(session_dir, state)
    lt_action = _sync_leaftheme(d, session_dir, state, location)
    store.save_state(session_dir, state)
    return SyncResult(wt_action, lt_action, skipped)


def _sync_dictionary(d, session_dir, state, location):
    dict_path = os.path.join(session_dir, drive.DICTIONARY_FILE_NAME)
    ops = store.load_ops(session_dir)
    has_local = os.path.exists(dict_path)
    local_changes = bool(ops) or state['wt_dirty']

    for _ in range(MAX_ATTEMPTS):
        remote = d.metadata(location.wt_id).get('md5Checksum')
        if has_local and remote == state['wt_base']:
            if not local_changes:
                return 'unchanged', []
            with open(dict_path, encoding='utf-8') as f:
                data = json.load(f)
            removed = _read(os.path.join(session_dir, drive.REMOVED_FILE_NAME))
            action, skipped = 'pushed', []
        else:
            g = json.loads(drive.read_wt(d.download(location.wt_id)))
            if not has_local or not local_changes:
                _write_dictionary(session_dir, g)
                state.update(wt_base=remote, wt_dirty=False)
                _reset_local_log(session_dir)
                return 'pulled', []
            g_dict = Dictionary(g)
            result = replay(g_dict, ops)
            skipped = list(result.skipped)
            if state['wt_dirty'] and not ops:
                skipped.append(Skipped({'field': 'unlogged'},
                                       'Local changes made outside the app could not be merged', None))
            data = g_dict.to_dict()
            removed = ''.join(f'1;{uid};{_now_iso()}\n' for uid in result.removed_uids).encode()
            action = 'merged'
            # Drive must not have moved again while we were merging
            if d.metadata(location.wt_id).get('md5Checksum') != remote:
                continue

        dict_bytes = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        wt_bytes = drive.build_wt(dict_bytes, removed)
        meta = d.update(location.wt_id, wt_bytes, 'application/zip')
        _write_dictionary(session_dir, data)
        store.atomic_write(os.path.join(session_dir, 'dictionary.wt'), wt_bytes)  # for sideloading/debugging
        state.update(wt_base=meta.get('md5Checksum'), wt_dirty=False)
        _reset_local_log(session_dir)
        return action, skipped

    raise drive.DriveFailure('The dictionary on Google Drive kept changing during sync. Try again.')


def _sync_leaftheme(d, session_dir, state, location):
    lt_path = os.path.join(session_dir, drive.LEAFTHEME_FILE_NAME)
    has_local = os.path.exists(lt_path)

    if state['lt_dirty'] and has_local:
        data = _read(lt_path)
        if location.lt_id:
            meta = d.update(location.lt_id, data, 'application/json')
        else:
            meta = d.create(location.folder_id, drive.LEAFTHEME_FILE_NAME, data, 'application/json')
        state.update(lt_file_id=meta['id'], lt_base=meta.get('md5Checksum'), lt_dirty=False)
        return 'pushed'

    state.update(lt_file_id=location.lt_id, lt_dirty=False)
    if location.lt_id is None:
        if not has_local:
            store.atomic_write(lt_path, json.dumps(store.EMPTY_LEAFTHEME))
        return 'unchanged'
    remote = d.metadata(location.lt_id).get('md5Checksum')
    if has_local and remote == state['lt_base']:
        return 'unchanged'
    store.atomic_write(lt_path, d.download(location.lt_id))
    state.update(lt_base=remote)
    return 'pulled'


def _write_dictionary(session_dir, data):
    store.atomic_write(os.path.join(session_dir, drive.DICTIONARY_FILE_NAME),
                       json.dumps(data, ensure_ascii=False, separators=(',', ':')))


def _reset_local_log(session_dir):
    """Local dictionary now matches Drive: nothing left to replay or to delete on the phone."""
    store.clear_ops(session_dir)
    removed_path = os.path.join(session_dir, drive.REMOVED_FILE_NAME)
    if os.path.exists(removed_path):
        os.remove(removed_path)


def _read(path):
    if not os.path.exists(path):
        return b''
    with open(path, 'rb') as f:
        return f.read()

_APPLIED = 'applied'
_ALREADY = 'already'


def replay(d, ops):
    """Apply ops to Dictionary d (in place).

    Returns ReplayResult: applied — number of changes applied or already present,
    skipped — list of Skipped, removed_uids — uids of words deleted by the replay
    (they belong in removed.txt).
    """
    applied, skipped, removed_uids = 0, [], []
    for op in ops:
        outcome = _apply(d, op, removed_uids)
        if isinstance(outcome, Skipped):
            skipped.append(outcome)
        else:
            applied += 1
    d.ops.clear()  # the replay's own mutations are not new local changes
    return ReplayResult(applied, skipped, removed_uids)


def _apply(d, op, removed_uids):
    uid, field, old, new = op['uid'], op['field'], op['from'], op['to']
    word = d.word_by_uid(uid)

    if field == 'word':
        current = d.snapshot(word)
        if current == new:
            return _ALREADY
        if current != old:
            reason = 'Word already exists on Drive' if old is None else 'Word was changed on Drive'
            return Skipped(op, reason, current)
        if new is None:
            d.remove_word(word.id)
            removed_uids.append(uid)
            return _APPLIED
        theme = d.theme_by_uid(new['theme'])
        if theme is None:
            return Skipped(op, 'Theme no longer exists on Drive', None)
        clash = d.find_spelling(new['m'])
        if clash is not None:
            return Skipped(op, 'Spelling already exists on Drive', d.snapshot(clash))
        d.add_word(theme.id, new['m'], new['t'], uid=uid)
        return _APPLIED

    if word is None:
        return Skipped(op, 'Word no longer exists on Drive', None)

    if field == 'm':
        current = word.word
    elif field == 't':
        current = word.translation
    elif field == 'theme':
        current = d.snapshot(word)['theme']
    else:
        return Skipped(op, f'Unknown change type {field!r}', None)

    if current == new:
        return _ALREADY
    if current != old:
        return Skipped(op, 'Changed on Drive', current)

    if field == 'm':
        clash = d.find_spelling(new, exclude_uid=uid)
        if clash is not None:
            return Skipped(op, 'Spelling already exists on Drive', d.snapshot(clash))
        d.edit_word(word.id, word_str=new)
    elif field == 't':
        d.edit_word(word.id, translation=new)
    else:
        theme = d.theme_by_uid(new)
        if theme is None:
            return Skipped(op, 'Theme no longer exists on Drive', None)
        d.move_word(word.id, theme.id)
    return _APPLIED
