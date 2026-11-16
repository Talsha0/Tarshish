"""Typo-tolerant product search for Hebrew and English.

A query matches a product when (most of) its words are found among the
product's words. Each query word is compared with each product word and the
best of these wins:

    exact word            "watch"   -> watch          100
    start of a word       "smartw"  -> smartwatch      ~90   (partial typing)
    word plus an ending   "watches" -> watch            82   (plurals and suffixes)
    inside a word         "phone"   -> earphones        80
    one or two typos      "wacth"   -> watch          70-78  (swapped, missing, extra or wrong letter)
    typo in a partial     "erbud"   -> earbuds        60-68

How many typos are forgiven depends on the word length (none below 4 letters,
one up to 7, two from 8), so short words do not match everything.

Hebrew needs three extra steps, all in ``normalize``: vowel points are dropped,
final letters are folded into their regular form (so "שעון" also finds
"שעונים"), and geresh/gershayim are removed ("בלוטות׳" = "בלוטות"). A leading
one-letter prefix (ה, ו, ב, ל, מ, ש, כ) may be ignored: "לריצה" finds "ריצה".

Edit distances come from ``rapidfuzz`` when it can be loaded and from a small
pure-Python implementation otherwise, so search keeps working on machines where
a native library is unavailable or blocked.
"""

from __future__ import annotations

import math
import re
import unicodedata
from functools import lru_cache
from typing import Callable, Iterable, Sequence, TypeVar

T = TypeVar("T")

MAX_QUERY_WORDS = 8          # longer queries are cut: they add cost, not precision
_MIN_MATCHED_SHARE = 0.6     # 1 of 1, 2 of 2, 2 of 3, 3 of 4 … query words must be found

_FINAL_LETTERS = str.maketrans("ךםןףץ", "כמנפצ")
_HEBREW_PREFIXES = frozenset("הובלמשכ")
_QUOTE_MARKS = re.compile("[׳״'\"`’”]")   # geresh, gershayim and their ASCII stand-ins
_NOT_WORD = re.compile(r"[\W_]+")


def _py_osa_distance(a: str, b: str, cutoff: int) -> int:
    """Optimal string alignment distance (edits + adjacent swaps), ``cutoff + 1`` when above ``cutoff``."""
    if abs(len(a) - len(b)) > cutoff:
        return cutoff + 1
    before: list[int] = []
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            cost = previous[j - 1] + (char_a != char_b)
            cost = min(cost, previous[j] + 1, current[j - 1] + 1)
            if i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                cost = min(cost, before[j - 2] + 1)
            current.append(cost)
        if min(current) > cutoff:
            return cutoff + 1
        before, previous = previous, current
    return previous[-1] if previous[-1] <= cutoff else cutoff + 1


try:
    from rapidfuzz.distance import OSA as _OSA

    def edit_distance(a: str, b: str, cutoff: int) -> int:
        return int(_OSA.distance(a, b, score_cutoff=cutoff))

    ENGINE = "rapidfuzz"
except Exception:  # not installed, or its native library was refused by the operating system
    edit_distance = _py_osa_distance
    ENGINE = "python"


def normalize(text: str) -> str:
    """Lower-case, strip accents / Hebrew points / punctuation and fold Hebrew final letters."""
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _QUOTE_MARKS.sub("", text)
    return _NOT_WORD.sub(" ", text).translate(_FINAL_LETTERS).strip()


@lru_cache(maxsize=8192)
def tokenize(text: str) -> tuple[str, ...]:
    """Distinct normalised words of ``text``, in order of first appearance."""
    return tuple(dict.fromkeys(normalize(text).split()))


def allowed_typos(length: int) -> int:
    return 0 if length < 4 else 1 if length < 8 else 2


def _word_score(query: str, word: str) -> float:
    if query == word:
        return 100.0
    if len(query) >= 2 and word.startswith(query):
        return 86.0 + 8.0 * len(query) / len(word)
    if len(query) >= 3 and query in word:
        return 80.0
    if len(word) >= 3 and query.startswith(word) and len(query) - len(word) <= 3:
        return 82.0  # the product word plus an ending: "watches" -> watch, "שעונים" -> שעון
    budget = allowed_typos(len(query))
    if budget == 0:
        return 0.0
    if abs(len(query) - len(word)) <= budget:
        distance = edit_distance(query, word, budget)
        if distance <= budget:
            return 86.0 - 8.0 * distance
    if len(word) > len(query) + 1:
        # The shopper may still be typing, or mean the last part of a compound ("wacth" -> smartwatch):
        # compare with the start and with the end of the word.
        distance = min(
            edit_distance(query, word[: len(query)], budget),
            edit_distance(query, word[: len(query) + 1], budget),
            edit_distance(query, word[-len(query):], budget),
        )
        if distance <= budget:
            return 76.0 - 8.0 * distance
    return 0.0


def word_score(query: str, word: str) -> float:
    """0-100: how well one query word matches one product word."""
    score = _word_score(query, word)
    if score < 100.0 and len(query) >= 4 and query[0] in _HEBREW_PREFIXES:
        score = max(score, 0.94 * _word_score(query[1:], word))
    return score


def match_score(query: str, texts: Sequence[str]) -> float:
    """0-100 relevance of a product described by ``texts``; 0 means "not a match".

    The first two texts are the titles: a query that appears in one as a phrase scores higher.
    """
    words = tokenize(query)[:MAX_QUERY_WORDS]
    if not words:
        return 0.0
    candidates = tuple(dict.fromkeys(token for text in texts if text for token in tokenize(text)))
    if not candidates:
        return 0.0
    scores = [max(word_score(word, candidate) for candidate in candidates) for word in words]
    matched = sum(1 for score in scores if score > 0)
    if matched < math.ceil(len(words) * _MIN_MATCHED_SHARE):
        return 0.0
    total = sum(scores) / len(words)
    phrase = " ".join(words)
    if len(words) > 1 and any(phrase in normalize(text) for text in texts[:2] if text):
        total += 4.0
    return min(total, 100.0)


def rank(query: str, items: Iterable[T], texts: Callable[[T], Sequence[str]]) -> list[tuple[T, float]]:
    """Items matching ``query``, best first. Equal scores keep the order of ``items``."""
    scored = [(item, match_score(query, texts(item))) for item in items]
    matches = [(item, score) for item, score in scored if score > 0]
    matches.sort(key=lambda pair: -pair[1])
    return matches
