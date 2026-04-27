import html as _html
import io
import json
import os
import random
import struct
import zipfile
from datetime import timedelta
from urllib.parse import quote_plus

import flask

import google.oauth2.credentials
from google.auth.exceptions import RefreshError
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.http
from googleapiclient.discovery import build
from . import dictionary
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

DICTIONARY_FILE_NAME = 'dictionary.txt'
LEAFTHEME_FILE_NAME = 'leaftheme.json'
REMOVED_FILE_NAME = 'removed.txt'
EMPTY_LEAFTHEME = {"version": 1, "forward_srs": {}, "reverse_srs": {}}

SCOPES = ['https://www.googleapis.com/auth/drive.metadata.readonly',
          'https://www.googleapis.com/auth/drive.file',
          'https://www.googleapis.com/auth/drive',
          "https://www.googleapis.com/auth/drive.metadata"
          ]

API_SERVICE_NAME = 'drive'
API_VERSION = 'v3'
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


@app.route('/load_dictionary')
def load_dictionary():
    if _has_unsaved() and not flask.request.args.get('force'):
        return flask.render_template('unsaved_warning.html', menu_items=get_menu_items())

    if 'credentials' not in flask.session:
        return flask.redirect('authorize')

    try:
        credentials = google.oauth2.credentials.Credentials(
            **flask.session['credentials'])

        drive = build(API_SERVICE_NAME, API_VERSION, credentials=credentials)

        query = ("mimeType = 'application/vnd.google-apps.folder' " +
                 "and name = 'WordTheme' and 'root' in parents and trashed=false")

        fields = 'files(id, name, mimeType, modifiedTime)'

        wt_folders = drive.files().list(q=query, fields=fields).execute()
        wt_folders = wt_folders.get("files", [])

        if not wt_folders:
            return "No WordTheme folders found."

        dict_file = None

        for item in wt_folders:
            query = "mimeType = 'application/zip' " \
                     "and name contains '.wt' " \
                     "and '{}' in parents " \
                     "and trashed=false".format(item['id'])
            results = (
                drive.files().list(q=query, fields=fields).execute()
            )
            child_items = results.get("files", [])
            for child_item in child_items:
                if not dict_file or child_item['modifiedTime'] > dict_file['modifiedTime']:
                    dict_file = child_item

        flask.session['dict_file_id'] = dict_file['id']
        flask.session['wt_folder_id'] = wt_folders[0]['id']

        request = drive.files().get_media(fileId=dict_file['id'])
        file = io.BytesIO()
        downloader = googleapiclient.http.MediaIoBaseDownload(file, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        os.makedirs(flask.session['file_name'], exist_ok=True)
        file_name = f'{flask.session['file_name']}/dictionary.zip'
        with open(file_name, 'wb') as f:
            f.write(file.getvalue())
        with zipfile.ZipFile(file_name, 'r') as zip_file:
            zip_file.extract(DICTIONARY_FILE_NAME, flask.session['file_name'])

        # Download leaftheme.json (reverse SRS data) from same folder
        lt_query = ("name = 'leaftheme.json'"
                    " and '{}' in parents"
                    " and trashed=false".format(wt_folders[0]['id']))
        lt_results = drive.files().list(q=lt_query, fields=fields).execute()
        lt_files = lt_results.get("files", [])
        lt_path = os.path.join(flask.session['file_name'], LEAFTHEME_FILE_NAME)
        if lt_files:
            flask.session['leaftheme_file_id'] = lt_files[0]['id']
            lt_request = drive.files().get_media(fileId=lt_files[0]['id'])
            lt_buf = io.BytesIO()
            lt_dl = googleapiclient.http.MediaIoBaseDownload(lt_buf, lt_request)
            lt_done = False
            while not lt_done:
                _, lt_done = lt_dl.next_chunk()
            with open(lt_path, 'wb') as f:
                f.write(lt_buf.getvalue())
        else:
            flask.session['leaftheme_file_id'] = None
            with open(lt_path, 'w', encoding='utf-8') as f:
                json.dump(EMPTY_LEAFTHEME, f)

    except RefreshError:
        return flask.redirect(flask.url_for('clear_credentials'))

    _clear_unsaved()
    _clear_removed()
    return flask.render_template('loaded.html', menu_items=get_menu_items())


@app.route('/themes')
def get_themes():
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME

    if not os.path.exists(file_name):
        return flask.redirect('load_dictionary')

    with open(file_name, encoding="utf8") as f:
        wt_dict = dictionary.Dictionary(json.load(f))
    return flask.render_template('themes.html', menu_items=get_menu_items(),
                                 themes=wt_dict.themes.values())


@app.route('/words/<theme_id>')
def get_words(theme_id):
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME

    if not os.path.exists(file_name):
        return flask.redirect('load_dictionary')

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
        return flask.redirect('load_dictionary')

    with open(file_name, encoding="utf8") as f:
        wt_dict = dictionary.Dictionary(json.load(f))

    if flask.request.method == 'POST':
        theme_id = int(flask.request.form['theme_id'])
        word_str = flask.request.form['word'].strip()
        translation = flask.request.form['translation'].strip()
        redirect_to = flask.request.form.get('redirect_to', '')

        existing = next((w for w in wt_dict.words.values()
                         if w.word.lower() == word_str.lower()), None)
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
        with open(file_name, 'w', encoding='utf8') as f:
            json.dump(wt_dict.to_dict(), f, ensure_ascii=False, separators=(',', ':'))
        _mark_unsaved()
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


@app.route('/test_extra/<int:word_id>')
def test_extra_field(word_id):
    """Temporary route: add a test extra field to one word to check Android app compatibility."""
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return flask.redirect('load_dictionary')
    word = wt_dict.words[word_id]
    word._extra['_test_prod_tm'] = 250
    _save_dictionary(wt_dict, file_name)
    return f"Added _test_prod_tm=250 to word '{word.word}' (id={word_id}). Now Save to Drive and check the app."


@app.route('/word/<int:word_id>', methods=['GET', 'POST'])
def get_word(word_id):
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return flask.redirect('load_dictionary')

    word = wt_dict.words.get(word_id)
    if word is None:
        flask.abort(404)

    if flask.request.method == 'POST':
        new_word = flask.request.form.get('word', '').strip()
        new_translation = flask.request.form.get('translation', '').strip()
        new_theme_id = flask.request.form.get('theme_id', type=int)
        redirect_to = flask.request.form.get('redirect_to', '').strip()
        if new_word:
            word.word = new_word
        if new_translation:
            word.translation = new_translation
        if new_theme_id is not None and new_theme_id != word.theme:
            wt_dict.move_word(word_id, new_theme_id)
        _save_dictionary(wt_dict, file_name)
        return flask.redirect(redirect_to or flask.url_for('get_word', word_id=word_id))

    redirect_to = flask.request.args.get('redirect_to', '').strip() or flask.request.referrer or ''
    theme = wt_dict.themes[word.theme] if word.theme is not None else None
    return flask.render_template('word.html',
                                 menu_items=get_menu_items(), word=word, theme=theme,
                                 all_themes=wt_dict.themes.values(),
                                 redirect_to=redirect_to)


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
        return flask.redirect('load_dictionary')
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
        return flask.redirect('load_dictionary')

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
        return None, flask.redirect('load_dictionary')
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
    return_url = flask.request.args.get('return_url', '/themes')
    words_json, err = _load_words_json(word_ids_str)
    if err:
        return err
    return flask.render_template('test_flashcard.html',
                                 menu_items=get_menu_items(),
                                 words_json=words_json, return_url=return_url)


@app.route('/test/typein')
def test_typein():
    word_ids_str = flask.request.args.get('word_ids', '')
    return_url = flask.request.args.get('return_url', '/themes')
    words_json, err = _load_words_json(word_ids_str)
    if err:
        return err
    return flask.render_template('test_typein.html',
                                 menu_items=get_menu_items(),
                                 words_json=words_json, return_url=return_url)


@app.route('/test/check_answer', methods=['POST'])
def test_check_answer():
    from rapidfuzz import fuzz
    data = flask.request.get_json()
    word_id = int(data['word_id'])
    answer = data['answer'].strip()

    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    with open(file_name, encoding="utf8") as f:
        wt_dict = dictionary.Dictionary(json.load(f))

    correct = wt_dict.words[word_id].word  # Finnish word is the expected answer
    # Length guard: reject if answer length is too far from correct length
    len_ratio = len(answer) / max(len(correct), 1)
    if len_ratio < 0.5 or len_ratio > 1.5:
        return flask.jsonify(matched=False, correct_answer=correct)

    score = fuzz.ratio(answer.lower(), correct.lower())
    matched = score >= 65
    return flask.jsonify(matched=matched, correct_answer=correct)


@app.route('/test/results', methods=['POST'])
def test_results():
    data = flask.request.get_json()
    return_url = data.get('return_url', '/themes')
    # Results stored in data['results'] as {word_id: rating}
    # Score updates will be added when assessment/review flows are built
    return flask.jsonify(status='ok', redirect=return_url)


# --- Review (Spaced Repetition) flow ---

REVIEW_BATCH_SIZE = 20


def _heaven_themes(wt_dict):
    """Return list of themes whose name contains 'heaven' (case-insensitive)."""
    return [t for t in wt_dict.themes.values() if 'heaven' in t.name.lower()]


@app.route('/review')
def review_pick():
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return flask.redirect('load_dictionary')

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
        return flask.redirect('load_dictionary')

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

    # Save both files to Drive and clear the unsaved flag
    try:
        if 'credentials' in flask.session:
            save_dictionary_to_drive(
                flask.session['credentials'],
                flask.session['dict_file_id'],
                flask.session['file_name']
            )
            save_leaftheme_to_drive(
                flask.session['credentials'],
                flask.session.get('leaftheme_file_id'),
                flask.session.get('wt_folder_id'),
                flask.session['file_name']
            )
            _clear_unsaved()
    except Exception:
        pass  # Don't break the review if Drive save fails

    redirect_args = {'direction': direction, 'reviewed': len(results)}
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
                                 remaining=remaining,
                                 direction=direction,
                                 continue_url=flask.url_for('review_start', **continue_args))


def _fetch_from_drive():
    """Re-download dict + leaftheme from Drive using IDs already stored in the session.
    Called automatically on dyno cold-start when temp files are missing."""
    credentials = google.oauth2.credentials.Credentials(**flask.session['credentials'])
    drive = build(API_SERVICE_NAME, API_VERSION, credentials=credentials)

    # Download .wt zip directly using the stored file ID (no folder search needed)
    request = drive.files().get_media(fileId=flask.session['dict_file_id'])
    file = io.BytesIO()
    downloader = googleapiclient.http.MediaIoBaseDownload(file, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    os.makedirs(flask.session['file_name'], exist_ok=True)
    zip_path = f'{flask.session["file_name"]}/dictionary.zip'
    with open(zip_path, 'wb') as f:
        f.write(file.getvalue())
    with zipfile.ZipFile(zip_path, 'r') as zip_file:
        zip_file.extract(DICTIONARY_FILE_NAME, flask.session['file_name'])

    # Download leaftheme.json
    lt_path = os.path.join(flask.session['file_name'], LEAFTHEME_FILE_NAME)
    if flask.session.get('leaftheme_file_id'):
        lt_request = drive.files().get_media(fileId=flask.session['leaftheme_file_id'])
        lt_buf = io.BytesIO()
        lt_dl = googleapiclient.http.MediaIoBaseDownload(lt_buf, lt_request)
        lt_done = False
        while not lt_done:
            _, lt_done = lt_dl.next_chunk()
        with open(lt_path, 'wb') as f:
            f.write(lt_buf.getvalue())
    else:
        with open(lt_path, 'w', encoding='utf-8') as f:
            json.dump(EMPTY_LEAFTHEME, f)

    _clear_removed()


def _load_dictionary():
    """Load dictionary from session file. Returns (Dictionary, file_path) or (None, path).
    If temp files are missing but credentials are in session, re-fetches from Drive silently."""
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    if not os.path.exists(file_name):
        if 'credentials' in flask.session and flask.session.get('dict_file_id'):
            try:
                _fetch_from_drive()
            except Exception:
                pass
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
    """Write leaftheme.json to session dir and mark unsaved."""
    session_dir = flask.session.get('file_name')
    if not session_dir:
        return
    lt_path = os.path.join(session_dir, LEAFTHEME_FILE_NAME)
    with open(lt_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    _mark_unsaved()



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


UNSAVED_MARKER = '_UNSAVED_'


def _mark_unsaved():
    session_dir = flask.session.get('file_name')
    if session_dir:
        open(os.path.join(session_dir, UNSAVED_MARKER), 'w').close()


def _clear_unsaved():
    session_dir = flask.session.get('file_name')
    if session_dir:
        path = os.path.join(session_dir, UNSAVED_MARKER)
        if os.path.exists(path):
            os.remove(path)


def _has_unsaved():
    session_dir = flask.session.get('file_name')
    if session_dir:
        return os.path.exists(os.path.join(session_dir, UNSAVED_MARKER))
    return False


def _save_dictionary(wt_dict, file_name):
    """Write dictionary back to the session file and mark as unsaved."""
    with open(file_name, 'w', encoding='utf8') as f:
        json.dump(wt_dict.to_dict(), f, ensure_ascii=False, separators=(',', ':'))
    _mark_unsaved()


def _removed_path():
    session_dir = flask.session.get('file_name')
    return os.path.join(session_dir, REMOVED_FILE_NAME) if session_dir else None


def _load_removed():
    """Return current removed.txt content as bytes (may be empty)."""
    path = _removed_path()
    if path and os.path.exists(path):
        with open(path, 'rb') as f:
            return f.read()
    return b''


def _append_removed(word):
    """Append a deleted word's entry to the local removed.txt."""
    from leaftheme.dictionary import _now_iso
    path = _removed_path()
    if not path:
        return
    line = f'1;{word.uid};{_now_iso()}\n'
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line)


def _clear_removed():
    """Reset removed.txt to empty (after a successful Drive upload or fresh download)."""
    path = _removed_path()
    if path:
        with open(path, 'w') as f:
            pass


@app.route('/stats')
def stats():
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return flask.redirect('load_dictionary')

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
        return flask.redirect('load_dictionary')

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


@app.route('/save_dictionary')
def save_dictionary():
    if 'credentials' not in flask.session:
        return flask.redirect('authorize')
    if 'dict_file_id' not in flask.session:
        return flask.redirect('load_dictionary')

    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    if not os.path.exists(file_name):
        return flask.redirect('load_dictionary')

    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    with open(file_name, encoding="utf8") as f:
        wt_dict = dictionary.Dictionary(json.load(f))

    try:
        save_dictionary_to_drive(
            flask.session['credentials'],
            flask.session['dict_file_id'],
            flask.session['file_name']
        )
        save_leaftheme_to_drive(
            flask.session['credentials'],
            flask.session.get('leaftheme_file_id'),
            flask.session.get('wt_folder_id'),
            flask.session['file_name']
        )
    except RefreshError:
        return flask.redirect('authorize')

    _clear_unsaved()
    _clear_removed()
    word_count = sum(t.word_count() for t in wt_dict.themes.values())
    theme_count = len(wt_dict.themes)
    return flask.render_template('saved.html', menu_items=get_menu_items(),
                                 word_count=word_count, theme_count=theme_count,
                                 title=wt_dict.title)


@app.route('/authorize')
def authorize():

    import uuid
    file_name = str(uuid.uuid4())
    if not os.path.exists(file_name):
        os.makedirs(file_name)
    flask.session['file_name'] = file_name

    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ['G_CLIENT_ID'],
                "project_id": "goblin-queendom",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_secret": os.environ['G_CLIENT_SECRET'],
                "redirect_uris": [
                    "https://leaftheme-df31c6a9a848.herokuapp.com/oauth"
                ],
                "javascript_origins": [
                    "https://leaftheme-df31c6a9a848.herokuapp.com"
                ]
            }
        }
        , scopes=SCOPES)

    flow.redirect_uri = flask.url_for('oauth2callback', _external=True)

    authorization_url, state = flow.authorization_url(
        # Enable offline access so that you can refresh an access token without
        # re-prompting the user for permission. Recommended for web server apps.
        access_type='offline',
        # Enable incremental authorization. Recommended as a best practice.
        include_granted_scopes='true')

    flask.session['state'] = state
    flask.session['code_verifier'] = flow.code_verifier

    return flask.redirect(authorization_url)


@app.route('/oauth2callback')
def oauth2callback():
    state = flask.session['state']

    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ['G_CLIENT_ID'],
                "project_id": PROJECT_ID,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "client_secret": os.environ['G_CLIENT_SECRET']
            }
        }, scopes=SCOPES, state=state)
    flow.redirect_uri = flask.url_for('oauth2callback', _external=True)

    authorization_response = flask.request.url
    flow.fetch_token(authorization_response=authorization_response,
                     code_verifier=flask.session['code_verifier'])

    credentials = flow.credentials
    flask.session['credentials'] = credentials_to_dict(credentials)
    flask.session.permanent = True

    return flask.redirect(flask.url_for('load_dictionary'))


@app.route('/clear')
def clear_credentials():
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
    return flask.render_template('index.html', menu_items=get_menu_items())


def credentials_to_dict(credentials):
    return {'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes}


def _fix_zip_for_android(data):
    """Patch a zip so Android's ZipInputStream can read it.

    Two fixes:
    1. Clear the data-descriptor flag (bit 3) from flag_bits.  Python's
       zipfile sets bit 3 but fills CRC/sizes in the local header and does
       NOT write a data descriptor record.  Android sees bit 3, ignores the
       local header sizes, then fails looking for a data descriptor.
    2. Zero external_attr in central directory entries.  Python writes OS
       file permissions (e.g. 0x81b60000) which the app doesn't expect;
       the original app-generated zips use 0x00000000.
    """
    data = bytearray(data)
    DATA_DESC_FLAG = 0x0008

    # Patch local file headers (signature PK\x03\x04, flags at offset +6)
    pos = 0
    while True:
        pos = data.find(b'PK\x03\x04', pos)
        if pos == -1:
            break
        flags = struct.unpack_from('<H', data, pos + 6)[0]
        if flags & DATA_DESC_FLAG:
            struct.pack_into('<H', data, pos + 6, flags & ~DATA_DESC_FLAG)
        pos += 4

    # Patch central directory headers (signature PK\x01\x02)
    pos = 0
    while True:
        pos = data.find(b'PK\x01\x02', pos)
        if pos == -1:
            break
        # Clear data-descriptor flag (offset +8)
        flags = struct.unpack_from('<H', data, pos + 8)[0]
        if flags & DATA_DESC_FLAG:
            struct.pack_into('<H', data, pos + 8, flags & ~DATA_DESC_FLAG)
        # Zero external_attr (offset +38)
        struct.pack_into('<I', data, pos + 38, 0)
        pos += 4

    return bytes(data)


def save_dictionary_to_drive(credentials_dict, file_id, session_dir):
    """Zip dictionary.txt from session_dir and upload it to Drive, overwriting file_id."""
    dict_path = os.path.join(session_dir, DICTIONARY_FILE_NAME)
    if not os.path.exists(dict_path):
        raise FileNotFoundError(f"No {DICTIONARY_FILE_NAME} in {session_dir}")

    # Re-serialize JSON with compact separators (no spaces) to match app format
    with open(dict_path, encoding='utf-8') as f:
        dict_data = json.load(f)
    dict_bytes = json.dumps(dict_data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        dict_info = zipfile.ZipInfo(DICTIONARY_FILE_NAME)
        dict_info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(dict_info, dict_bytes)
        # removed.txt — carries pending deletions so the Android app removes them on next sync
        removed_path = os.path.join(session_dir, REMOVED_FILE_NAME)
        removed_bytes = open(removed_path, 'rb').read() if os.path.exists(removed_path) else b''
        removed_info = zipfile.ZipInfo('removed.txt')
        removed_info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(removed_info, removed_bytes)

    # Fix zip for Android compatibility
    fixed = _fix_zip_for_android(buf.getvalue())

    # Save .wt locally for sideloading/debugging
    wt_path = os.path.join(session_dir, 'dictionary.wt')
    with open(wt_path, 'wb') as f:
        f.write(fixed)

    upload_buf = io.BytesIO(fixed)
    credentials = google.oauth2.credentials.Credentials(**credentials_dict)
    drive = build(API_SERVICE_NAME, API_VERSION, credentials=credentials)
    media = googleapiclient.http.MediaIoBaseUpload(upload_buf, mimetype='application/zip')
    drive.files().update(fileId=file_id, media_body=media).execute()


def save_leaftheme_to_drive(credentials_dict, file_id, folder_id, session_dir):
    """Upload leaftheme.json to Drive (create if new, update if exists)."""
    lt_path = os.path.join(session_dir, LEAFTHEME_FILE_NAME)
    if not os.path.exists(lt_path):
        return

    with open(lt_path, encoding='utf-8') as f:
        lt_bytes = f.read().encode('utf-8')

    credentials = google.oauth2.credentials.Credentials(**credentials_dict)
    drive = build(API_SERVICE_NAME, API_VERSION, credentials=credentials)
    media = googleapiclient.http.MediaIoBaseUpload(
        io.BytesIO(lt_bytes), mimetype='application/json')

    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    elif folder_id:
        metadata = {'name': LEAFTHEME_FILE_NAME, 'parents': [folder_id]}
        result = drive.files().create(body=metadata, media_body=media).execute()
        flask.session['leaftheme_file_id'] = result['id']


def get_menu_items():
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    out = []
    if 'file_name' in flask.session and os.path.exists(file_name):
        out.append(("Update Dictionary", "/load_dictionary"))
        unsaved = _has_unsaved()
        save_label = "Save to Drive *" if unsaved else "Save to Drive"
        out.append((save_label, "/save_dictionary"))
        out.append(('Themes', "/themes"))
        out.append(('Search', "/search"))
        out.append(('Add Word', "/add_word"))
        out.append(('Review', "/review"))
        out.append(('Stats', "/stats"))
        out.append(('Reading', "/reading"))
    else:
        out.append(("Load Dictionary", "/load_dictionary"))
    out.append(("Logout", "/clear"))
    return out
