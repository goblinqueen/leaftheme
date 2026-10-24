# LeafTheme
WordTheme Dictionary sync for all types of cursed uses

Uses [WordTheme](http://wordtheme.soregainochi.com/)'s dictionary from your Google Drive if you've turned on sync in the app.
This repo is designed for Heroku deployment.

## Setup for google drive sync
You'll need to set up your google credentials (as explained [here](https://developers.google.com/identity/protocols/oauth2/web-server#creatingcred))
and set the following env variables
```
SECRET_KEY=<any string>
G_CLIENT_ID=<id from your credentials json>
G_CLIENT_SECRET=<secret from your credentials json>
```
and replace the `PROJECT_ID` constant in `leaftheme/main.py`

If you're deploying to heroku, don't forget to add env variables to app's Settings -> Config Vars

## Run locally
Python 3.12 (see `runtime.txt`).
```
$ python -m venv .venv
$ .venv\Scripts\activate          # Windows; on Linux/macOS: source .venv/bin/activate
$ pip install -r requirements.txt
$ python wsgi.py
```
The app runs on http://127.0.0.1:8080. Set the three env variables above first (e.g. from a local `dev.env`),
and add `http://127.0.0.1:8080/oauth2callback` as an authorized redirect URI for your OAuth client
in the Google Cloud console, otherwise Google login fails locally.

## Run locally without Google API
Look for a `WordTheme/*.wt` file in your Google Drive. 

It's a zip file that should have `dictionary.txt`. Extract it and load it with the data model:
```python
import json
from leaftheme.dictionary import Dictionary

with open('dictionary.txt', encoding='utf-8') as f:
    d = Dictionary(json.load(f))

print(len(d.words), 'words in', len(d.themes), 'themes')
print(d.search('kitty'))
```
