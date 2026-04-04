import json
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote

from glosbe import GlosbeTranslator

sys.stdout.reconfigure(encoding='utf-8')

DICT_PATH = 'aac65aca-0850-4053-a81e-de793b55dbc8/dictionary.txt'
HISTORY_PATH = 'history.json'
THEME_NAME = 'Looked Up'


def extract_glosbe_lookups(history_path):
    """Extract looked-up words from Glosbe URLs in browser history.

    Returns two sets:
      fi_words — Finnish words looked up via fi/ru (need Russian translation)
      ru_words — Russian words looked up via ru/fi (need Finnish translation, then flip)
    """
    history = json.load(open(history_path, encoding='utf-8'))
    fi_words = set()
    ru_words = set()
    for entry in history:
        url = entry.get('url', '')
        if 'glosbe.com/fi/ru/' in url:
            word = url.split('/fi/ru/')[-1].split('?')[0].strip()
            if word:
                fi_words.add(unquote(word))
        # elif 'glosbe.com/ru/fi/' in url:  // too many
        #     word = url.split('/ru/fi/')[-1].split('?')[0].strip()
        #     if word:
        #         ru_words.add(unquote(word))
    return fi_words, ru_words


def now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'


def add_word(data, theme_id, next_word_id, finnish, russian):
    ts = now_iso()
    data['lword'].append({
        'id': next_word_id,
        'uid': str(uuid.uuid4()),
        'm': finnish,
        't': russian,
        'dc': ts,
        'dm': ts,
    })
    data['listAssoWT'].append({'t': theme_id, 'w': next_word_id})


def main():
    fi_words, ru_words = extract_glosbe_lookups(HISTORY_PATH)
    print(f'Found {len(fi_words)} fi→ru and {len(ru_words)} ru→fi lookups in history')

    with open(DICT_PATH, encoding='utf-8') as f:
        data = json.load(f)

    existing_words = {w['m'].lower() for w in data['lword']}

    # fi/ru: Finnish word in URL, need to fetch Russian translation
    new_fi = sorted(w for w in fi_words if w.lower() not in existing_words)
    # ru/fi: Russian word in URL, need to fetch Finnish equivalent then flip
    # We can't filter ru_words against existing_words yet since we don't know the Finnish side
    new_ru = sorted(ru_words)

    print(f'fi→ru: {len(fi_words) - len(new_fi)} already in dict, {len(new_fi)} new')
    print(f'ru→fi: {len(new_ru)} to look up')

    if not new_fi and not new_ru:
        print('Nothing to add.')
        return

    target_theme = next((t for t in data['ltheme'] if t['l'] == THEME_NAME), None)
    if target_theme is None:
        next_id = max((t['id'] for t in data['ltheme']), default=0) + 1
        target_theme = {'id': next_id, 'l': THEME_NAME, 'uid': str(uuid.uuid4())}
        data['ltheme'].append(target_theme)
        print(f"Created theme '{THEME_NAME}' (id={next_id})")
    else:
        print(f"Adding to existing theme '{THEME_NAME}' (id={target_theme['id']})")

    next_word_id = max((w['id'] for w in data['lword']), default=0) + 1
    added = 0

    # fi→ru: straightforward — Finnish is 'm', look up Russian for 't'
    if new_fi:
        print(f'\n--- fi→ru ({len(new_fi)} words) ---')
        fi_ru = GlosbeTranslator(lang_from='fi', lang_to='ru')
        for word_str in new_fi:
            translation = fi_ru.translate(word_str)
            print(f'  {word_str} → {translation or "(not found)"}')
            add_word(data, target_theme['id'], next_word_id, word_str, translation)
            next_word_id += 1
            added += 1

    # ru→fi: look up Finnish equivalents, then add as Finnish origin + Russian translation
    if new_ru:
        print(f'\n--- ru→fi ({len(new_ru)} words) ---')
        ru_fi = GlosbeTranslator(lang_from='ru', lang_to='fi')
        # Refresh existing set (may have grown from fi→ru pass above)
        existing_words = {w['m'].lower() for w in data['lword']}
        for ru_word in new_ru:
            fi_csv = ru_fi.translate(ru_word)
            if not fi_csv:
                print(f'  {ru_word} → (no Finnish translation found, skipped)')
                continue
            fi_word = fi_csv.split(',')[0].strip()
            if fi_word.lower() in existing_words:
                print(f'  {ru_word} → {fi_word} (already in dict)')
                continue
            print(f'  {ru_word} → {fi_word} (added as: {fi_word} → {ru_word})')
            add_word(data, target_theme['id'], next_word_id, fi_word, ru_word)
            existing_words.add(fi_word.lower())
            next_word_id += 1
            added += 1

    print(f'\nAdded {added} words')

    if added:
        with open(DICT_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
        print(f'Saved to {DICT_PATH}')


if __name__ == '__main__':
    main()
