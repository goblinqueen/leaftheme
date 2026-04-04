import re
import time

import requests
from bs4 import BeautifulSoup


class GlosbeTranslator:

    def __init__(self, lang_from='fi', lang_to='ru', delay=1.0):
        self.lang_from = lang_from
        self.lang_to = lang_to
        self.delay = delay
        self._session = requests.Session()
        self._session.headers['User-Agent'] = 'Mozilla/5.0'
        self._last_request = 0.0

    def _get(self, word):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        url = f'https://glosbe.com/{self.lang_from}/{self.lang_to}/{word}'
        r = self._session.get(url, timeout=10)
        self._last_request = time.monotonic()
        return r

    def translate(self, word):
        """Return top translations for word as a comma-joined string, or '' if none found."""
        try:
            r = self._get(word)
            soup = BeautifulSoup(r.content, 'html.parser')
            translations = []
            for h3 in soup.find_all('h3'):
                txt = h3.get_text(strip=True)
                if self._is_target_lang(txt):
                    if txt not in translations:
                        translations.append(txt)
                elif translations:
                    break
            return ', '.join(translations)
        except Exception as e:
            print(f'[glosbe] fetch failed for {word!r}: {e}')
            return ''

    def _is_target_lang(self, text):
        """Check if text is in the target language."""
        if self.lang_to == 'ru':
            return bool(re.search(r'[А-Яа-яёЁ]', text))
        elif self.lang_to == 'fi':
            # Finnish uses Latin script; exclude headings/boilerplate
            return bool(text) and re.fullmatch(r'[a-zäöåA-ZÄÖÅ\s\-]+', text) is not None
        # Fallback: accept any short non-empty text
        return bool(text) and len(text) < 60
