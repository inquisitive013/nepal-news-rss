"""Outlet names in Nepali, and the misspellings the Nepali writer makes of them.

The source line under a Nepali caption names each outlet the way Nepali readers know it. A
name missing here stays as the record gives it. The spelling fixes run over every Nepali text
the writer returns, so a slip like रातोपाती for रातोपाटी never reaches a reader.
"""

from __future__ import annotations

import re

from .models import Article

NEPALI_NAMES = {
    "kantipur": "कान्तिपुर",
    "kantipur (ekantipur)": "कान्तिपुर",
    "ekantipur": "कान्तिपुर",
    "onlinekhabar": "अनलाइनखबर",
    "onlinekhabar english": "अनलाइनखबर",
    "setopati": "सेतोपाटी",
    "setopati english": "सेतोपाटी",
    "ratopati": "रातोपाटी",
    "ratopati english": "रातोपाटी",
    "ratopati (english)": "रातोपाटी",
    "ratopati (nepali)": "रातोपाटी",
    "bbc nepali": "बीबीसी नेपाली",
    "dc nepal": "डीसी नेपाल",
    "nepal press": "नेपाल प्रेस",
    "annapurna post": "अन्नपूर्ण पोस्ट",
    "nagarik": "नागरिक",
    "nagarik news": "नागरिक",
    "the kathmandu post": "काठमाडौं पोस्ट",
    "kathmandu post": "काठमाडौं पोस्ट",
    "the himalayan times": "द हिमालयन टाइम्स",
    "republica": "रिपब्लिका",
    "myrepublica": "रिपब्लिका",
    "the rising nepal": "द राइजिङ नेपाल",
    "gorkhapatra": "गोरखापत्र",
    "desh sanchar": "देशसञ्चार",
    "desh sanchar (english)": "देशसञ्चार",
    "khabarhub": "खबरहब",
    "khabarhub (english)": "खबरहब",
    "the statesman": "द स्टेट्सम्यान",
}

# Wrong spelling, right spelling. Only joined forms that name an outlet.
MISSPELLINGS = {
    "रातोपाती": "रातोपाटी",
    "सेतोपाती": "सेतोपाटी",
}


def nepali_name(name: str) -> str:
    """The outlet as Nepali readers know it, or the name as given when it has no Nepali form here."""
    plain = " ".join((name or "").split())
    return NEPALI_NAMES.get(plain.lower(), plain)


def fix_spellings(text: str) -> str:
    for wrong, right in MISSPELLINGS.items():
        text = text.replace(wrong, right)
    return text


def ranked_sources(article: Article) -> list[str]:
    """The story's outlets, once each, the ones its text cites most first; ties keep the list's order.

    A short source line then names the outlets the story leans on: on 29 September the first
    five in list order left out OnlineKhabar, the source for the Rana caption's 12:30 am.
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


def source_line_ne(article: Article, limit: int = 5) -> str:
    """"स्रोत: नेपाल प्रेस, रिपब्लिका, ..." for the Nepali caption: the outlets the story cites most, once each."""
    names: list[str] = []
    for name in ranked_sources(article):
        ne = nepali_name(name)
        if ne not in names:
            names.append(ne)
    return f"स्रोत: {', '.join(names[:limit])}" if names else ""
