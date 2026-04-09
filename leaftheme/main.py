import io
import json
import os
import random
import struct
import zipfile

import flask

import google.oauth2.credentials
from google.auth.exceptions import RefreshError
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.http
from googleapiclient.discovery import build
from . import dictionary
from glosbe import GlosbeTranslator

_glosbe = GlosbeTranslator(lang_from='fi', lang_to='ru', delay=0)

PROJECT_ID = "goblin-queendom"

DICTIONARY_FILE_NAME = 'dictionary.txt'

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

        request = drive.files().get_media(fileId=dict_file['id'])
        file = io.BytesIO()
        downloader = googleapiclient.http.MediaIoBaseDownload(file, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        file_name = f'{flask.session['file_name']}/dictionary.zip'
        with open(file_name, 'wb') as f:
            f.write(file.getvalue())
        with zipfile.ZipFile(file_name, 'r') as zip_file:
            zip_file.extract(DICTIONARY_FILE_NAME, flask.session['file_name'])

    except RefreshError:
        return flask.redirect(flask.url_for('clear_credentials'))

    _clear_unsaved()
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
        wt_dict.add_word(theme_id, word_str, translation)
        with open(file_name, 'w', encoding='utf8') as f:
            json.dump(wt_dict.to_dict(), f, ensure_ascii=False, separators=(',', ':'))
        _mark_unsaved()
        return flask.redirect(flask.url_for('get_words', theme_id=theme_id))

    preselected = flask.request.args.get('theme_id', type=int)
    return flask.render_template('add_word.html',
                                 menu_items=get_menu_items(),
                                 themes=wt_dict.themes.values(),
                                 preselected=preselected)


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
        if new_word:
            word.word = new_word
        if new_translation:
            word.translation = new_translation
        if new_theme_id is not None and new_theme_id != word.theme:
            wt_dict.move_word(word_id, new_theme_id)
        _save_dictionary(wt_dict, file_name)
        return flask.redirect(flask.url_for('get_word', word_id=word_id))

    theme = wt_dict.themes[word.theme] if word.theme is not None else None
    return flask.render_template('word.html',
                                 menu_items=get_menu_items(), word=word, theme=theme,
                                 all_themes=wt_dict.themes.values())


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


@app.route('/word/<int:word_id>/delete', methods=['POST'])
def delete_word(word_id):
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return flask.redirect('load_dictionary')
    word = wt_dict.words.get(word_id)
    if word is None:
        flask.abort(404)
    theme_id = word.theme
    wt_dict.remove_word(word_id)
    _save_dictionary(wt_dict, file_name)
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


@app.route('/review')
def review_pick():
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return flask.redirect('load_dictionary')

    # Count due words per theme + total
    theme_counts = []
    total_due = 0
    for t in wt_dict.themes.values():
        due = len(wt_dict.due_words(t.id))
        theme_counts.append((t, due))
        total_due += due

    return flask.render_template('review_pick.html',
                                 menu_items=get_menu_items(),
                                 theme_counts=theme_counts,
                                 total_due=total_due)


@app.route('/review/start')
def review_start():
    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return flask.redirect('load_dictionary')

    theme_id = flask.request.args.get('theme_id', type=int)
    due = wt_dict.due_words(theme_id)

    if not due:
        return flask.render_template('review_done.html',
                                     menu_items=get_menu_items(),
                                     reviewed=0, theme_id=theme_id)

    batch = random.sample(due, min(REVIEW_BATCH_SIZE, len(due)))
    words_list = [{'id': w.id, 'word': w.word, 'translation': w.translation} for w in batch]
    words_json = json.dumps(words_list, ensure_ascii=False)

    return flask.render_template('review_session.html',
                                 menu_items=get_menu_items(),
                                 words_json=words_json,
                                 theme_id=theme_id,
                                 total_due=len(due))


@app.route('/review/apply', methods=['POST'])
def review_apply():
    """Apply SM-2 ratings to reviewed words."""
    data = flask.request.get_json()
    results = data.get('results', {})  # {word_id_str: quality}
    theme_id = data.get('theme_id')

    wt_dict, file_name = _load_dictionary()
    if wt_dict is None:
        return flask.jsonify(status='error', message='No dictionary'), 400

    for word_id_str, quality in results.items():
        word_id = int(word_id_str)
        if word_id in wt_dict.words:
            wt_dict.words[word_id].sm2_review(quality)

    _save_dictionary(wt_dict, file_name)

    if theme_id is not None:
        redirect = flask.url_for('review_start', theme_id=theme_id)
    else:
        redirect = flask.url_for('review_start')
    return flask.jsonify(status='ok', reviewed=len(results), redirect=redirect)


def _load_dictionary():
    """Load dictionary from session file. Returns (Dictionary, file_path) or redirects."""
    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    if not os.path.exists(file_name):
        return None, file_name
    with open(file_name, encoding="utf8") as f:
        return dictionary.Dictionary(json.load(f)), file_name


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
    except RefreshError:
        return flask.redirect('authorize')

    _clear_unsaved()
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
        # removed.txt must exist in the archive (app requires it)
        removed_info = zipfile.ZipInfo('removed.txt')
        removed_info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(removed_info, b'')

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
    else:
        out.append(("Load Dictionary", "/load_dictionary"))
    out.append(("Logout", "/clear"))
    return out
