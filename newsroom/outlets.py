"""The outlets a story leans on, for the card's source line, and the misspellings the Nepali
writer makes of their names.

The spelling fixes run over every Nepali text the writer returns, so a slip like रातोपाती for
रातोपाटी never reaches a reader.
"""

from __future__ import annotations

import re

from .models import Article

# Wrong spelling, right spelling. Only joined forms that name an outlet.
MISSPELLINGS = {
    "रातोपाती": "रातोपाटी",
    "सेतोपाती": "सेतोपाटी",
}


def fix_spellings(text: str) -> str:
    for wrong, right in MISSPELLINGS.items():
        text = text.replace(wrong, right)
    return text


def ranked_sources(article: Article) -> list[str]:
    """The story's outlets, once each, the ones its text cites most first; ties keep the list's order.

    The card's short source line then names the outlets the story leans on: on 29 September the
    first names in list order left out OnlineKhabar, the source for the Rana story's 12:30 am.
    """
    names: list[str] = []
    stems: set[str] = set()
    for src in article.sources:
        name = " ".join((src.get("name") or "").split())
        if name and _stem(name) not in stems:  # "OnlineKhabar English" and "OnlineKhabar" are one outlet here
            stems.add(_stem(name))
            names.append(name)
    text = (article.body_markdown or "").lower()

    def cited(name: str) -> int:
        return len(re.findall(r"(?<![\w])" + re.escape(_stem(name)) + r"(?![\w])", text))

    order = {name: i for i, name in enumerate(names)}
    return sorted(names, key=lambda n: (-cited(n), order[n]))


def _stem(name: str) -> str:
    """The outlet's name as a story's text cites it: no leading "the", no edition tag."""
    bare = re.sub(r"^the\s+", "", name.lower().strip())
    return re.sub(r"\s*\(?\b(english|nepali)\b\)?$", "", bare).strip()

