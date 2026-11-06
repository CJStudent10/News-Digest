"""Turn a digest into Markdown, HTML pages, an archive and an RSS feed."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import ROOT

ARCHIVE_DIR = ROOT / "archive"
TEMPLATES = ROOT / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html", "j2"], default_for_string=False),
    trim_blocks=False,
    lstrip_blocks=False,
)
_md_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=False)


def repo_url() -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"https://github.com/{repo}" if repo else None


def site_url() -> str | None:
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return None


def render_markdown(d: dict) -> str:
    return _md_env.get_template("digest.md.j2").render(d=d)


def render_html(d: dict, site_title: str, root: str = "") -> str:
    return _env.get_template("digest.html.j2").render(d=d, site_title=site_title, root=root, repo_url=repo_url(),
                                                      style=d.get("style", "balanced"))


def save_digest(d: dict, archive_dir: Path = ARCHIVE_DIR, latest_file: Path | None = None) -> Path:
    """Write <archive>/YYYY/MM/<slug>.json and .md, plus LATEST.md (or LATEST-<profile>.md)."""
    folder = archive_dir / d["slug"][:4] / d["slug"][5:7]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{d['slug']}.json").write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    md = render_markdown(d)
    (folder / f"{d['slug']}.md").write_text(md, encoding="utf-8")
    (latest_file or archive_dir.parent / "LATEST.md").write_text(md, encoding="utf-8")
    return folder / f"{d['slug']}.json"


def load_archive(archive_dir: Path = ARCHIVE_DIR, limit: int = 60) -> list[dict]:
    files = sorted(archive_dir.glob("*/*/*.json"), key=lambda p: p.stem, reverse=True)[:limit]
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def build_site(out_dir: Path, site_title: str, archive_dir: Path = ARCHIVE_DIR, limit: int = 60,
               style: str = "balanced") -> int:
    digests = load_archive(archive_dir, limit)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "digests").mkdir(parents=True)
    common = {"site_title": site_title, "repo_url": repo_url(), "style": style}

    for d in digests:
        (out_dir / "digests" / f"{d['slug']}.html").write_text(render_html(d, site_title, root="../"), encoding="utf-8")
    if digests:
        (out_dir / "index.html").write_text(render_html(digests[0], site_title), encoding="utf-8")
    else:
        (out_dir / "index.html").write_text(
            _env.get_template("archive.html.j2").render(digests=[], root="", **common), encoding="utf-8")
    (out_dir / "archive.html").write_text(
        _env.get_template("archive.html.j2").render(digests=digests, root="", **common), encoding="utf-8")
    (out_dir / "feed.xml").write_text(build_feed(digests[:20], site_title, out_dir.name if style != "balanced" else ""),
                                      encoding="utf-8")
    (out_dir / ".nojekyll").write_text("")
    return len(digests)


def build_feed(digests: list[dict], site_title: str, subdir: str = "") -> str:
    base = (site_url() or "") + (f"{subdir}/" if subdir and site_url() else "")
    items = []
    for d in digests:
        link = f"{base}digests/{d['slug']}.html"
        desc = d.get("overview") or " · ".join(s["headline"] for s in d["stories"][:5])
        pub = format_datetime(datetime.fromisoformat(d["generated_at"]))
        items.append(f"<item><title>{escape(d['edition'])} edition · {escape(d['local_time'])}</title>"
                     f"<link>{escape(link)}</link><guid isPermaLink=\"false\">{escape(d['slug'])}</guid>"
                     f"<pubDate>{pub}</pubDate><description>{escape(desc)}</description></item>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
            f"<title>{escape(site_title)}</title><link>{escape(base)}</link>"
            "<description>A personal news brief, three times a day.</description>"
            + "".join(items) + "</channel></rss>\n")
