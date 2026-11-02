"""Download RSS/Atom feeds and (optionally) the full text of selected articles."""
from __future__ import annotations

import calendar
import hashlib
import html
import logging
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from .config import Source

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; PersonalNewsDigest/1.0; personal RSS reader)"
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|cmpid|ocid|taid|traffic_source)", re.I)


@dataclass
class Article:
    id: str
    title: str
    url: str
    source_id: str
    source_name: str
    lean: str
    bucket: str
    category: str
    summary: str
    published: datetime | None
    text: str = ""
    topics: list[str] = field(default_factory=list)

    @property
    def blob(self) -> str:
        """The text used for keyword and topic matching."""
        return f"{self.title}. {self.summary}"

    def to_link(self) -> dict:
        return {
            "source": self.source_name,
            "lean": self.lean,
            "bucket": self.bucket,
            "title": self.title,
            "url": self.url,
            "published": self.published.isoformat() if self.published else None,
        }


def clean_text(raw: str, limit: int = 600) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rsplit(" ", 1)[0] + "…" if len(text) > limit else text


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)])
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, query, ""))


def http_get(url: str, timeout: float = 20) -> bytes:
    """GET a URL. Also accepts file:// URLs and local paths (used by tests and --fixtures)."""
    if url.startswith("file://"):
        return Path(url[7:]).read_bytes()
    if not url.startswith(("http://", "https://")):
        return Path(url).read_bytes()
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


def fetch_source(source: Source, since: datetime, url_override: str | None = None) -> list[Article]:
    data = http_get(url_override or source.url)
    feed = feedparser.parse(data)
    if feed.bozo and not feed.entries:
        raise ValueError(f"unparseable feed: {feed.bozo_exception}")
    out = []
    for e in feed.entries:
        link, title = e.get("link"), clean_text(e.get("title", ""), 300)
        if not link or not title:
            continue
        published = _entry_time(e)
        if published and published < since:
            continue
        url = normalize_url(link)
        out.append(Article(
            id=hashlib.sha1(url.encode()).hexdigest()[:10],
            title=title,
            url=url,
            source_id=source.id,
            source_name=source.name,
            lean=source.lean,
            bucket=source.bucket,
            category=source.category,
            summary=clean_text(e.get("summary") or e.get("description") or ""),
            published=published,
        ))
    return out


def fetch_all(sources: list[Source], lookback_hours: float, fixtures: Path | None = None,
              now: datetime | None = None) -> tuple[list[Article], dict[str, str]]:
    """Fetch every feed in parallel. Returns (articles, {source_id: error}) for failed feeds."""
    now = now or datetime.now(timezone.utc)
    # Sample fixtures have fixed dates, so don't drop them for being old.
    since = datetime.min.replace(tzinfo=timezone.utc) if fixtures else now - timedelta(hours=lookback_hours)

    def one(src: Source):
        override = None
        if fixtures:
            f = fixtures / f"{src.id}.xml"
            if not f.exists():
                return src, [], None  # no fixture for this source, skip quietly
            override = str(f)
        try:
            return src, fetch_source(src, since, override), None
        except Exception as exc:  # one bad feed shouldn't stop the digest
            return src, [], f"{type(exc).__name__}: {exc}"

    articles, errors, seen = [], {}, set()
    with ThreadPoolExecutor(max_workers=12) as pool:
        for src, items, err in pool.map(one, sources):
            if err:
                log.warning("feed failed: %s (%s)", src.name, err)
                errors[src.id] = err
            for a in items:
                if a.url not in seen:
                    seen.add(a.url)
                    articles.append(a)
    log.info("fetched %d articles from %d/%d sources", len(articles), len(sources) - len(errors), len(sources))
    return articles, errors


def fetch_full_text(articles: list[Article], max_chars: int) -> None:
    """Fill article.text with the main body text of each page (best-effort, in place)."""
    try:
        import trafilatura
    except ImportError:
        log.info("trafilatura not installed; using feed summaries only")
        return

    def one(a: Article) -> None:
        try:
            page = http_get(a.url, timeout=15).decode("utf-8", errors="replace")
            text = trafilatura.extract(page, include_comments=False, include_tables=False) or ""
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 300:  # shorter usually means a paywall, consent wall or video page
                a.text = text[:max_chars]
        except Exception as exc:
            log.debug("full text failed for %s: %s", a.url, exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, articles))
