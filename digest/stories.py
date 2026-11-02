"""Group articles into stories, score them, and pick a balanced set.

This step is deterministic on purpose: which stories make the digest depends
on how many outlets across the spectrum cover them and on your topics, never
on a language model's judgement.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import BUCKETS, Config, Investigation
from .fetch import Article

STOPWORDS = set("""
a about above after again against all also am amid an and any are as at be because been before being below between
both but by can could did do does doing down during each few for from further had has have having he her here hers
him his how i if in into is it its just me more most my no nor not now of off on once only or other our out over own
same she should so some such than that the their them then there these they this those through to too under until up
very was we were what when where which while who whom why will with would you your says said say new news live latest
update updates video watch report reports reportedly year years week day days time first last one two three may might
get gets got make makes made back still people according told amp just like
""".split())

_WORD = re.compile(r"[a-z0-9][a-z0-9'’\-]*[a-z0-9]|[a-z0-9]")


def tokenize(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower()):
        w = re.sub(r"['’]s$", "", w)
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]  # crude plural folding: "tariffs" -> "tariff"
        if w not in STOPWORDS and (len(w) > 2 or w.isdigit() or w in {"ai", "us", "uk", "eu", "un"}):
            out.append(w)
    return out


def _normalize(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


@dataclass
class Story:
    id: str
    articles: list[Article]
    topics: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def sources(self) -> set[str]:
        return {a.source_id for a in self.articles}

    @property
    def coverage(self) -> dict[str, int]:
        """How many distinct outlets in each bucket covered it."""
        seen = {(a.bucket, a.source_id) for a in self.articles}
        c = Counter(b for b, _ in seen)
        return {b: c.get(b, 0) for b in BUCKETS}

    @property
    def blindspot(self) -> str | None:
        """'left' or 'right' when only one side of the spectrum is covering it."""
        cov = self.coverage
        if len(self.sources) < 3:
            return None
        if cov["left"] and not cov["right"] and cov["left"] >= cov["center"]:
            return "left"
        if cov["right"] and not cov["left"] and cov["right"] >= cov["center"]:
            return "right"
        return None

    @property
    def newest(self) -> datetime | None:
        times = [a.published for a in self.articles if a.published]
        return max(times) if times else None

    def pick_articles(self, limit: int) -> list[Article]:
        """Up to `limit` articles, one source each, round-robin across left/center/right."""
        by_bucket: dict[str, list[Article]] = {b: [] for b in BUCKETS}
        used = set()
        for a in sorted(self.articles, key=lambda a: (-len(a.text), -len(a.summary))):
            if a.source_id not in used:
                used.add(a.source_id)
                by_bucket[a.bucket].append(a)
        picked: list[Article] = []
        while len(picked) < limit and any(by_bucket.values()):
            for b in ("center", "left", "right"):
                if by_bucket[b] and len(picked) < limit:
                    picked.append(by_bucket[b].pop(0))
        return picked


def cluster(articles: list[Article], threshold: float = 0.28) -> list[Story]:
    """Greedy TF-IDF clustering: each article joins the most similar story or starts a new one."""
    docs = [Counter(tokenize(a.title) * 2 + tokenize(a.summary[:300])) for a in articles]
    df = Counter(t for d in docs for t in d)
    n = len(docs)
    idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
    vecs = [_normalize({t: tf * idf[t] for t, tf in d.items()}) for d in docs]

    clusters: list[tuple[list[int], dict[str, float]]] = []  # (member indexes, centroid)
    # Longest headlines first: they carry more context and make better cluster seeds.
    for i in sorted(range(n), key=lambda i: -len(docs[i])):
        best, best_sim = None, threshold
        for ci, (_, centroid) in enumerate(clusters):
            sim = _cosine(vecs[i], centroid)
            if sim >= best_sim:
                best, best_sim = ci, sim
        if best is None:
            clusters.append(([i], dict(vecs[i])))
        else:
            members, centroid = clusters[best]
            members.append(i)
            summed = Counter()
            for m in members:
                summed.update(vecs[m])
            clusters[best] = (members, _normalize(dict(summed)))

    return [Story(id=f"s{k + 1}", articles=[articles[i] for i in members]) for k, (members, _) in enumerate(clusters)]


def tag_topics(articles: list[Article], config: Config) -> None:
    for a in articles:
        a.topics = [t.name for t in config.topics if t.matches(a.blob, a.source_id)]


def score_stories(stories: list[Story], config: Config, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    weights = {t.name: t.weight for t in config.topics}
    lookback = float(config.get("lookback_hours", 14))
    for s in stories:
        topic_counts = Counter(t for a in s.articles for t in a.topics)
        s.topics = [t for t, _ in topic_counts.most_common()]
        n_sources = len(s.sources)
        buckets_present = sum(1 for v in s.coverage.values() if v)
        topic_boost = max((weights[t] for t in s.topics), default=0.0)
        recency = 0.0
        if s.newest:
            age_h = (now - s.newest).total_seconds() / 3600
            recency = max(0.0, 1 - age_h / lookback)
        # Breadth of coverage matters most, then reaching across the spectrum, then your interests.
        s.score = n_sources + 1.5 * (buckets_present - 1) + 3.0 * topic_boost + recency


def select_stories(stories: list[Story], config: Config) -> list[Story]:
    max_stories = int(config.get("max_stories", 8))
    min_sources = int(config.get("min_sources_per_story", 2))
    mute = config.mute_regex

    eligible = [
        s for s in stories
        if not (mute and any(mute.search(a.blob) for a in s.articles))
        and (len(s.sources) >= min_sources or s.topics)
    ]
    eligible.sort(key=lambda s: s.score, reverse=True)

    chosen: list[Story] = []
    # 1) Give each topic its reserved slots, highest-weight topics first.
    for topic in sorted(config.topics, key=lambda t: -t.weight):
        already = sum(1 for s in chosen if topic.name in s.topics)
        for s in eligible:
            if len(chosen) >= max_stories or already >= topic.slots:
                break
            if topic.name in s.topics and s not in chosen:
                chosen.append(s)
                already += 1
    # 2) Fill the remaining slots with the biggest stories overall.
    for s in eligible:
        if len(chosen) >= max_stories:
            break
        if s not in chosen:
            chosen.append(s)
    chosen.sort(key=lambda s: s.score, reverse=True)
    for k, s in enumerate(chosen, 1):
        s.id = f"s{k}"
    return chosen


def match_investigations(articles: list[Article], investigations: list[Investigation],
                         limit: int = 8) -> dict[str, list[Article]]:
    """All matching articles for each investigation, newest first."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    out = {}
    for inv in investigations:
        hits = [a for a in articles if inv.matches(a.blob)]
        hits.sort(key=lambda a: a.published or epoch, reverse=True)
        out[inv.id] = hits[:limit]
    return out
