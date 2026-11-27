import re
import unicodedata
from collections.abc import Iterator

_TOKEN = re.compile(r'[\u3400-\u9fff\U00020000-\U000323af]+|[^\W_\u3400-\u9fff\U00020000-\U000323af]+', re.UNICODE)
_HAN = re.compile(r'^[\u3400-\u9fff\U00020000-\U000323af]')


def word_runs(text: str) -> list[str]:
    return _TOKEN.findall(unicodedata.normalize('NFKC', text).casefold())


def is_han(word: str) -> bool:
    return bool(_HAN.match(word))


def tokens(text: str) -> Iterator[str]:
    for word in word_runs(text):
        if is_han(word):
            yield from word
            yield from (word[i:i + 2] for i in range(len(word) - 1))
        else:
            yield word


def fts_query(text: str) -> str:
    unique = list(dict.fromkeys(tokens(text)))[:64]
    return ' OR '.join('"' + term + '"' for term in unique)
