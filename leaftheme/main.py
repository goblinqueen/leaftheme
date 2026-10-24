import html as _html
import json
import os
import random
from datetime import timedelta
from urllib.parse import quote_plus

import flask

import google_auth_oauthlib.flow
from . import dictionary
from . import drive
from . import store
from . import sync as sync_engine
import re
from glosbe import GlosbeTranslator

_glosbe_fi_ru = GlosbeTranslator(lang_from='fi', lang_to='ru', delay=0)
_glosbe_ru_fi = GlosbeTranslator(lang_from='ru', lang_to='fi', delay=0)
_glosbe = _glosbe_fi_ru  # default, used by word_glosbe


def _pick_glosbe(word):
    """Return (translator, lang_from, lang_to) based on detected script."""
    if re.search(r'[А-Яа-яёЁ]', word):
        return _glosbe_ru_fi, 'ru', 'fi'
    return _glosbe_fi_ru, 'fi', 'ru'

PROJECT_ID = "goblin-queendom"

DICTIONARY_FILE_NAME = drive.DICTIONARY_FILE_NAME
LEAFTHEME_FILE_NAME = drive.LEAFTHEME_FILE_NAME
REMOVED_FILE_NAME = drive.REMOVED_FILE_NAME
EMPTY_LEAFTHEME = store.EMPTY_LEAFTHEME

SCOPES = ['https://www.googleapis.com/auth/drive.metadata.readonly',
          'https://www.googleapis.com/auth/drive.file',
          'https://www.googleapis.com/auth/drive',
          "https://www.googleapis.com/auth/drive.metadata"
          ]

os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

app = flask.Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']
app.permanent_session_lifetime = timedelta(days=60)


@app.template_filter('fmtdate')
def fmtdate(iso):
    """Format an ISO timestamp string to 'DD Mon YYYY'."""
    if not iso:
        return ''
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return f"{dt.day} {dt.strftime('%b %Y')}"
    except Exception:
        return iso


@app.route('/')
def index():
    return flask.render_template('index.html', menu_items=get_menu_items())


LAST_SYNC_FILE_NAME = 'last_sync.json'


@app.route('/sync', methods=['GET', 'POST'])
def sync_with_drive():
    """The one place local data and Google Drive are brought in line (see leaftheme/sync.py).

    Only syncs when asked to: a POST (the Sync button), a sync interrupted by a login, or when
    there is no local copy at all (nothing to lose, and nothing to show without one). A plain
    visit otherwise just offers the button, so redirects/back/refresh never trigger a sync.
    ?next=/path — go there afterwards if nothing needs the user's attention."""
    next_url = _safe_next(flask.request.values.get('next'))
    retry_url = flask.url_for('sync_with_drive', next=next_url) if next_url else flask.url_for('sync_with_drive')
    if 'credentials' not in flask.session or 'file_name' not in flask.session:
        flask.session['pending_sync'] = True
        return _login(retry_url)

    has_local_copy = os.path.exists(os.path.join(flask.session['file_name'], DICTIONARY_FILE_NAME))
    asked = flask.request.method == 'POST' or flask.session.pop('pending_sync', False)
    if has_local_copy and not asked:
        return flask.render_template('sync_confirm.html', menu_items=get_menu_items(),
                                     next_url=next_url, unsynced=_has_unsaved())
    try:
        result = _run_sync()
    except drive.NeedsLogin:
        flask.session['pending_sync'] = True
        return _login(retry_url)
    except drive.DriveFailure as e:
        return _drive_error(e)

    if next_url and not result.skipped:
        return flask.redirect(next_url)
    wt_dict, _ = _load_dictionary()
    summary = {'wt': result.wt, 'lt': result.lt,
               'skipped_rows': _skipped_rows(result.skipped, wt_dict)}
    store.atomic_write(os.path.join(flask.session['file_name'], LAST_SYNC_FILE_NAME),
                       json.dumps(summary, ensure_ascii=False))
    return flask.redirect(flask.url_for('sync_result', next=next_url) if next_url
                          else flask.url_for('sync_result'))


@app.route('/sync/result')
def sync_result():
    """Shows what the last sync did. Visiting it again never syncs."""
    session_dir = flask.session.get('file_name')
    path = os.path.join(session_dir, LAST_SYNC_FILE_NAME) if session_dir else None
    if not path or not os.path.exists(path):
        return flask.redirect(flask.url_for('sync_with_drive'))
    with open(path, encoding='utf-8') as f:
        summary = json.load(f)
    wt_dict, _ = _load_dictionary()
    return flask.render_template('sync_result.html', menu_items=get_menu_items(),
                                 summary=summary,
                                 next_url=_safe_next(flask.request.args.get('next')),
                                 title=wt_dict.title if wt_dict else '',
                                 theme_count=len(wt_dict.themes) if wt_dict else 0,
                                 word_count=len(wt_dict.words) if wt_dict else 0)


def _skipped_rows(skipped, wt_dict):
    """Human-readable rows for the changes a sync couldn't apply."""
    def theme_name(uid):
        theme = wt_dict.theme_by_uid(uid) if wt_dict else None
        return theme.name if theme else '(deleted theme)'

    def describe(field, value):
        if value is None:
            return '—'
        if field == 'theme':
            return theme_name(value)
        if field == 'word':
            return f"{value['m']} – {value['t']} ({theme_name(value['theme'])})"
        return value

    rows = []
    for s in skipped:
        field = s.op.get('field')
        if field == 'unlogged':
            rows.append({'word': '—', 'word_id': None, 'change': '—', 'drive': '—', 'reason': s.reason})
            continue
        current = wt_dict.word_by_uid(s.op['uid']) if wt_dict else None
        if current is not None:
            name = current.word
        else:
            snap = s.op['from'] if field == 'word' and s.op['from'] else s.op['to']
            name = snap['m'] if field == 'word' and snap else (s.op['from'] or '?')
        if field == 'word':
            change = 'add ' + describe(field, s.op['to']) if s.op['from'] is None else 'delete'
        else:
            label = {'m': 'Finnish', 't': 'Russian', 'theme': 'Theme'}.get(field, field)
            change = f"{label}: {describe(field, s.op['from'])} → {describe(field, s.op['to'])}"
        rows.append({'word': name, 'word_id': current.id if current is not None else None,
                     'change': change, 'drive': describe(field, s.drive_value), 'reason': s.reason})
    return rows


@app.route('/load_dictionary')
@app.route('/save_dictionary')
def legacy_load_save():
    """Old Update Dictionary / Save to Drive links: both are a sync now."""
    return flask.redirect(flask.url_for('sync_with_drive'))


@app.route('/themes')
def get_themes():
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME

    if not os.path.exists(file_name):
        return _missing_dictionary()

    with open(file_name, encoding="utf8") as f:
        wt_dict = dictionary.Dictionary(json.load(f))
    return flask.render_template('themes.html', menu_items=get_menu_items(),
                                 themes=wt_dict.themes.values())


@app.route('/words/<theme_id>')
def get_words(theme_id):
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME

    if not os.path.exists(file_name):
        return _missing_dictionary()

    with open(file_name, encoding="utf8") as f:
        wt_dict = dictionary.Dictionary(json.load(f))

    theme = wt_dict.themes[int(theme_id)]
    out = []
    for i, word in enumerate(sorted(theme.words.values())):
        if i > 30:
            break
        out.append(word)
    return flask.render_template('words.html',
                                 menu_items=get_menu_items(), words=out, theme=theme)


@app.route('/add_word', methods=['GET', 'POST'])
def add_word():
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME

    if not os.path.exists(file_name):
        return _missing_dictionary()

    with open(file_name, encoding="utf8") as f:
        wt_dict = dictionary.Dictionary(json.load(f))

    if flask.request.method == 'POST':
        theme_id = int(flask.request.form['theme_id'])
        word_str = flask.request.form['word'].strip()
        translation = flask.request.form['translation'].strip()
        redirect_to = flask.request.form.get('redirect_to', '')

        existing = wt_dict.find_spelling(word_str)
        if existing:
            existing_theme = wt_dict.themes.get(existing.theme)
            error = f'"{existing.word}" already exists' + (
                f' in {existing_theme.name}' if existing_theme else '')
            return flask.render_template('add_word.html',
                                         menu_items=get_menu_items(),
                                         themes=wt_dict.themes.values(),
                                         preselected=theme_id,
                                         prefill_word=word_str,
                                         prefill_translation=translation,
                                         redirect_to=redirect_to,
                                         error=error)

        wt_dict.add_word(theme_id, word_str, translation)
        _save_dictionary(wt_dict, file_name)
        if redirect_to:
            return flask.redirect(redirect_to)
        return flask.redirect(flask.url_for('get_words', theme_id=theme_id))

    preselected = flask.request.args.get('theme_id', type=int)
    prefill_word = flask.request.args.get('word', '')
    redirect_to = flask.request.args.get('redirect_to', '')
    return flask.render_template('add_word.html',
                                 menu_items=get_menu_items(),
                                 themes=wt_dict.themes.values(),
                                 preselected=preselected,
                                 prefill_word=prefill_word,
                                 redirect_to=redirect_to)


@app.route('/word/<int:word_id>', methods=['GET', 'POST'])
def get_word(word_id):
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return _missing_dictionary()

    word = wt_dict.words.get(word_id)
    if word is None:
        flask.abort(404)

    if flask.request.method == 'POST':
        new_word = flask.request.form.get('word', '').strip()
        new_translation = flask.request.form.get('translation', '').strip()
        new_theme_id = flask.request.form.get('theme_id', type=int)
        redirect_to = flask.request.form.get('redirect_to', '').strip()
        clash = wt_dict.find_spelling(new_word, exclude_uid=word.uid) if new_word else None
        if clash is not None:
            clash_theme = wt_dict.themes.get(clash.theme)
            error = f'"{clash.word}" already exists' + (
                f' in {clash_theme.name}' if clash_theme else '') + ' — nothing was saved.'
            return _render_word(wt_dict, word, redirect_to, error=error)
        wt_dict.edit_word(word_id, new_word, new_translation)
        if new_theme_id is not None and new_theme_id != word.theme:
            wt_dict.move_word(word_id, new_theme_id)
        _save_dictionary(wt_dict, file_name)
        return flask.redirect(redirect_to or flask.url_for('get_word', word_id=word_id))

    redirect_to = flask.request.args.get('redirect_to', '').strip() or flask.request.referrer or ''
    return _render_word(wt_dict, word, redirect_to)


def _render_word(wt_dict, word, redirect_to, error=None):
    theme = wt_dict.themes[word.theme] if word.theme is not None else None
    lt_data = _load_leaftheme()
    reviews = [(label, _srs_view(lt_data.get(key, {}).get(word.uid)))
               for label, key in (('Fi → Ru', 'forward_srs'), ('Ru → Fi', 'reverse_srs'))]
    return flask.render_template('word.html',
                                 menu_items=get_menu_items(), word=word, theme=theme,
                                 all_themes=wt_dict.themes.values(),
                                 in_review=theme is not None and 'heaven' in theme.name.lower(),
                                 reviews=reviews,
                                 redirect_to=redirect_to, error=error)


def _srs_view(entry):
    """Display values for one leaftheme.json SRS entry (None if the word was never reviewed)."""
    if not entry or entry.get('dr') is None:
        return None
    from datetime import datetime, timezone
    log = entry.get('log') or []
    tm = entry.get('tm') or 0
    return {
        'ease': (tm / 100) if tm >= 130 else 2.5,
        'reviews': len(log) if log else entry.get('ca'),
        'interval': entry.get('di') or 0,
        'next': entry['dr'],
        'due': datetime.now(timezone.utc) >= dictionary._parse_iso(entry['dr']),
        'last': log[-1]['dt'] if log else None,
        'fails': entry.get('cf', 0),
    }


@app.route('/word/<int:word_id>/glosbe')
def word_glosbe(word_id):
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return flask.jsonify(error='no dictionary'), 400
    word = wt_dict.words.get(word_id)
    if word is None:
        return flask.jsonify(error='not found'), 404
    translation = _glosbe.translate(word.word)
    return flask.jsonify(translation=translation)


@app.route('/glosbe')
def glosbe_lookup():
    word = flask.request.args.get('word', '').strip()
    if not word:
        return flask.jsonify(error='no word'), 400
    direction = flask.request.args.get('dir', '')
    if direction == 'fi-ru':
        translator, lang_from, lang_to = _glosbe_fi_ru, 'fi', 'ru'
    elif direction == 'ru-fi':
        translator, lang_from, lang_to = _glosbe_ru_fi, 'ru', 'fi'
    else:
        translator, lang_from, lang_to = _pick_glosbe(word)
    translation = translator.translate(word)
    return flask.jsonify(translation=translation, lang_from=lang_from, lang_to=lang_to)


@app.route('/word/<int:word_id>/delete', methods=['POST'])
def delete_word(word_id):
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return _missing_dictionary()
    word = wt_dict.words.get(word_id)
    if word is None:
        flask.abort(404)
    theme_id = word.theme
    redirect_to = flask.request.form.get('redirect_to', '').strip()
    _append_removed(word)
    wt_dict.remove_word(word_id)
    _save_dictionary(wt_dict, file_name)
    if redirect_to:
        return flask.redirect(redirect_to)
    if theme_id is not None:
        return flask.redirect(flask.url_for('get_words', theme_id=theme_id))
    return flask.redirect(flask.url_for('get_themes'))


@app.route('/search')
def search():
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    if not os.path.exists(file_name):
        return _missing_dictionary()

    query = ""
    results = []

    if 'query' in flask.request.args:
        query = flask.request.args.get('query')

        with open(file_name, encoding="utf8") as f:
            wt_dict = dictionary.Dictionary(json.load(f))

        results = wt_dict.search(query)

    return flask.render_template('search.html',
                                 menu_items=get_menu_items(), query=query, results=results)


def _load_words_json(word_ids_str):
    """Load dictionary and return (words_json_str, error_response_or_None)."""
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    if not os.path.exists(file_name):
        return None, _missing_dictionary()
    with open(file_name, encoding="utf8") as f:
        wt_dict = dictionary.Dictionary(json.load(f))
    ids = [int(x) for x in word_ids_str.split(',') if x.strip()]
    words_list = []
    for wid in ids:
        if wid in wt_dict.words:
            w = wt_dict.words[wid]
            words_list.append({'id': w.id, 'word': w.word, 'translation': w.translation})
    if not words_list:
        return None, ('No valid word IDs', 400)
    return json.dumps(words_list, ensure_ascii=False), None


@app.route('/test/flashcard')
def test_flashcard():
    word_ids_str = flask.request.args.get('word_ids', '')
    return_url = _safe_next(flask.request.args.get('return_url')) or '/themes'
    words_json, err = _load_words_json(word_ids_str)
    if err:
        return err
    return flask.render_template('test_flashcard.html',
                                 menu_items=get_menu_items(),
                                 words_json=words_json, return_url=return_url)


# --- Review (Spaced Repetition) flow ---

REVIEW_BATCH_SIZE = 20


def _heaven_themes(wt_dict):
    """Return list of themes whose name contains 'heaven' (case-insensitive)."""
    return [t for t in wt_dict.themes.values() if 'heaven' in t.name.lower()]


@app.route('/review')
def review_pick():
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return _missing_dictionary()

    lt_data = _load_leaftheme()

    # Only Heaven themes participate in SRS review
    theme_counts = []
    total_fwd = 0
    total_rev = 0
    for t in _heaven_themes(wt_dict):
        fwd = len(_srs_due_words(wt_dict, lt_data, 'forward_srs', t.id))
        rev = len(_srs_due_words(wt_dict, lt_data, 'reverse_srs', t.id))
        theme_counts.append((t, fwd, rev))
        total_fwd += fwd
        total_rev += rev

    return flask.render_template('review_pick.html',
                                 menu_items=get_menu_items(),
                                 theme_counts=theme_counts,
                                 total_fwd=total_fwd,
                                 total_rev=total_rev)


@app.route('/review/start')
def review_start():
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return _missing_dictionary()

    theme_id = flask.request.args.get('theme_id', type=int)
    direction = flask.request.args.get('direction', 'forward')
    srs_key = 'reverse_srs' if direction == 'reverse' else 'forward_srs'

    lt_data = _load_leaftheme()

    # SRS only applies to Heaven themes
    if theme_id is not None:
        due = _srs_due_words(wt_dict, lt_data, srs_key, theme_id)
    else:
        due = []
        for t in _heaven_themes(wt_dict):
            due.extend(_srs_due_words(wt_dict, lt_data, srs_key, t.id))

    if not due:
        return flask.render_template('review_done.html',
                                     menu_items=get_menu_items(),
                                     reviewed=0, theme_id=theme_id)

    batch = random.sample(due, min(REVIEW_BATCH_SIZE, len(due)))
    if direction == 'reverse':
        words_list = [{'id': w.uid, 'word_id': w.id,
                       'word': w.translation, 'translation': w.word} for w in batch]
    else:
        words_list = [{'id': w.uid, 'word_id': w.id,
                       'word': w.word, 'translation': w.translation} for w in batch]
    words_json = json.dumps(words_list, ensure_ascii=False)

    return flask.render_template('review_session.html',
                                 menu_items=get_menu_items(),
                                 words_json=words_json,
                                 theme_id=theme_id,
                                 total_due=len(due),
                                 direction=direction)


@app.route('/review/apply', methods=['POST'])
def review_apply():
    """Apply SM-2 ratings, save both files to Drive, redirect to confirmation page."""
    data = flask.request.get_json()
    results = data.get('results', {})  # {word_uid: quality}
    theme_id = data.get('theme_id')
    direction = data.get('direction', 'forward')
    srs_key = 'reverse_srs' if direction == 'reverse' else 'forward_srs'

    lt_data = _load_leaftheme()
    for uid, quality in results.items():
        _apply_srs(lt_data, srs_key, uid, quality)
    _save_leaftheme(lt_data)

    # Sync right away; the ratings are safe locally either way, so a failure is reported, not fatal
    redirect_args = {'direction': direction, 'reviewed': len(results)}
    try:
        _run_sync()
    except drive.NeedsLogin:
        redirect_args['sync_error'] = 'Google login expired.'
    except drive.DriveFailure as e:
        redirect_args['sync_error'] = str(e)

    if theme_id is not None:
        redirect_args['theme_id'] = theme_id
    return flask.jsonify(status='ok', reviewed=len(results),
                         redirect=flask.url_for('review_saved', **redirect_args))


@app.route('/review/saved')
def review_saved():
    """Shown after each review batch is saved to Drive."""
    direction = flask.request.args.get('direction', 'forward')
    theme_id = flask.request.args.get('theme_id', type=int)
    reviewed = flask.request.args.get('reviewed', type=int, default=0)
    sync_error = flask.request.args.get('sync_error')

    # Count remaining due words so the button shows the number
    wt_dict, _ = _load_dictionary()
    remaining = 0
    if wt_dict is not None:
        lt_data = _load_leaftheme()
        srs_key = 'reverse_srs' if direction == 'reverse' else 'forward_srs'
        heaven_uids = {w.uid for t in _heaven_themes(wt_dict) for w in t.words.values()}
        due = [w for w in _srs_due_words(wt_dict, lt_data, srs_key, theme_id)
               if w.uid in heaven_uids]
        remaining = len(due)

    continue_args = {'direction': direction}
    if theme_id is not None:
        continue_args['theme_id'] = theme_id

    return flask.render_template('review_saved.html',
                                 menu_items=get_menu_items(),
                                 reviewed=reviewed,
                                 sync_error=sync_error,
                                 remaining=remaining,
                                 direction=direction,
                                 continue_url=flask.url_for('review_start', **continue_args))


def _drive():
    """Drive client for the logged-in user. Raises drive.NeedsLogin without credentials."""
    if 'credentials' not in flask.session:
        raise drive.NeedsLogin()
    return drive.Drive(flask.session['credentials'])


def _keep_credentials(d):
    """Store the (possibly refreshed) token back into the session."""
    flask.session['credentials'] = d.session_credentials()


def _login(next_url):
    """Send the user through OAuth, then back to next_url. Local session files are kept."""
    flask.session['oauth_next'] = next_url
    return flask.redirect(flask.url_for('authorize'))


def _drive_error(error):
    return flask.render_template('drive_error.html', menu_items=get_menu_items(),
                                 message=str(error)), 502


def _run_sync():
    """Sync the session dir with Drive. Raises drive.NeedsLogin / drive.DriveFailure."""
    session_dir = flask.session['file_name']
    if not os.path.exists(os.path.join(session_dir, store.STATE_FILE_NAME)):
        # Sessions from before state.json kept the Drive file id in the cookie
        os.makedirs(session_dir, exist_ok=True)
        store.update_state(session_dir, wt_file_id=flask.session.get('dict_file_id'))
    d = _drive()
    result = sync_engine.sync(d, session_dir)
    _keep_credentials(d)
    return result


def _safe_next(url):
    """Only allow local paths as a post-sync destination."""
    if url and url.startswith('/') and not url.startswith('//'):
        return url
    return None


def _missing_dictionary():
    """The server has no local copy (new session, or Heroku restarted and wiped it).

    Renders a page whose script restores the browser's copy from the last sync, or runs a sync
    if the browser has none, then comes back to the current page."""
    next_url = flask.request.full_path.rstrip('?') if flask.request.method == 'GET' else '/themes'
    return flask.render_template('restore.html', menu_items=get_menu_items(), next_url=next_url)


def _load_dictionary():
    """Load dictionary from session file. Returns (Dictionary, file_path) or (None, path)."""
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    if not os.path.exists(file_name):
        return None, file_name
    with open(file_name, encoding="utf8") as f:
        return dictionary.Dictionary(json.load(f)), file_name


def _load_leaftheme():
    """Load leaftheme.json from session dir. Migrates forward SRS from .wt on first load."""
    session_dir = flask.session.get('file_name')
    if not session_dir:
        return dict(EMPTY_LEAFTHEME)
    lt_path = os.path.join(session_dir, LEAFTHEME_FILE_NAME)
    if not os.path.exists(lt_path):
        return dict(EMPTY_LEAFTHEME)
    with open(lt_path, encoding='utf-8') as f:
        data = json.load(f)
    # Ensure both keys exist (upgrade from older format)
    data.setdefault('forward_srs', {})
    data.setdefault('reverse_srs', {})
    return data


def _save_leaftheme(data):
    """Write leaftheme.json to session dir and flag it for the next sync."""
    session_dir = flask.session.get('file_name')
    if not session_dir:
        return
    store.atomic_write(os.path.join(session_dir, LEAFTHEME_FILE_NAME),
                       json.dumps(data, ensure_ascii=False))
    store.update_state(session_dir, lt_dirty=True)


def _srs_due_words(wt_dict, lt_data, srs_key, theme_id=None):
    """Return Word objects due for review in the given SRS direction."""
    from datetime import datetime, timezone
    srs = lt_data.get(srs_key, {})
    if theme_id is not None and theme_id in wt_dict.themes:
        words = wt_dict.themes[theme_id].words.values()
    else:
        words = wt_dict.words.values()
    due = []
    for w in words:
        entry = srs.get(w.uid)
        if entry is None or entry.get('dr') is None:
            due.append(w)
        else:
            dr = dictionary._parse_iso(entry['dr'])
            if datetime.now(timezone.utc) >= dr:
                due.append(w)
    return due


def _apply_srs(lt_data, srs_key, uid, quality):
    """Apply SM-2 to an SRS entry in the given direction, creating it if needed."""
    from leaftheme.dictionary import _now_iso
    srs = lt_data.setdefault(srs_key, {})
    entry = srs.get(uid, {})
    new_ease, new_reps, new_interval, new_dr = dictionary.Dictionary.Word.sm2_calculate(
        entry.get('tm', 0), entry.get('ca'), entry.get('di'), quality)
    cf = entry.get('cf', 0)
    cf = cf + 1 if quality == 0 else 0
    log = entry.get('log', [])
    log.append({'q': quality, 'dt': _now_iso()})
    srs[uid] = {'tm': new_ease, 'ca': new_reps, 'di': new_interval, 'dr': new_dr, 'cf': cf, 'log': log}


def _has_unsaved():
    session_dir = flask.session.get('file_name')
    return bool(session_dir) and os.path.isdir(session_dir) and store.has_local_changes(session_dir)


def _save_dictionary(wt_dict, file_name):
    """Write dictionary back to the session file and append its changes to the log for the next sync."""
    store.atomic_write(file_name, json.dumps(wt_dict.to_dict(), ensure_ascii=False,
                                             separators=(',', ':')))
    store.append_ops(os.path.dirname(file_name), wt_dict.ops)
    wt_dict.ops.clear()


def _removed_path():
    session_dir = flask.session.get('file_name')
    return os.path.join(session_dir, REMOVED_FILE_NAME) if session_dir else None


def _append_removed(word):
    """Append a deleted word's entry to the local removed.txt (sent with the next .wt upload)."""
    from leaftheme.dictionary import _now_iso
    path = _removed_path()
    if not path:
        return
    line = f'1;{word.uid};{_now_iso()}\n'
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line)


@app.route('/stats')
def stats():
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return _missing_dictionary()

    from datetime import datetime, timezone
    from collections import Counter

    lt_data = _load_leaftheme()

    fwd_srs = lt_data.get('forward_srs', {})
    rev_srs = lt_data.get('reverse_srs', {})

    all_words = list(wt_dict.words.values())
    total_words = len(all_words)
    total_themes = len(wt_dict.themes)

    # Heaven words only for SRS stats
    heaven_word_list = []
    for t in _heaven_themes(wt_dict):
        heaven_word_list.extend(t.words.values())
    heaven_uids = {w.uid for w in heaven_word_list}
    heaven_count = len(heaven_word_list)

    # Due / reviewed / never reviewed — forward (Heaven only)
    fwd_due = [w for w in _srs_due_words(wt_dict, lt_data, 'forward_srs')
               if w.uid in heaven_uids]
    fwd_reviewed_uids = {uid for uid, e in fwd_srs.items()
                         if e.get('dr') is not None and uid in heaven_uids}
    fwd_reviewed_count = len(fwd_reviewed_uids)
    fwd_never_reviewed = heaven_count - fwd_reviewed_count

    # Due / reviewed / never reviewed — reverse (Heaven only)
    rev_due = [w for w in _srs_due_words(wt_dict, lt_data, 'reverse_srs')
               if w.uid in heaven_uids]
    rev_reviewed_uids = {uid for uid, e in rev_srs.items()
                         if e.get('dr') is not None and uid in heaven_uids}
    rev_reviewed_count = len(rev_reviewed_uids)
    rev_never_reviewed = heaven_count - rev_reviewed_count

    def _srs_distributions(srs, reviewed_uids):
        ef_buckets = Counter()
        int_buckets = Counter()
        for uid in reviewed_uids:
            entry = srs[uid]
            tm = entry.get('tm', 0)
            ef = (tm / 100) if tm and tm >= 130 else 2.5
            if ef < 1.5:
                ef_buckets['< 1.5'] += 1
            elif ef < 2.0:
                ef_buckets['1.5 – 2.0'] += 1
            elif ef < 2.5:
                ef_buckets['2.0 – 2.5'] += 1
            elif ef < 3.0:
                ef_buckets['2.5 – 3.0'] += 1
            else:
                ef_buckets['>= 3.0'] += 1

            days = entry.get('di') or 0
            if days <= 1:
                int_buckets['1 day'] += 1
            elif days <= 7:
                int_buckets['2–7 days'] += 1
            elif days <= 30:
                int_buckets['8–30 days'] += 1
            elif days <= 90:
                int_buckets['1–3 months'] += 1
            else:
                int_buckets['3+ months'] += 1
        return ef_buckets, int_buckets

    ef_labels = ['< 1.5', '1.5 – 2.0', '2.0 – 2.5', '2.5 – 3.0', '>= 3.0']
    int_labels = ['1 day', '2–7 days', '8–30 days', '1–3 months', '3+ months']

    fwd_ef, fwd_int = _srs_distributions(fwd_srs, fwd_reviewed_uids)
    rev_ef, rev_int = _srs_distributions(rev_srs, rev_reviewed_uids)

    # Learning flow stages — detect by theme name keywords
    FLOW_STAGES = [
        ('Hell', 'bg-danger'),
        ('Purgatory', 'bg-warning text-dark'),
        ('Gates', 'bg-secondary'),
        ('Ring', 'bg-info text-dark'),
        ('Heaven', 'bg-success'),
    ]
    flow_stats = []
    for keyword, badge_class in FLOW_STAGES:
        matching = [t for t in wt_dict.themes.values()
                    if keyword.lower() in t.name.lower()]
        count = sum(t.word_count() for t in matching)
        theme_ids = [t.id for t in matching]
        flow_stats.append({'label': keyword, 'count': count,
                           'badge': badge_class, 'theme_ids': theme_ids})

    # Struggling words in Heaven (forward): cf >= 2 or tm <= 150
    heaven_themes = [t for t in wt_dict.themes.values()
                     if 'heaven' in t.name.lower()]
    struggling_words = []
    for t in heaven_themes:
        for w in t.words.values():
            entry = fwd_srs.get(w.uid, {})
            cf = entry.get('cf', 0)
            tm = entry.get('tm', 0)
            reviewed = entry.get('dr') is not None
            if reviewed and (cf >= 2 or (tm and 0 < tm <= 150)):
                struggling_words.append({
                    'id': w.id, 'word': w.word, 'translation': w.translation,
                    'cf': cf, 'tm': tm, 'theme': t.name,
                })
    struggling_words.sort(key=lambda x: (-x['cf'], x['tm']))

    # Theme breakdown
    theme_stats = []
    for t in sorted(wt_dict.themes.values(), key=lambda t: t.word_count(), reverse=True):
        count = t.word_count()
        fwd_theme_due = len(_srs_due_words(wt_dict, lt_data, 'forward_srs', t.id))
        rev_theme_due = len(_srs_due_words(wt_dict, lt_data, 'reverse_srs', t.id))
        theme_stats.append({'name': t.name, 'count': count,
                            'fwd_due': fwd_theme_due, 'rev_due': rev_theme_due})

    # Words added per month (from created_date)
    month_counts = Counter()
    for w in all_words:
        if w.created_date:
            month_counts[w.created_date[:7]] += 1  # YYYY-MM
    months_sorted = sorted(month_counts.keys())
    month_labels = months_sorted
    month_data = [month_counts[m] for m in months_sorted]

    return flask.render_template('stats.html',
                                 menu_items=get_menu_items(),
                                 total_words=total_words,
                                 total_themes=total_themes,
                                 fwd_due=len(fwd_due),
                                 fwd_reviewed=fwd_reviewed_count,
                                 fwd_never=fwd_never_reviewed,
                                 rev_due=len(rev_due),
                                 rev_reviewed=rev_reviewed_count,
                                 rev_never=rev_never_reviewed,
                                 flow_stats=flow_stats,
                                 struggling_words=struggling_words,
                                 ef_labels=json.dumps(ef_labels),
                                 fwd_ef_data=json.dumps([fwd_ef.get(l, 0) for l in ef_labels]),
                                 rev_ef_data=json.dumps([rev_ef.get(l, 0) for l in ef_labels]),
                                 int_labels=json.dumps(int_labels),
                                 fwd_int_data=json.dumps([fwd_int.get(l, 0) for l in int_labels]),
                                 rev_int_data=json.dumps([rev_int.get(l, 0) for l in int_labels]),
                                 theme_stats=theme_stats,
                                 month_labels=json.dumps(month_labels),
                                 month_data=json.dumps(month_data))


_CLASS_BADGE = {
    'nimisana':   'bg-info text-dark',
    'teonsana':   'bg-primary',
    'adjektiivi': 'bg-success',
    'adverbi':    'bg-warning text-dark',
    'asemosana':  'bg-secondary',
}


def _word_popup_html(token):
    """Build Bootstrap popover HTML content for a word token."""
    parts = []
    for e in token['entries']:
        base = _html.escape(e['base'] or '')
        trans = _html.escape(e['translation']) if e['translation'] else None
        word_class = e.get('word_class') or ''
        word_id = e.get('word_id')

        badge = _CLASS_BADGE.get(word_class, 'bg-secondary')

        line = f'<div class="wt-pe border rounded px-2 py-1 mb-1"><span class="fw-medium">{base}</span>'
        if trans:
            line += f' \u2013 {trans}'
        if word_class:
            line += f' <span class="badge {badge}">{_html.escape(word_class)}</span>'
        if word_id is not None:
            line += f' <a href="/word/{word_id}" class="text-secondary ms-1 text-decoration-none">&#x2197;</a>'
        line += '</div>'
        parts.append(line)

    if not any(e['translation'] for e in token['entries']):
        first_base = token['entries'][0]['base'] if token['entries'] else token['src']
        parts.append(f'<a href="/add_word?word={quote_plus(first_base)}" class="small">+ Add</a>')

    return ''.join(parts)


@app.route('/reading', methods=['GET', 'POST'])
def reading():
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return _missing_dictionary()

    import read as _read

    input_text = ''
    tokens = None

    if flask.request.method == 'POST':
        input_text = flask.request.form.get('text', '').strip()
        if input_text:
            tokens = _read.analyze_text(input_text, wt_dict)
            for tok in tokens:
                if tok['type'] == 'word':
                    tok['popup_html'] = _word_popup_html(tok)
                elif tok['type'] == 'word_unanalyzed':
                    tok['popup_html'] = f'<a href="/add_word?word={quote_plus(tok["src"])}" class="small">+ Add</a>'

    return flask.render_template('reading.html',
                                 menu_items=get_menu_items(),
                                 input_text=input_text,
                                 tokens=tokens)


def _sync_rev(session_dir):
    """Identifies the last synced Drive state; changes only when a sync moves something."""
    state = store.load_state(session_dir)
    if not state['wt_base']:
        return None
    return f"{state['wt_base']}|{state['lt_base']}"


@app.context_processor
def _inject_sync_rev():
    session_dir = flask.session.get('file_name')
    rev = None
    if session_dir and os.path.exists(os.path.join(session_dir, DICTIONARY_FILE_NAME)):
        rev = _sync_rev(session_dir)
    return {'sync_rev': rev}


@app.route('/api/snapshot')
def api_snapshot():
    """The session's files as of the last sync, for the browser's backup copy.

    Refused while there are local changes: the browser copy must always be a synced state,
    so restoring it can never push stale data (the next sync just pulls what's newer)."""
    session_dir = flask.session.get('file_name')
    if not session_dir or not os.path.exists(os.path.join(session_dir, DICTIONARY_FILE_NAME)):
        return flask.jsonify({'error': 'not loaded'}), 404
    if store.has_local_changes(session_dir):
        return flask.jsonify({'error': 'local changes not synced'}), 409
    with open(os.path.join(session_dir, DICTIONARY_FILE_NAME), encoding='utf-8') as f:
        dict_content = f.read()
    lt_path = os.path.join(session_dir, LEAFTHEME_FILE_NAME)
    lt_content = json.dumps(EMPTY_LEAFTHEME)
    if os.path.exists(lt_path):
        with open(lt_path, encoding='utf-8') as f:
            lt_content = f.read()
    return flask.jsonify({'rev': _sync_rev(session_dir), 'dict': dict_content,
                          'leaftheme': lt_content, 'state': store.load_state(session_dir)})


@app.route('/api/restore', methods=['POST'])
def api_restore():
    """Put the browser's backup copy back after the server lost its files (Heroku restart)."""
    session_dir = flask.session.get('file_name')
    if not session_dir:
        return flask.jsonify({'error': 'no session'}), 401
    if os.path.exists(os.path.join(session_dir, DICTIONARY_FILE_NAME)):
        return flask.jsonify({'ok': True, 'skipped': 'server copy exists'})
    data = flask.request.get_json(silent=True) or {}
    try:
        dict_data = json.loads(data['dict'])
        lt_data = json.loads(data.get('leaftheme') or json.dumps(EMPTY_LEAFTHEME))
        state = dict(data['state'])
        dictionary.Dictionary(dict_data)  # must at least parse
    except (KeyError, TypeError, ValueError):
        return flask.jsonify({'error': 'bad backup'}), 400
    state = {k: state.get(k, v) for k, v in store.DEFAULT_STATE.items()}
    state.update(wt_dirty=False, lt_dirty=False)
    os.makedirs(session_dir, exist_ok=True)
    store.atomic_write(os.path.join(session_dir, DICTIONARY_FILE_NAME),
                       json.dumps(dict_data, ensure_ascii=False, separators=(',', ':')))
    store.atomic_write(os.path.join(session_dir, LEAFTHEME_FILE_NAME),
                       json.dumps(lt_data, ensure_ascii=False))
    store.save_state(session_dir, state)
    store.clear_ops(session_dir)
    return flask.jsonify({'ok': True})


@app.route('/authorize')
def authorize():

    import uuid
    if 'file_name' not in flask.session:
        file_name = str(uuid.uuid4())
        if not os.path.exists(file_name):
            os.makedirs(file_name)
        flask.session['file_name'] = file_name

    flow = google_auth_oauthlib.flow.Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = flask.url_for('oauth2callback', _external=True)

    authorization_url, state = flow.authorization_url(
        # Enable offline access so that you can refresh an access token without
        # re-prompting the user for permission. Recommended for web server apps.
        access_type='offline',
        # Enable incremental authorization. Recommended as a best practice.
        include_granted_scopes='true',
        # Google only returns a refresh token on consent; without this a repeat login
        # yields credentials that stop working once the access token expires.
        prompt='consent')

    flask.session['state'] = state
    flask.session['code_verifier'] = flow.code_verifier

    return flask.redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    state = flask.session['state']

    flow = google_auth_oauthlib.flow.Flow.from_client_config(_client_config(), scopes=SCOPES,
                                                             state=state)
    flow.redirect_uri = flask.url_for('oauth2callback', _external=True)

    authorization_response = flask.request.url
    flow.fetch_token(authorization_response=authorization_response,
                     code_verifier=flask.session['code_verifier'])

    flask.session['credentials'] = drive.credentials_to_session(flow.credentials)
    flask.session.permanent = True

    next_url = flask.session.pop('oauth_next', None)
    return flask.redirect(next_url or flask.url_for('sync_with_drive'))


@app.route('/clear', methods=['GET', 'POST'])
def clear_credentials():
    """Logout: forget the credentials and delete the session's local files.

    With changes that haven't reached Drive, asks first (sync then log out, or discard)."""
    discard = flask.request.method == 'POST' and flask.request.form.get('discard')
    if _has_unsaved() and not discard:
        return flask.render_template('logout_confirm.html', menu_items=get_menu_items())

    if 'credentials' in flask.session:
        del flask.session['credentials']
    if 'file_name' in flask.session:
        if os.path.exists(flask.session['file_name']):
            for root, dirs, files in os.walk(flask.session['file_name'], topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.removedirs(flask.session['file_name'])
        del flask.session['file_name']
    return flask.render_template('index.html', menu_items=get_menu_items(), logged_out=True)


def _client_config():
    return {
        "web": {
            "client_id": os.environ['G_CLIENT_ID'],
            "project_id": PROJECT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": drive.TOKEN_URI,
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_secret": os.environ['G_CLIENT_SECRET'],
        }
    }


def get_menu_items():
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    out = []
    if 'file_name' in flask.session and os.path.exists(file_name):
        out.append(("Sync with Drive *" if _has_unsaved() else "Sync with Drive", "/sync"))
        out.append(('Themes', "/themes"))
        out.append(('Search', "/search"))
        out.append(('Add Word', "/add_word"))
        out.append(('Review', "/review"))
        out.append(('Stats', "/stats"))
        out.append(('Reading', "/reading"))
    else:
        out.append(("Load Dictionary", "/sync"))
    out.append(("Logout", "/clear"))
    return out
