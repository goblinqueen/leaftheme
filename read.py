TEXT = '''
Herään arkisin aina kello 7. Pidän rauhallisista aamuista, joten herään yleensä ajoissa. Viikonloppuisin saatan nukkua hieman pidempään. Aamulla teen aamupalaa ja keitän kahvin. Aamupalaa syön lukiessa päivän lehteä. Yleensä syön kaurapuuroa lisukkeilla, mutta joskus saatan tehdä voileivän tai syödä jugurttia myslillä.
Sitten vaihdan vaatteet ja valmistaudun työpäivään. Työpäivä alkaa kello 9, joten lähden kotoa aina kello 8:30. Menen töihin linja-autolla. Aamuisin on yleensä ruuhkaa ja bussi on melkein aina täynnä. Joskus olen töissä vasta kello 9:10.
Päivällä käyn työkavereiden kanssa lounaalla ravintolassa. Olen töissä kello 17 asti. Onneksi en jää koskaan ylitöihin. Töiden jälkeen hoidan usein keskustassa asioita, käyn kaupassa tai tapaan ystäviä. Sitten menen kotiin. Joskus käyn illalla kuntosalilla, katson televisiota tai luen kirjaa. Joskus teen vähän töitä kotona illalla. Joko minä tai tyttöystäväni tekee illallisen - yleensä vuorottelemme. Illalla katsomme aina kymmenen uutiset ja sen jälkeen aloitamme iltapuuhat ja menemme nukkumaan.
'''

import re
import pyvoikko
from leaftheme.dictionary import Dictionary
import json

_current_dict = None  # set by analyze_text()


def get_translation(lemma):
    if _current_dict is None:
        return None
    for word in _current_dict.words.values():
        if word.word == lemma.lower():
            return word.translation
    return None


def analyse(token):
    if token == 'Viikonloppuisin':
        pass
    analysis = pyvoikko.analyse(token.lower())
    out = []
    for x in analysis:
        if x.CLASS in ['etunimi', 'sukunimi'] and len(analysis) > 1:
            continue
        out.append((x.BASEFORM.replace('--', '-'), x.CLASS))
    return out


def process_tokens(tokens):  # Returns (src word, base form (to lookup/add), translation, classes)
    prev_word = None
    prev_result = None
    for word in tokens:
        if prev_word:
            if get_translation(f"{prev_word} {word}"):
                yield f"{prev_word} {word}", f"{prev_word} {word}", get_translation(f"{prev_word} {word}"), None
                prev_word = word
                prev_result = None
                continue
            else:
                if prev_result:
                    yield prev_result
        if get_translation(word):
            prev_result = (word, word, get_translation(word), None)
        else:
            analysis = analyse(word)
            if analysis:
                baseforms = [b for b, c in analysis]
                classes = [c for b, c in analysis]
                prev_result = (word, baseforms, [get_translation(b) for b in baseforms], classes)
            else:
                prev_result = (word, word, 'None', None)
        prev_word = word
    if prev_result:
        yield prev_result


def _find_word_obj(lemma):
    if _current_dict is None:
        return None
    for word in _current_dict.words.values():
        if word.word == lemma.lower():
            return word
    return None


def _make_entries(base, trans, classes=None):
    """Convert process_tokens output to a list of entry dicts."""
    def _entry(b, t, c):
        w = _find_word_obj(b)
        return {
            'base': b,
            'translation': t if t and t != 'None' else None,
            'word_class': c,
            'word_id': w.id if w else None,
        }

    if isinstance(base, list):
        cls = classes or [None] * len(base)
        return [_entry(b, t, c) for b, t, c in zip(base, trans, cls)]
    return [_entry(base, trans, classes)]


def _tokenize(text):
    """Yield (token, is_word) pairs preserving all original characters.
    Hyphens between letters (e.g. jalkapallo-ottelua) are kept inside the word token.
    """
    for m in re.finditer(r'[a-zA-ZäöåÄÖÅ]+(?:-[a-zA-ZäöåÄÖÅ]+)*|[^a-zA-ZäöåÄÖÅ]+', text):
        tok = m.group()
        yield tok, tok[0].isalpha()


def analyze_text(text, wt_dict):
    """
    Analyze Finnish text and return a list of display tokens.

    Token types:
      'punct'           — spaces/punctuation, 'src' key only
      'word'            — analyzed word: 'src', 'entries', 'has_translation'
      'word_unanalyzed' — word with no analysis (last-word edge case): 'src', 'popup_html'
    """
    global _current_dict
    _current_dict = wt_dict
    try:
        pairs = list(_tokenize(text))
        words = [tok for tok, is_word in pairs if is_word]

        analyzed = list(process_tokens(words))

        # Map each word position to its analysis; bigrams span two positions
        analysis_by_pos = {}
        pos = 0
        for src, base, trans, classes in analyzed:
            wc = len(src.split())
            for i in range(pos, pos + wc):
                analysis_by_pos[i] = (src, base, trans, classes, wc, pos)
            pos += wc

        display = []
        word_pos = 0
        bigram_end = -1  # skip punct/words consumed by a bigram

        pair_idx = 0
        while pair_idx < len(pairs):
            tok, is_word = pairs[pair_idx]
            if not is_word:
                if word_pos <= bigram_end:
                    pair_idx += 1
                    continue
                display.append({'type': 'punct', 'src': tok})
            else:
                info = analysis_by_pos.get(word_pos)
                if info is None:
                    display.append({'type': 'word_unanalyzed', 'src': tok})
                elif word_pos != info[5]:
                    pass  # non-first word of a bigram, already merged
                else:
                    src, base, trans, classes, wc, _ = info
                    if wc > 1:
                        bigram_end = word_pos + wc - 1
                    entries = _make_entries(base, trans, classes)
                    display.append({
                        'type': 'word',
                        'src': src,
                        'entries': entries,
                        'has_translation': any(e['translation'] for e in entries),
                    })
                word_pos += 1
            pair_idx += 1

        return display
    finally:
        _current_dict = None


def main():
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    path = "9fb425ac-e028-4b4b-8d7c-3b22ef723c32/dictionary.txt"
    with open(path, encoding="utf8") as f:
        d = Dictionary(json.load(f))

    tokens = analyze_text(TEXT, d)
    for tok in tokens:
        if tok['type'] == 'punct':
            print(tok['src'])
        elif tok['type'] == 'word_unanalyzed':
            print(tok['src'])
        else:
            tip = ' | '.join(
                f"{e['base']} \u2013 {e['translation']}"
                for e in tok['entries'] if e['translation']
            ) or tok['src']
            print(tok['src'], tip)


if __name__ == '__main__':
    main()
