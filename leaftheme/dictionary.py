import json
import uuid
from datetime import datetime, timezone


class Dictionary:

    class Theme:
        def __init__(self, value):
            self.id = value['id']
            self.uid = value.get('uid', str(uuid.uuid4()))
            self.name = value['l']
            self.modified_date = value.get('dm', datetime.now(timezone.utc).isoformat())
            self.words = {}

        def __str__(self):
            return self.name

        def __repr__(self):
            return f"<Theme {self.name}: [{len(self.words)}]>"

        def add_word(self, word):
            key = str(word)
            if key in self.words:
                print('Duplicate word ' + key)
            self.words[key] = word

        def word_count(self):
            return len(self.words)

        def search(self, query):
            from rapidfuzz import process
            results = process.extract(
                query,
                [str(x) for x in self.words],
                limit=10,
                score_cutoff=50)
            out = []
            for res in results:
                out.append((res[1], self.words[res[0]], self.id, self.name))
            return out

    class Word:
        def __init__(self, value):
            self.id = value['id']
            self.uid = value.get('uid', str(uuid.uuid4()))
            self.word = value['m']
            self.translation = value['t']
            self.created_date = value.get('dc', datetime.now(timezone.utc).isoformat())
            self.modified_date = value.get('dm', datetime.now(timezone.utc).isoformat())
            self.score = value.get('tm', 0)
            self.theme = None

        def set_theme(self, theme_id):
            self.theme = theme_id

        def __str__(self):
            return self.word

        def __repr__(self):
            return f"<Word {self.word}>"

        def __eq__(self, other):
            return self.id == other.id

        def __gt__(self, other):
            return self.score > other.score

    def __init__(self, dict_dict: dict):
        self.title = dict_dict.get('libelle', '')
        self.identifier = dict_dict.get('identifier', str(uuid.uuid4()))
        self.version = dict_dict.get('version', '3')
        self.modified_date = dict_dict.get('dm', datetime.now(timezone.utc).isoformat())

        self.themes = {x['id']: self.Theme(x) for x in dict_dict['ltheme']}
        self.words = {x['id']: self.Word(x) for x in dict_dict['lword']}
        for x in dict_dict['listAssoWT']:
            self.words[x['w']].set_theme(x['t'])
            self.themes[x['t']].add_word(self.words[x['w']])

    def _next_theme_id(self):
        return max(self.themes.keys(), default=0) + 1

    def _next_word_id(self):
        return max(self.words.keys(), default=0) + 1

    def add_theme(self, name):
        theme = self.Theme({
            'id': self._next_theme_id(),
            'l': name,
        })
        self.themes[theme.id] = theme
        return theme

    def add_word(self, theme_id, word_str, translation):
        if theme_id not in self.themes:
            raise KeyError(f"Theme {theme_id} not found")
        word = self.Word({
            'id': self._next_word_id(),
            'm': word_str,
            't': translation,
        })
        word.set_theme(theme_id)
        self.words[word.id] = word
        self.themes[theme_id].add_word(word)
        return word

    def to_dict(self):
        ltheme = []
        for t in self.themes.values():
            ltheme.append({'id': t.id, 'uid': t.uid, 'l': t.name, 'dm': t.modified_date})

        lword = []
        for w in self.words.values():
            entry = {'id': w.id, 'uid': w.uid, 'm': w.word, 't': w.translation,
                     'dc': w.created_date, 'dm': w.modified_date}
            if w.score:
                entry['tm'] = w.score
            lword.append(entry)

        list_asso = []
        for w in self.words.values():
            if w.theme is not None:
                list_asso.append({'t': w.theme, 'w': w.id})

        return {
            'libelle': self.title,
            'identifier': self.identifier,
            'version': self.version,
            'dm': self.modified_date,
            'listAssoWT': list_asso,
            'ltheme': ltheme,
            'lword': lword,
            'listWordThemeAssociation': [{'idTheme': -1, 'idWord': -1}],
        }

    def search(self, query):
        results = []
        for theme in self.themes:
            results += self.themes[theme].search(query)
        return sorted(results, reverse=True)[:10]


def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    path = "../ad3a7a57-efdf-4e58-8f93-1e8110431f1a/dictionary.txt"
    with open(path, encoding="utf8") as f:
        dictionary = Dictionary(json.load(f))

    # Test: add a word to the first theme
    theme = next(iter(dictionary.themes.values()))
    new_word = dictionary.add_word(theme.id, "test_main_FI_2", "test_main_RU_2")
    print(f"Added word: {new_word!r} to theme '{theme}' (id={new_word.id})")

    # Write back
    with open(path, "w", encoding="utf8") as f:
        json.dump(dictionary.to_dict(), f, ensure_ascii=False)
    print(f"Saved to {path}")

    # Verify round-trip
    with open(path, encoding="utf8") as f:
        d2 = Dictionary(json.load(f))
    assert new_word.word in d2.themes[theme.id].words, "Word not found after round-trip!"
    print("Round-trip OK")

    print("\nLeast known 30 words from theme {}:\n".format(theme.name))
    for i, word in enumerate(sorted(theme.words.values())):
        if i > 30:
            break
        print(word)


if __name__ == '__main__':
    main()
