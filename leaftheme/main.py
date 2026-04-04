import io
import json
import os
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


@app.route('/')
def index():
    return flask.render_template('index.html', menu_items=get_menu_items())


@app.route('/load_dictionary')
def load_dictionary():
    if 'credentials' not in flask.session:
        return flask.redirect('authorize')

    try:
        credentials = google.oauth2.credentials.Credentials(
            **flask.session['credentials'])
    except RefreshError:
        return flask.redirect('authorize')

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
        out.append(str(word))
    return flask.render_template('words.html',
                                 menu_items=get_menu_items(), words=out, theme=theme)


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



@app.route('/save_dictionary')
def save_dictionary():
    if 'credentials' not in flask.session:
        return flask.redirect('authorize')
    if 'dict_file_id' not in flask.session:
        return flask.redirect('load_dictionary')

    file_name = flask.session.get('file_name', '_none_') + '/' + DICTIONARY_FILE_NAME
    if not os.path.exists(file_name):
        return flask.redirect('load_dictionary')

    try:
        save_dictionary_to_drive(
            flask.session['credentials'],
            flask.session['dict_file_id'],
            flask.session['file_name']
        )
    except RefreshError:
        return flask.redirect('authorize')
    return flask.render_template('loaded.html', menu_items=get_menu_items())


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
        out.append(("Save to Drive", "/save_dictionary"))
        out.append(('Themes', "/themes"))
        out.append(('Search', "/search"))
    else:
        out.append(("Load Dictionary", "/load_dictionary"))
    out.append(("Logout", "/clear"))
    return out
