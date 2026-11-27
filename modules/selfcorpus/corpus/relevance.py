"""Admit passage evidence before fusion, independent of candidate rank."""
from collections import Counter
from unicodedata import name

from .lexical import is_han, word_runs

# Published Shared snapshot and separate bilingual control measurements, see
# docs/verification.md. No default floor for other model revisions or dev hash.
_E5_REVISION = (
    'intfloat/multilingual-e5-large@66076b8dc6e367337e3e90e6fb309fb0f3addaf6:'
    'e5-onnx-mean-l2-v1:512:1024'
)
_SEMANTIC_FLOORS = {_E5_REVISION: 0.800}
_FUNCTION_WORDS = frozenset((
    'a an and are as at be been being but by can could did do does for from had has have how i if '
    'in into is it its may might much of on or our should than that the their them these they this '
    'those to was we were what when where which who why will with would you your'
).split())


def _script(words):
    counts = Counter(name(c, '').split(' ', 1)[0] for word in words for c in word if c.isalpha())
    if not counts:
        return None
    dominant, count = counts.most_common(1)[0]
    return dominant if dominant and 2 * count > counts.total() else None


class Relevance:
    def __init__(self, query, signature):
        self.terms = frozenset(word for word in word_runs(query) if word not in _FUNCTION_WORDS)
        self.script = _script(sorted(self.terms))
        self.floor = _SEMANTIC_FLOORS.get(signature)

    def _overlap(self, words):
        present = set(words)
        han = [word for word in present if is_han(word)]
        return sum(term in present or (is_han(term) and any(term in word for word in han)) for term in self.terms)

    def lexical(self, content):
        # The index also includes titles, headings and individual Han characters.
        # None of those alone proves that the passage contains the requested topic.
        return bool(self.terms) and self._overlap(word_runs(content)) == len(self.terms)

    def semantic(self, content, cosine):
        if not self.terms or self.floor is None or not cosine >= self.floor:
            return False
        words = word_runs(content)
        passage_script = _script(words)
        if self.script and passage_script and self.script != passage_script:
            return True  # script-disjoint evidence cannot require word overlap
        # The E5 score alone can be high for a different question about the same
        # subject. With a shared script, require two thirds of the distinct terms.
        return 3 * self._overlap(words) >= 2 * len(self.terms)
