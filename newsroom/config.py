"""Load YAML settings, the source list and the style guide."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _deep_get(d: dict, path: str, default: Any = None) -> Any:
    cur: Any = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


@dataclass
class Settings:
    raw: dict[str, Any]
    sources: list[dict[str, Any]]
    google_news: list[dict[str, Any]]
    style: str
    root: Path = ROOT
    style_ne: str = ""  # config/style_ne.md, how the Nepali edition is written
    data_dir: Path = DATA_DIR
    mock: bool = False
    fixtures_dir: Path | None = None

    def get(self, path: str, default: Any = None) -> Any:
        return _deep_get(self.raw, path, default)

    # Convenience accessors used all over the pipeline.
    @property
    def site_name(self) -> str:
        return self.get("site.name", "Nepal Wire")

    @property
    def custom_domain(self) -> str:
        """Your own domain for the site, e.g. nepalwire.com, once it points at GitHub Pages."""
        raw = str(self.get("site.custom_domain", "") or "").strip().lower()
        return raw.removeprefix("https://").removeprefix("http://").strip("/")

    @property
    def site_url(self) -> str:
        # Precedence: the SITE_URL repository variable, then site.custom_domain, then the
        # project Pages address the workflow passes as PAGES_URL, then site.url in settings.yaml.
        override = os.environ.get("SITE_URL", "").strip()
        if override:
            return override.rstrip("/")
        if self.custom_domain:
            return f"https://{self.custom_domain}"
        return (os.environ.get("PAGES_URL", "").strip() or self.get("site.url", "")).rstrip("/")

    @property
    def language(self) -> str:
        return os.environ.get("SITE_LANGUAGE") or self.get("site.language", "en")

    @property
    def timezone(self) -> str:
        return self.get("site.timezone", "Asia/Kathmandu")

    def role_model(self, role: str) -> str:
        return self.get(f"llm.roles.{role}.model") or self.get("llm.model", "claude-opus-5")

    def role_effort(self, role: str) -> str:
        return self.get(f"llm.roles.{role}.effort") or self.get("llm.effort", "high")

    def web_search_uses(self, role: str) -> int:
        return int(self.get(f"llm.web_search_uses.{role}", 0) or 0)


def load_settings(root: Path | None = None, mock: bool | None = None) -> Settings:
    base = root or ROOT
    cfg_dir = base / "config"
    with open(cfg_dir / "settings.yaml", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    with open(cfg_dir / "sources.yaml", encoding="utf-8") as fh:
        src = yaml.safe_load(fh) or {}
    style_path = cfg_dir / "style.md"
    style = style_path.read_text(encoding="utf-8") if style_path.exists() else ""
    style_ne_path = cfg_dir / "style_ne.md"
    style_ne = style_ne_path.read_text(encoding="utf-8") if style_ne_path.exists() else ""
    if mock is None:
        mock = os.environ.get("NEWSROOM_MOCK", "") not in ("", "0", "false", "False")
    return Settings(
        raw=raw,
        sources=list(src.get("sources") or []),
        google_news=list(src.get("google_news") or []),
        style=style,
        style_ne=style_ne,
        root=base,
        data_dir=base / "data",
        mock=bool(mock),
    )
