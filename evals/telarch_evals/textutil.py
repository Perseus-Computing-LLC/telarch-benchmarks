"""Text helpers: the keyword planner used to query Kibisis, and answer normalization for F1."""

from __future__ import annotations

import hashlib
import re
import string
from collections import Counter

# English function words and question words. A question is reduced to its content words before it
# reaches Kibisis, because Kibisis full-text search requires every term of a query to match.
STOPWORDS = frozenset("""
a about above after again against ago all also am an and any are aren as at be because been before
being below between both but by can cannot could couldn did didn do does doesn doing don down during
each else ever few for from further get gets got had hadn has hasn have haven having he her here hers
herself him himself his how i if in into is isn it its itself just let ll me might mightn more most
much must mustn my myself no nor not now of off on once only or other our ours ourselves out over own
please re s same shall shan she should shouldn so some such t tell than that the their theirs them
themselves then there these they this those through to too under until up us ve very was wasn way we
were weren what whats when where whether which while who whom whose why will with won would wouldn y
yet you your yours yourself yourselves
""".split())  # noqa: SIM905 - one word list reads better than 160 literals

STOPWORDS_SHA256 = hashlib.sha256(" ".join(sorted(STOPWORDS)).encode()).hexdigest()

_WORD = re.compile(r"[a-z0-9]+")


def keyword_terms(text: str, max_terms: int = 16) -> list[str]:
    """Content words of `text` in first-seen order, lowercased, without stopwords or one-letter tokens."""
    terms: list[str] = []
    for word in _WORD.findall(text.lower()):
        if word in STOPWORDS or (len(word) < 2 and not word.isdigit()):
            continue
        if word not in terms:
            terms.append(word)
    if not terms:  # a question made only of stopwords: fall back to its words
        terms = list(dict.fromkeys(w for w in _WORD.findall(text.lower()) if len(w) > 1))
    return terms[:max_terms]


def normalize_answer(text: str) -> str:
    """Lowercase, drop punctuation and the words a, an, the and and, collapse whitespace."""
    text = str(text).lower()
    text = "".join(ch for ch in text if ch not in set(string.punctuation))
    text = re.sub(r"\b(a|an|the|and)\b", " ", text)
    return " ".join(text.split())


def token_f1(prediction: str, gold: str) -> float:
    pred_tokens = normalize_answer(prediction).split()
    gold_tokens = normalize_answer(gold).split()
    if not pred_tokens or not gold_tokens:
        return float(pred_tokens == gold_tokens)
    common = Counter(pred_tokens) & Counter(gold_tokens)
    same = sum(common.values())
    if same == 0:
        return 0.0
    precision = same / len(pred_tokens)
    recall = same / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)
