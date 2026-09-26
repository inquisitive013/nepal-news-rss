"""Assemble the system prompt for each role from markdown files plus the style guide."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .config import PROMPTS_DIR, Settings

LANGUAGE_NAMES = {"en": "English", "ne": "Nepali (नेपाली, Devanagari script)"}

# Roles whose output is prose and therefore must follow the house style closely.
STYLE_ROLES = {"writer", "reviser", "red_team", "defense", "validation_judge", "ranking_judge"}


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"missing prompt file {path}")
    return path.read_text(encoding="utf-8").strip()


def system_for(role: str, settings: Settings) -> str:
    """Common preamble + role prompt (+ style guide for prose roles). Stable per run for caching."""
    lang = settings.language
    replacements = {
        "{{site_name}}": settings.site_name,
        "{{site_tagline}}": settings.get("site.tagline", ""),
        "{{language_name}}": LANGUAGE_NAMES.get(lang, lang),
        "{{language_code}}": lang,
    }
    parts = [_read("_common"), _read(role)]
    if role in STYLE_ROLES and settings.style:
        parts.append("# House style guide (binding)\n\n" + settings.style)
    text = "\n\n---\n\n".join(parts)
    for key, val in replacements.items():
        text = text.replace(key, str(val))
    return text


def available_roles() -> list[str]:
    return sorted(p.stem for p in Path(PROMPTS_DIR).glob("*.md") if not p.stem.startswith("_"))
