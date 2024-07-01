import io
import json
import os.path
import zipfile

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

# If modifying these scopes, delete the file token.json.
from dictionary.sync_dictionary import Dictionary

SCOPES = ['https://www.googleapis.com/auth/drive.file',
          'https://www.googleapis.com/auth/drive',
          'https://www.googleapis.com/auth/drive.file',
          'https://www.googleapis.com/auth/drive.metadata'
          ]


def main():
    """Shows basic usage of the Drive v3 API.
    Prints the names and ids of the first 10 files the user has access to.
    """
    creds = None
    # The file token.json stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time.
    if os.path.exists("token.json"):
        try:
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        except ValueError as e:
            print("Reading creds from file failed: {}".format(e))
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=8080, redirect_uri_trailing_slash=False)
        # Save the credentials for the next run
        with open("token.json", "w") as token:
            token.write(creds.to_json())

    try:
        service = build("drive", "v3", credentials=creds)

        # Call the Drive v3 API
        search = "mimeType = 'application/vnd.google-apps.folder' " \
                 "and name = 'WordTheme' and 'root' in parents and trashed=false"
        # search = "mimeType = 'application/vnd.google-apps.folder' " \
        #          "and name = 'WordTheme' " \
        #          "and 'root' in parents " \
        #          "and trashed=false"
        #
        # search = "mimeType = 'application/vnd.google-apps.folder' " \
        #          "and name contains '.wt' " \
        #          "and 'WordTheme' in parents " \
        #          "and trashed=false"

        fields = 'files(id, name, mimeType, modifiedTime)'

        results = (
            service.files().list(q=search, fields=fields).execute()
        )
        items = results.get("files", [])

        if not items:
            print("No files found.")
            return
        dict_file_id = None
        for item in items:
            search = "mimeType = 'application/zip' " \
                     "and name contains '.wt' " \
                     "and '{}' in parents " \
                     "and trashed=false".format(item['id'])
            results = (
                service.files().list(q=search, fields=fields).execute()
            )
            child_items = results.get("files", [])
            for child_item in child_items:
                print(f"{child_item['name']} ({child_item['id']}) ({child_item['modifiedTime']})")
                dict_file_id = child_item['id']

        request = service.files().get_media(fileId=dict_file_id)
        file = io.BytesIO()
        downloader = MediaIoBaseDownload(file, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
            print(f"Download {int(status.progress() * 100)}.")
        with open('1.zip', 'wb') as f:
            f.write(file.getvalue())
        input_zip = zipfile.ZipFile('1.zip')

        dict = Dictionary(json.loads(input_zip.read('dictionary.txt')))
        print(dict.themes)
        heaven = dict.themes[31]
        out = []
        for i, word in enumerate(sorted(heaven.words)):
            if i > 30:
                break
            print(word)
            out.append(str(word))
        return "<br />".join(out)

    except HttpError as error:
        print(f"An error occurred: {error}")


if __name__ == "__main__":
    main()
