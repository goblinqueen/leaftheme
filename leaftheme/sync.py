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
from collections import namedtuple

ReplayResult = namedtuple('ReplayResult', 'applied skipped removed_uids')
Skipped = namedtuple('Skipped', 'op reason drive_value')

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
