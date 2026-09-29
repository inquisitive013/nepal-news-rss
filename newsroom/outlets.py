"""Outlet names in Nepali, and the misspellings the Nepali writer makes of them.

The source line under a Nepali caption names each outlet the way Nepali readers know it. A
name missing here stays as the record gives it. The spelling fixes run over every Nepali text
the writer returns, so a slip like रातोपाती for रातोपाटी never reaches a reader.
"""

from __future__ import annotations

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


def source_line_ne(article: Article, limit: int = 5) -> str:
    """"स्रोत: नेपाल प्रेस, रिपब्लिका, ..." for the Nepali caption: every outlet the story cites, once, in its order."""
    names: list[str] = []
    for src in article.sources:
        name = nepali_name(src.get("name", ""))
        if name and name not in names:
            names.append(name)
    return f"स्रोत: {', '.join(names[:limit])}" if names else ""
