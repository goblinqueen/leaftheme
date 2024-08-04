import io
import json
import os
import uuid
import zipfile

import flask
import requests

import google.oauth2.credentials
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.http
from googleapiclient.discovery import build
from . import dictionary

PROJECT_ID = "goblin-queendom"

DICTIONARY_FILE = 'dictionary.txt'

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

    # Load credentials from the session.
    credentials = google.oauth2.credentials.Credentials(
        **flask.session['credentials'])

    drive = build(API_SERVICE_NAME, API_VERSION, credentials=credentials)

    search = ("mimeType = 'application/vnd.google-apps.folder' " +
              "and name = 'WordTheme' and 'root' in parents and trashed=false")

    fields = 'files(id, name, mimeType, modifiedTime)'

    wt_folders = drive.files().list(q=search, fields=fields).execute()
    wt_folders = wt_folders.get("files", [])

    if not wt_folders:
        return "No WordTheme folders found."

    dict_file = None

    for item in wt_folders:
        search = "mimeType = 'application/zip' " \
                 "and name contains '.wt' " \
                 "and '{}' in parents " \
                 "and trashed=false".format(item['id'])
        results = (
            drive.files().list(q=search, fields=fields).execute()
        )
        child_items = results.get("files", [])
        for child_item in child_items:
            if not dict_file or child_item['modifiedTime'] > dict_file['modifiedTime']:
                dict_file = child_item

    request = drive.files().get_media(fileId=dict_file['id'])
    file = io.BytesIO()
    downloader = googleapiclient.http.MediaIoBaseDownload(file, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
    file_name = 'dictionary.zip'
    with open(file_name, 'wb') as f:
        f.write(file.getvalue())
    with zipfile.ZipFile(file_name, 'r') as zip_file:
        zip_file.extract(DICTIONARY_FILE, '.')

    return flask.render_template('loaded.html', menu_items=get_menu_items())


@app.route('/themes')
def get_themes():
    if not os.path.exists(DICTIONARY_FILE):
        return flask.redirect('load_dictionary')

    with open(DICTIONARY_FILE, encoding="utf8") as f:
        wt_dict = dictionary.Dictionary(json.load(f))
    return flask.render_template('themes.html', menu_items=get_menu_items(),
                                 themes=wt_dict.themes.values())


@app.route('/words/<theme_id>')
def get_words(theme_id):
    if not os.path.exists(DICTIONARY_FILE):
        return flask.redirect('load_dictionary')

    with open(DICTIONARY_FILE, encoding="utf8") as f:
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
    if not os.path.exists(DICTIONARY_FILE):
        return flask.redirect('load_dictionary')

    query = ""
    results = []

    if 'query' in flask.request.args:
        query = flask.request.args.get('query')

        with open(DICTIONARY_FILE, encoding="utf8") as f:
            wt_dict = dictionary.Dictionary(json.load(f))

        results = wt_dict.search(query)

    return flask.render_template('search.html',
                                 menu_items=get_menu_items(), query=query, results=results)



@app.route('/authorize')
def authorize():
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
    print(authorization_response)
    flow.fetch_token(authorization_response=authorization_response)

    credentials = flow.credentials
    flask.session['credentials'] = credentials_to_dict(credentials)

    return flask.redirect(flask.url_for('load_dictionary'))


@app.route('/clear')
def clear_credentials():
    if 'credentials' in flask.session:
        del flask.session['credentials']
    if os.path.exists(DICTIONARY_FILE):
        os.remove(DICTIONARY_FILE)
    return flask.render_template('index.html', menu_items=get_menu_items())


def credentials_to_dict(credentials):
    return {'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes}


def get_menu_items():
    out = []
    if os.path.exists(DICTIONARY_FILE):
        out.append(("Update Dictionary", "/load_dictionary"))
        out.append(('Themes', "/themes"))
        out.append(('Search', "/search"))
    else:
        out.append(("Load Dictionary", "/load_dictionary"))
    out.append(("Logout", "/clear"))
    return out
