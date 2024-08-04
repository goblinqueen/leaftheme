import json


class Dictionary:

    class Theme:
        def __init__(self, value):
            self.id = value['id']
            self.name = value['l']
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
            self.str = value['m']
            self.translation = value['t']
            self.score = value.get('tm', 0)
            self.theme = None

        def set_theme(self, theme_id):
            self.theme = theme_id

        def __str__(self):
            return self.str

        def __repr__(self):
            return f"<Word {self.str}>"

        def __eq__(self, other):
            return self.id == other.id

        def __gt__(self, other):
            return self.score > other.score

    def __init__(self, dict_dict: dict):
        self.themes = {x['id']: self.Theme(x) for x in dict_dict['ltheme']}
        self.words = {x['id']: self.Word(x) for x in dict_dict['lword']}
        for x in dict_dict['listAssoWT']:
            self.words[x['w']].set_theme(x['t'])
            self.themes[x['t']].add_word(self.words[x['w']])

    def search(self, query):
        results = []
        for theme in self.themes:
            results += self.themes[theme].search(query)
        return sorted(results, reverse=True)[:10]


def main():
    with open("../dictionary.txt", encoding="utf8") as f:
        dictionary = Dictionary(json.load(f))
    theme = next(iter(dictionary.themes.values()))
    print("Least known 30 words from theme {}:\n".format(theme.name))
    for i, word in enumerate(sorted(theme.words.values())):
        if i > 30:
            break
        print(word)


if __name__ == '__main__':
    main()
