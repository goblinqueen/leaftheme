import json
import uuid
from datetime import datetime, timedelta, timezone


def _now_iso():
    """Generate ISO timestamp matching the WordTheme app format: milliseconds + Z suffix."""
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'


def _future_iso(days):
    """Generate ISO timestamp `days` from now."""
    t = datetime.now(timezone.utc) + timedelta(days=days)
    return t.strftime('%Y-%m-%dT%H:%M:%S.') + f'{t.microsecond // 1000:03d}Z'


def _parse_iso(s):
    """Parse an ISO timestamp string back to datetime."""
    if s is None:
        return None
    s = s.replace('Z', '+00:00')
    return datetime.fromisoformat(s)


class Dictionary:

    class Theme:
        _KNOWN_KEYS = {'id', 'uid', 'l', 'dm'}

        def __init__(self, value):
            self.id = value['id']
            self.uid = value.get('uid', str(uuid.uuid4()))
            self.name = value['l']
            self.modified_date = value.get('dm', _now_iso())
            self.words = {}
            self._extra = {k: v for k, v in value.items() if k not in self._KNOWN_KEYS}

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

        def search(self, query, reverse=False):
            from rapidfuzz import process
            words = list(self.words.values())
            if reverse:
                candidates = {w.translation: w for w in words}
            else:
                candidates = {w.word: w for w in words}
            results = process.extract(query, list(candidates.keys()), limit=10, score_cutoff=50)
            return [(res[1], candidates[res[0]], self.id, self.name) for res in results]

    class Word:
        _KNOWN_KEYS = {'id', 'uid', 'm', 't', 'dc', 'dm', 'tm', 'ca', 'di', 'dr', 'gcl'}

        def __init__(self, value):
            self.id = value['id']
            self.uid = value.get('uid', str(uuid.uuid4()))
            self.word = value['m']
            self.translation = value['t']
            self.created_date = value.get('dc', _now_iso())
            self.modified_date = value.get('dm', _now_iso())
            self.score = value.get('tm', 0)
            self.correct_answers = value.get('ca')
            self.review_interval = value.get('di')
            self.review_date = value.get('dr')
            self.grammar_context = value.get('gcl')
            self.theme = None
            self._extra = {k: v for k, v in value.items() if k not in self._KNOWN_KEYS}

        def set_theme(self, theme_id):
            self.theme = theme_id

        def __str__(self):
            return self.word

        def __repr__(self):
            return f"<Word {self.word}>"

        # SM-2 defaults
        DEFAULT_EASE = 250  # 2.5 × 100

        @property
        def ease_factor(self):
            """Ease factor as int (×100). Treats legacy/unset scores as default 250."""
            if self.score is None or self.score < 130:
                return self.DEFAULT_EASE
            return self.score

        @property
        def repetitions(self):
            return self.correct_answers or 0

        @property
        def interval_days(self):
            return self.review_interval or 0

        def is_due(self):
            """True if this word is due for review (or has never been reviewed)."""
            if self.review_date is None:
                return True
            due = _parse_iso(self.review_date)
            return datetime.now(timezone.utc) >= due

        @staticmethod
        def sm2_calculate(ease_x100, repetitions, interval_days, quality):
            """Pure SM-2 calculation. Returns (new_ease_x100, new_reps, new_interval, new_review_date_iso).

            quality: 0=fail, 1=hard, 2=good, 3=easy.
            Maps our 0-3 scale to SM-2's 0-5 scale:
              0 (fail)  → SM-2 grade 1
              1 (hard)  → SM-2 grade 3
              2 (good)  → SM-2 grade 4
              3 (easy)  → SM-2 grade 5
            """
            grade_map = {0: 1, 1: 3, 2: 4, 3: 5}
            grade = grade_map.get(quality, 1)

            ef = ease_x100 / 100.0 if ease_x100 and ease_x100 >= 130 else 2.5
            reps = repetitions or 0
            interval = interval_days or 0

            if grade >= 3:  # pass
                if reps == 0:
                    interval = 1
                elif reps == 1:
                    interval = 6
                else:
                    interval = round(interval * ef)
                reps += 1
            else:  # fail
                reps = 0
                interval = 1

            # Update ease factor: EF' = EF + (0.1 - (5-grade) * (0.08 + (5-grade) * 0.02))
            ef = ef + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
            if ef < 1.3:
                ef = 1.3

            return round(ef * 100), reps, interval, _future_iso(interval)

        def sm2_review(self, quality):
            """Apply SM-2 algorithm to this word."""
            self.score, self.correct_answers, self.review_interval, self.review_date = \
                self.sm2_calculate(self.ease_factor, self.repetitions, self.interval_days, quality)
            self.modified_date = _now_iso()

        def __eq__(self, other):
            return self.id == other.id

        def __gt__(self, other):
            return self.score > other.score

    def __init__(self, dict_dict: dict):
        self.title = dict_dict.get('libelle', '')
        self.identifier = dict_dict.get('identifier', str(uuid.uuid4()))
        self.version = dict_dict.get('version', '3')
        self.modified_date = dict_dict.get('dm', _now_iso())

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

    def move_word(self, word_id, new_theme_id):
        """Move a word from its current theme to a different theme."""
        if new_theme_id not in self.themes:
            raise KeyError(f"Theme {new_theme_id} not found")
        word = self.words[word_id]
        # Remove from old theme
        if word.theme is not None and word.theme in self.themes:
            old_theme = self.themes[word.theme]
            key = str(word)
            if key in old_theme.words:
                del old_theme.words[key]
        # Add to new theme
        word.set_theme(new_theme_id)
        self.themes[new_theme_id].add_word(word)
        return word

    def remove_word(self, word_id):
        """Remove a word from the dictionary and its theme."""
        if word_id not in self.words:
            raise KeyError(f"Word {word_id} not found")
        word = self.words.pop(word_id)
        if word.theme is not None and word.theme in self.themes:
            self.themes[word.theme].words.pop(str(word), None)
        return word

    def ensure_theme(self, name):
        """Return theme with given name, creating it if it doesn't exist."""
        for t in self.themes.values():
            if t.name == name:
                return t
        return self.add_theme(name)

    def to_dict(self):
        ltheme = []
        for t in self.themes.values():
            entry = {'id': t.id, 'uid': t.uid, 'l': t.name, 'dm': t.modified_date}
            entry.update(t._extra)
            ltheme.append(entry)

        lword = []
        for w in self.words.values():
            entry = {'id': w.id, 'uid': w.uid, 'm': w.word, 't': w.translation,
                     'dc': w.created_date, 'dm': w.modified_date}
            if w.score:
                entry['tm'] = w.score
            if w.correct_answers is not None:
                entry['ca'] = w.correct_answers
            if w.review_date is not None:
                entry['dr'] = w.review_date
            if w.review_interval is not None:
                entry['di'] = w.review_interval
            if w.grammar_context is not None:
                entry['gcl'] = w.grammar_context
            entry.update(w._extra)
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

    def due_words(self, theme_id=None):
        """Return words that are due for review, optionally filtered by theme."""
        words = self.words.values()
        if theme_id is not None:
            if theme_id not in self.themes:
                return []
            words = self.themes[theme_id].words.values()
        return [w for w in words if w.is_due()]

    def search(self, query):
        import re
        reverse = bool(re.search(r'[А-Яа-яёЁ]', query))
        results = []
        for theme in self.themes.values():
            results += theme.search(query, reverse=reverse)
        return sorted(results, reverse=True)[:10]


def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    path = "../004dd37d-cba6-4186-8dd6-158c3c0b20b8/dictionary.txt"
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
