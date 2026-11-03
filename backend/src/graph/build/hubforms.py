"""Hub name forms: the word forms in which hub names are keyed and matched."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from graph.core.identity import normalise_surface

_WORD: Final = re.compile(r"\w+")
_ARTICLES: Final = frozenset({"the", "a", "an"})
_HUB_SEPARATORS: Final = re.compile(r"[-_]+")
_LETTER_DOT: Final = re.compile(r"(?<=[^\W\d_])\.(?=[^\W\d_])")
_VOWELS: Final = frozenset("aeiouy")
_SNAKE: Final = "_"
MIN_PLURAL_LETTERS: Final = 3
"""Words up to this many letters are never folded to a singular."""
MIN_IES_LETTERS: Final = 4
"""Words of more letters than this fold ``-ies`` to ``-y``."""
MIN_ACRONYM_PLURAL: Final = 2
"""An acronym plural (``APIs``) has more letters than this."""
MIN_SNAKE_PARTS: Final = 2
"""Parts an upper-case ``_`` compound (``ORDER_ITEMS``) has at least."""
MAX_ACRONYM_LETTERS: Final = 4
"""Parts of an upper-case ``_`` compound up to this many characters stay acronyms (``CORS_POLICY``)."""
_OPS: Final = "ops"
"""The ending of mixed-case practice names (DevOps, FinOps) that are never plurals."""
_ES: Final = "es"
_NO_FOLD: Final = frozenset(
    {
        "news",
        "series",
        "species",
        "means",
        "lens",
        "canvas",
        "alias",
        "atlas",
        "bias",
        "gas",
        "analytics",
        "logistics",
        "statistics",
        "economics",
        "physics",
        "ethics",
        "graphics",
        "electronics",
        "robotics",
        "goods",
    }
)
"""English words that end in ``s`` in the singular (or have no singular): never folded."""
_IE_WORDS: Final = frozenset(
    {
        "auntie",
        "birdie",
        "boogie",
        "brownie",
        "budgie",
        "calorie",
        "collie",
        "cookie",
        "cutie",
        "foodie",
        "freebie",
        "genie",
        "goalie",
        "groupie",
        "hippie",
        "hoodie",
        "lingerie",
        "magpie",
        "movie",
        "necktie",
        "newbie",
        "pixie",
        "prairie",
        "rookie",
        "selfie",
        "smoothie",
        "sortie",
        "sweetie",
        "talkie",
        "techie",
        "veggie",
        "walkie",
        "zombie",
    }
)
"""English singulars in ``-ie`` whose plural ``-ies`` drops only the ``s``."""
_IRREGULAR: Final = {
    "indices": "index",
    "matrices": "matrix",
    "vertices": "vertex",
    "appendices": "appendix",
    "analyses": "analysis",
    "crises": "crisis",
    "diagnoses": "diagnosis",
    "hypotheses": "hypothesis",
    "theses": "thesis",
    "syntheses": "synthesis",
    "parentheses": "parenthesis",
}
"""English plurals of Latin and Greek words, to their singular."""


def tokens(text: str) -> list[str]:
    """Word tokens of ``text``: NFKC, case-folded, punctuation dropped."""

    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())


def fold_plural(word: str) -> str:
    """A case-folded word's singular (module docstring, rule 4). Words of at most three letters, or
    with a non-letter, stay as they are."""

    if len(word) <= MIN_PLURAL_LETTERS or not word.isalpha() or not word.endswith("s") or word in _NO_FOLD:
        return word
    return _IRREGULAR.get(word) or _exception_singular(word) or _singular(word)


def _exception_singular(word: str) -> str | None:
    """The singular of a word known to end in ``s`` by the exceptions of rule 4 (module docstring):
    plurals of ``_NO_FOLD`` words, of ``_IE_WORDS``, ``-uses`` after a vowel and ``-ches`` after two
    vowels; None for any other word."""

    if word.endswith(_ES) and word[:-2] in _NO_FOLD:
        return word[:-2]
    if word.endswith("ies") and word[:-1] in _IE_WORDS:
        return word[:-1]
    if word.endswith("uses") and _ends_in_vowel(word[:-4]):
        return word[:-1]
    if word.endswith("ches") and _ends_in_vowel(word[:-4]) and _ends_in_vowel(word[:-5]):
        return word[:-2]
    return None


def _singular(word: str) -> str:
    """``fold_plural`` of a word known to end in ``s``, to be folded, by the general rules."""

    if word.endswith("ies") and len(word) > MIN_IES_LETTERS:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith(("uses", "xes", "shes")):
        return word[:-2]
    if word.endswith("ches") and not _ends_in_vowel(word[:-4]):
        return word[:-2]
    if word.endswith(("ss", "us", "is")):
        return word
    return word[:-1]


def _ends_in_vowel(stem: str) -> bool:
    return bool(stem) and stem[-1] in _VOWELS


def _lower_snake(text: str) -> str:
    """``text`` with the long parts of each upper-case ``_`` compound (``ORDER_ITEMS``) case-folded, so
    they fold as lower-case words; short parts stay acronyms (module docstring, rule 2)."""

    return _WORD.sub(lambda match: _lower_snake_parts(match[0]) if _is_upper_snake(match[0]) else match[0], text)


def _lower_snake_parts(token: str) -> str:
    return _SNAKE.join(part.casefold() if len(part) > MAX_ACRONYM_LETTERS else part for part in token.split(_SNAKE))


def _is_upper_snake(token: str) -> bool:
    return token.isupper() and sum(1 for part in token.split(_SNAKE) if part) >= MIN_SNAKE_PARTS


def hub_surface(name: str) -> str:
    """The identity value of a hub name (module docstring): words separated at ``.`` between letters,
    ``-`` and ``_``, no leading article, each word ``_hub_word``-ed, then ``normalise_surface``."""

    spaced = _LETTER_DOT.sub(" ", _lower_snake(unicodedata.normalize("NFKC", name)))
    words = _HUB_SEPARATORS.sub(" ", spaced).split()
    if len(words) > 1 and words[0].casefold() in _ARTICLES:
        words = words[1:]
    return normalise_surface(" ".join(_hub_word(word) for word in words))


def _hub_word(word: str) -> str:
    """An acronym keeps its form ("CORS"); its lower-case plural drops the ``s`` ("APIs"); a mixed-case
    word that ends in an upper-case letter or in ``ops`` is only case-folded ("macOS", "DevOps"); any
    other word is case-folded and ``fold_plural``-ed ("WebSockets")."""

    if word.isupper():
        return word.casefold()
    if word.endswith("s") and len(word) > MIN_ACRONYM_PLURAL and word[:-1].isupper():
        return word[:-1].casefold()
    folded = word.casefold()
    if any(char.isupper() for char in word[1:]) and (word[-1].isupper() or folded.endswith(_OPS)):
        return folded
    return fold_plural(folded)


def hub_tokens(text: str) -> list[str]:
    """Word tokens of ``text`` with plurals folded (``tokens`` then ``fold_plural``): the form in which
    hub names are matched against each other and against source lines."""

    return [fold_plural(token) for token in tokens(text)]


def hub_words(text: str) -> list[str]:
    """Word tokens of ``text`` in ``hub_surface``'s word form: NFKC, upper-case ``_`` compounds case-folded, ``-``/``_``
    and other punctuation as spaces, each word ``_hub_word``-ed.
    """

    spaced = _HUB_SEPARATORS.sub(" ", _lower_snake(unicodedata.normalize("NFKC", text)))
    return [_hub_word(word) for word in _WORD.findall(spaced)]


def hub_value_words(value: str) -> list[str]:
    """Word tokens of a hub identity value (already ``hub_surface``-d, e.g. a ``surface`` alias) in
    ``hub_words``' form, without folding again (a folded acronym such as ``cors`` stays as it is)."""

    return _WORD.findall(_HUB_SEPARATORS.sub(" ", value))
