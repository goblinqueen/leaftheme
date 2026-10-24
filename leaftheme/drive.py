"""Google Drive access for LeafTheme.

All Drive and OAuth-credential handling lives here. Callers get plain bytes and a small set of
exceptions instead of googleapiclient/google-auth internals:

* NeedsLogin   — no usable credentials (expired/revoked token, missing refresh token); re-authorize.
* DriveMissing — the WordTheme folder or .wt file can't be found.
* DriveFailure — anything else (network, quota, server errors, unreadable .wt).
"""
import io
import os
import struct
import zipfile
from collections import namedtuple

import google.oauth2.credentials
import googleapiclient.http
from google.auth.exceptions import RefreshError, TransportError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from httplib2 import HttpLib2Error

TOKEN_URI = 'https://oauth2.googleapis.com/token'
DICTIONARY_FILE_NAME = 'dictionary.txt'
LEAFTHEME_FILE_NAME = 'leaftheme.json'
REMOVED_FILE_NAME = 'removed.txt'

Location = namedtuple('Location', 'folder_id wt_id lt_id')


class NeedsLogin(Exception):
    pass


class DriveFailure(Exception):
    pass


class DriveMissing(DriveFailure):
    pass


def credentials_from_session(data):
    """Build Credentials from the session dict. Client id/secret always come from the environment."""
    return google.oauth2.credentials.Credentials(
        token=data.get('token'),
        refresh_token=data.get('refresh_token'),
        token_uri=data.get('token_uri') or TOKEN_URI,
        client_id=os.environ['G_CLIENT_ID'],
        client_secret=os.environ['G_CLIENT_SECRET'],
        scopes=data.get('scopes'))


def credentials_to_session(credentials):
    """Serialize Credentials for the (signed, not encrypted) session cookie — no client secret."""
    return {'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'scopes': credentials.scopes}


class _Errors:
    """Context manager translating Google client exceptions into this module's exceptions."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None or isinstance(exc, (NeedsLogin, DriveFailure)):
            return False
        if isinstance(exc, RefreshError):
            raise NeedsLogin(str(exc)) from exc
        if isinstance(exc, HttpError):
            status = exc.resp.status
            if status == 401:
                raise NeedsLogin(str(exc)) from exc
            if status == 404:
                raise DriveMissing(str(exc)) from exc
            raise DriveFailure(f'Google Drive error {status}') from exc
        if isinstance(exc, (TransportError, HttpLib2Error, OSError)):
            raise DriveFailure(f'Could not reach Google Drive: {exc}') from exc
        return False


class Drive:

    def __init__(self, session_credentials):
        self._session_credentials = session_credentials
        self.credentials = credentials_from_session(session_credentials)
        self._files = build('drive', 'v3', credentials=self.credentials,
                            cache_discovery=False).files()

    def session_credentials(self):
        """Session dict reflecting any token refresh that happened during this object's calls."""
        data = dict(self._session_credentials)
        data.pop('client_id', None)
        data.pop('client_secret', None)
        data['token'] = self.credentials.token
        if self.credentials.refresh_token:
            data['refresh_token'] = self.credentials.refresh_token
        return data

    def locate(self, wt_file_id=None):
        """Find the .wt file and its folder's leaftheme.json.

        Tries the stored .wt file id first; falls back to searching the WordTheme folders
        and picking the most recently modified .wt (leaftheme.json is taken from that same folder).
        """
        if wt_file_id:
            try:
                with _Errors():
                    meta = self._files.get(fileId=wt_file_id, fields='id, trashed, parents').execute()
            except DriveMissing:
                meta = None
            if meta and not meta.get('trashed') and meta.get('parents'):
                folder_id = meta['parents'][0]
                return Location(folder_id, wt_file_id, self._find_leaftheme(folder_id))
        return self._search()

    def _search(self):
        with _Errors():
            folders = self._files.list(
                q="mimeType = 'application/vnd.google-apps.folder' and name = 'WordTheme' "
                  "and 'root' in parents and trashed = false",
                fields='files(id)').execute().get('files', [])
            if not folders:
                raise DriveMissing('No WordTheme folder found on Google Drive.')
            newest, newest_folder = None, None
            for folder in folders:
                children = self._files.list(
                    q=f"mimeType = 'application/zip' and name contains '.wt' "
                      f"and '{folder['id']}' in parents and trashed = false",
                    fields='files(id, modifiedTime)').execute().get('files', [])
                for child in children:
                    if newest is None or child['modifiedTime'] > newest['modifiedTime']:
                        newest, newest_folder = child, folder['id']
        if newest is None:
            raise DriveMissing('No .wt dictionary found in the WordTheme folder on Google Drive.')
        return Location(newest_folder, newest['id'], self._find_leaftheme(newest_folder))

    def _find_leaftheme(self, folder_id):
        with _Errors():
            files = self._files.list(
                q=f"name = '{LEAFTHEME_FILE_NAME}' and '{folder_id}' in parents and trashed = false",
                orderBy='modifiedTime desc',
                fields='files(id)').execute().get('files', [])
        return files[0]['id'] if files else None

    def metadata(self, file_id):
        """Version info for a file: md5Checksum, headRevisionId, modifiedTime."""
        with _Errors():
            return self._files.get(fileId=file_id,
                                   fields='id, md5Checksum, headRevisionId, modifiedTime').execute()

    def download(self, file_id):
        with _Errors():
            buf = io.BytesIO()
            downloader = googleapiclient.http.MediaIoBaseDownload(
                buf, self._files.get_media(fileId=file_id))
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buf.getvalue()

    def update(self, file_id, data, mimetype):
        """Replace a file's content. Returns the new metadata (id, md5Checksum)."""
        with _Errors():
            media = googleapiclient.http.MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype)
            return self._files.update(fileId=file_id, media_body=media,
                                      fields='id, md5Checksum').execute()

    def create(self, folder_id, name, data, mimetype):
        """Create a new file in folder_id. Returns its metadata (id, md5Checksum)."""
        with _Errors():
            media = googleapiclient.http.MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype)
            return self._files.create(body={'name': name, 'parents': [folder_id]},
                                      media_body=media, fields='id, md5Checksum').execute()


def read_wt(data):
    """Return the dictionary.txt bytes from a .wt archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return zf.read(DICTIONARY_FILE_NAME)
    except (zipfile.BadZipFile, KeyError) as e:
        raise DriveFailure(f'The .wt file on Google Drive is not a readable dictionary: {e}') from e


def build_wt(dictionary_bytes, removed_bytes=b''):
    """Zip dictionary.txt + removed.txt into a .wt archive the Android app can read."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, content in ((DICTIONARY_FILE_NAME, dictionary_bytes),
                              (REMOVED_FILE_NAME, removed_bytes)):
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content)
    return _fix_zip_for_android(buf.getvalue())


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
