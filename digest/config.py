"""Load and save the YAML config files in config/."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

# Five-point lean scale collapsed into the three buckets used for balance.
LEAN_BUCKET = {
    "left": "left",
    "lean-left": "left",
    "center": "center",
    "lean-right": "right",
    "right": "right",
}
BUCKETS = ("left", "center", "right")


def keyword_regex(keywords: list[str]) -> re.Pattern | None:
    """Case-insensitive whole-word/phrase matcher for a keyword list."""
    words = [k.strip() for k in keywords if k and k.strip()]
    if not words:
        return None
    alts = "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![\w-])(?:{alts})(?![\w-])", re.IGNORECASE)


@dataclass
class Source:
    id: str
    name: str
    url: str
    lean: str
    category: str = "general"
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.lean not in LEAN_BUCKET:
            raise ValueError(f"Source {self.id!r}: lean must be one of {list(LEAN_BUCKET)}, got {self.lean!r}")

    @property
    def bucket(self) -> str:
        return LEAN_BUCKET[self.lean]


@dataclass
class Topic:
    name: str
    keywords: list[str]
    weight: float = 1.0
    exclude: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)  # every article from these source ids counts
    slots: int = 1  # stories reserved for this topic per edition (if it has news)

    @cached_property
    def _match(self) -> re.Pattern | None:
        return keyword_regex(self.keywords)

    @cached_property
    def _exclude(self) -> re.Pattern | None:
        return keyword_regex(self.exclude)

    def matches(self, text: str, source_id: str | None = None) -> bool:
        if self._exclude and self._exclude.search(text):
            return False
        if source_id and source_id in self.sources:
            return True
        return bool(self._match and self._match.search(text))


@dataclass
class Investigation:
    id: str
    question: str
    keywords: list[str]
    opened: str = ""
    issue_number: int | None = None  # set when it comes from a GitHub issue

    @cached_property
    def _match(self) -> re.Pattern | None:
        return keyword_regex(self.keywords)

    def matches(self, text: str) -> bool:
        return bool(self._match and self._match.search(text))


def _load(name: str, config_dir: Path) -> dict:
    path = config_dir / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _header_comment(path: Path) -> str:
    """The leading comment block of a YAML file, kept when the CLI rewrites it."""
    if not path.exists():
        return ""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
        else:
            break
    return "\n".join(lines).rstrip() + "\n\n" if lines else ""


def save_yaml(name: str, data: dict, config_dir: Path = CONFIG_DIR) -> None:
    path = config_dir / name
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120)
    path.write_text(_header_comment(path) + body, encoding="utf-8")


@dataclass
class Config:
    settings: dict
    sources: list[Source]
    topics: list[Topic]
    mute: list[str]
    investigations: list[Investigation]
    config_dir: Path = CONFIG_DIR

    @cached_property
    def mute_regex(self) -> re.Pattern | None:
        return keyword_regex(self.mute)

    def get(self, key: str, default=None):
        return self.settings.get(key, default)


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    settings = _load("settings.yaml", config_dir)
    src = _load("sources.yaml", config_dir).get("sources", [])
    top = _load("topics.yaml", config_dir)
    inv = _load("investigations.yaml", config_dir).get("investigations", []) or []
    return Config(
        settings=settings,
        sources=[Source(**s) for s in src if s.get("enabled", True)],
        topics=[Topic(**{**t, "keywords": t.get("keywords") or []}) for t in top.get("topics", []) or []],
        mute=top.get("mute", []) or [],
        investigations=[Investigation(**{**i, "opened": str(i.get("opened", ""))}) for i in inv],
        config_dir=config_dir,
    )
