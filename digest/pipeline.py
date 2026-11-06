"""The whole run: fetch → cluster → select → summarize → save → notify."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import BUCKETS, ROOT, Config
from .fetch import fetch_all, fetch_full_text
from .github import comment_on_issue, issue_investigations
from .notify import send_email
from .render import render_html, render_markdown, save_digest, site_url
from .stories import cluster, match_investigations, score_stories, select_stories, tag_topics
from .summarize import summarize

log = logging.getLogger(__name__)


def edition_name(local: datetime) -> str:
    if local.hour < 11:
        return "Morning"
    if local.hour < 16:
        return "Midday"
    return "Evening"


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(config: Config, *, use_llm: bool = True, fixtures: Path | None = None, dry_run: bool = False,
        now: datetime | None = None, archive_dir: Path | None = None, state_file: Path | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    archive_dir = archive_dir or config.archive_dir
    state_file = state_file or config.state_file
    light = config.style == "light"
    tz = ZoneInfo(config.get("timezone", "UTC"))
    local = now.astimezone(tz)
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")

    # 1. Fetch
    articles, failed = fetch_all(config.sources, float(config.get("lookback_hours", 14)), fixtures, now)
    if not articles:
        raise RuntimeError("no articles fetched; check your network or sources.yaml")

    # 2. Group into stories and pick a balanced set
    tag_topics(articles, config)
    stories = cluster(articles, float(config.get("cluster_threshold", 0.28)))
    score_stories(stories, config, now)
    max_stories = int(config.get("max_stories", 8))
    # Light style: pick a few spares, because Claude may set aside stories that are too heavy.
    chosen = select_stories(stories, config, limit=max_stories + 4 if light else max_stories)
    per_story = int(config.get("max_articles_per_story", 6))

    investigations = list(config.investigations)
    if repo and token and not fixtures:
        label = config.get("issue_label", "investigate")
        if label:
            investigations += issue_investigations(repo, token, label)
    inv_hits = match_investigations(articles, investigations)

    if config.get("fetch_full_text", True) and not fixtures:
        picked = {a.id: a for s in chosen for a in s.pick_articles(per_story)}
        fetch_full_text(list(picked.values()), int(config.get("full_text_chars", 3500)))

    story_inputs = [(s, s.pick_articles(per_story)) for s in chosen]
    inv_inputs = [(i, inv_hits[i.id]) for i in investigations]

    # 3. Summarize
    result, mode = summarize(story_inputs, inv_inputs, config, use_llm=use_llm)
    by_id = {s["story_id"]: s for s in result.get("stories", [])}
    inv_by_id = {i["id"]: i for i in result.get("investigations", [])}

    # 4. Assemble. Links and coverage come from the fetched data, never from the model.
    out_stories = []
    for story, arts in story_inputs:
        w = by_id.get(story.id)
        if not w:
            log.warning("no summary returned for %s; skipping it", story.id)
            continue
        if w.get("too_heavy"):
            log.info("set aside as too heavy: %s", w.get("headline"))
            continue
        if len(out_stories) >= max_stories:
            break
        out_stories.append({
            "id": story.id,
            "headline": w["headline"],
            "topics": story.topics,
            "summary": w["summary"],
            "key_facts": w.get("key_facts", []),
            "perspectives": [] if light else [p for p in w.get("perspectives", []) if story.coverage.get(p["lean"])],
            "disputed": w.get("disputed_or_unclear", []),
            "why": w.get("why_it_matters", ""),
            "coverage": story.coverage,
            "n_outlets": len(story.sources),
            "blindspot": story.blindspot,
            "links": [a.to_link() for a in arts],
        })

    out_inv = []
    for inv, arts in inv_inputs:
        w = inv_by_id.get(inv.id, {})
        out_inv.append({
            "id": inv.id,
            "question": inv.question,
            "issue_number": inv.issue_number,
            "status": w.get("status", "new_developments" if arts else "no_new_developments"),
            "update": w.get("update", ""),
            "links": [a.to_link() for a in arts[:5]],
        })

    balance = {b: sum(1 for s in out_stories for l in s["links"] if l["bucket"] == b) for b in BUCKETS}
    name_of = {s.id: s.name for s in config.sources}
    digest = {
        "title": config.get("site_title", "My Daily Brief"),
        "edition": edition_name(local),
        "generated_at": now.isoformat(),
        "local_time": local.strftime("%A, %B %-d, %Y · %-I:%M %p %Z"),
        "local_date": local.strftime("%b %-d, %Y"),
        "slug": local.strftime("%Y-%m-%d-%H%M") + "-" + edition_name(local).lower(),
        "mode": mode,
        "style": config.style,
        "profile": config.profile,
        "overview": result.get("overview", ""),
        "stories": out_stories,
        "investigations": out_inv,
        "balance": balance,
        "stats": {
            "articles": len(articles),
            "stories_found": len(stories),
            "sources_ok": len(config.sources) - len(failed),
            "sources_total": len(config.sources),
            "failed": [name_of.get(k, k) for k in failed],
        },
    }
    if dry_run:
        return digest

    # 5. Save, then notify
    save_digest(digest, archive_dir, config.latest_file if archive_dir == config.archive_dir else None)
    _notify_issues(digest, inv_inputs, repo, token, state_file)
    if (config.get("email", {}) or {}).get("enabled", True):
        link = site_url()
        if link and not config.is_main:
            link += f"{config.profile}/"
        footer = f'<p style="font-size:13px"><a href="{link}">Read on the web</a></p>' if link else ""
        send_email(f"{digest['title']}: {digest['edition']} · {digest['local_date']}",
                   render_html(digest, digest["title"], root=link or "") + footer, render_markdown(digest),
                   profile=config.profile)
    return digest


def _notify_issues(digest: dict, inv_inputs, repo: str | None, token: str | None, state_file: Path) -> None:
    """Comment on `investigate` issues when articles we haven't reported before turn up."""
    state = _load_state(state_file)
    seen: dict[str, list[str]] = state.setdefault("investigation_seen", {})
    updates = {i["id"]: i for i in digest["investigations"]}
    for inv, arts in inv_inputs:
        known = set(seen.get(inv.id, []))
        fresh = [a for a in arts if a.id not in known]
        if fresh and inv.issue_number and repo and token:
            lines = [f"**{digest['edition']} edition, {digest['local_time']}**", "", updates[inv.id]["update"], ""]
            lines += [f"- [{a.source_name}]({a.url}) ({a.lean}): {a.title}" for a in fresh]
            comment_on_issue(repo, token, inv.issue_number, "\n".join(lines))
        seen[inv.id] = (list(known) + [a.id for a in fresh])[-200:]
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
