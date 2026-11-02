from datetime import datetime, timezone
from pathlib import Path

import pytest

from digest import summarize as summ
from digest.config import keyword_regex, load_config
from digest.fetch import fetch_all, normalize_url
from digest.github import parse_keywords
from digest.pipeline import edition_name, run
from digest.render import build_site, render_markdown
from digest.stories import cluster, score_stories, select_stories, tag_topics

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def articles(cfg):
    arts, failed = fetch_all(cfg.sources, 14, fixtures=FIXTURES, now=NOW)
    assert not failed
    return arts


@pytest.fixture(scope="module")
def selected(cfg, articles):
    tag_topics(articles, cfg)
    stories = cluster(articles, cfg.get("cluster_threshold"))
    score_stories(stories, cfg, NOW)
    return select_stories(stories, cfg)


def _find(stories, word):
    return next(s for s in stories if any(word in a.title.lower() for a in s.articles))


def test_keyword_regex_matches_whole_words_only():
    rx = keyword_regex(["ai", "white house"])
    assert rx.search("New AI rules") and rx.search("the White  House said")
    assert not rx.search("she said") and not rx.search("Maine") and not rx.search("whitehouse")


def test_tracking_params_are_stripped():
    assert normalize_url("https://Example.com/a?utm_source=rss&id=3#top") == "https://example.com/a?id=3"


def test_same_story_from_different_outlets_is_grouped(selected):
    fed = _find(selected, "federal reserve")
    assert len(fed.sources) == 5
    assert fed.coverage == {"left": 1, "center": 2, "right": 2}
    assert all("rate" in a.title.lower() for a in fed.articles)


def test_one_sided_stories_are_flagged(selected):
    assert _find(selected, "voucher").blindspot == "right"
    assert _find(selected, "climate adaptation").blindspot == "left"
    assert _find(selected, "federal reserve").blindspot is None


def test_muted_and_uninteresting_singletons_are_dropped(selected):
    titles = [a.title.lower() for s in selected for a in s.articles]
    assert not any("horoscope" in t for t in titles)
    assert not any("orchestra" in t for t in titles)  # one outlet, matches no topic


def test_every_topic_with_news_gets_a_slot(cfg, selected):
    covered = {t for s in selected for t in s.topics}
    assert {"Economy", "Tech & AI", "Science & Space", "World Affairs"} <= covered


def test_pick_articles_spreads_across_spectrum(selected):
    picked = _find(selected, "federal reserve").pick_articles(3)
    assert {a.bucket for a in picked} == {"left", "center", "right"}


def test_issue_keywords_parsing():
    body = "### Keywords\n\nfederal reserve, rate cut\n\n### Notes\n\nwatching this"
    assert parse_keywords(body, "x") == ["federal reserve", "rate cut"]
    assert parse_keywords("keywords: spacex, starship", "x") == ["spacex", "starship"]
    assert "starship" in parse_keywords("", "Will Starship launch again?")


def test_schema_is_valid_for_structured_outputs():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        if node.get("type") == "array":
            walk(node["items"])
    walk(summ.SCHEMA)


def test_edition_names():
    assert [edition_name(datetime(2026, 1, 1, h)) for h in (7, 12, 18)] == ["Morning", "Midday", "Evening"]


def test_basic_mode_end_to_end(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    d = run(cfg, fixtures=FIXTURES, now=NOW, archive_dir=tmp_path / "archive", state_file=tmp_path / "s.json")
    assert d["mode"] == "basic" and d["stories"]
    assert all(l["url"].startswith("https://example.com/") for s in d["stories"] for l in s["links"])
    inv = d["investigations"][0]
    assert inv["status"] == "new_developments" and "AI Act" in inv["links"][0]["title"]
    assert list((tmp_path / "archive").glob("*/*/*.json"))
    n = build_site(tmp_path / "site", "Test", archive_dir=tmp_path / "archive")
    assert n == 1 and (tmp_path / "site" / "index.html").read_text().count('class="story"') == len(d["stories"])
    assert (tmp_path / "site" / "feed.xml").exists()
    page = (tmp_path / "site" / "index.html").read_text()
    assert inv["update"] in page and "built-in method" not in page
    assert inv["update"] in render_markdown(d)


def test_ai_mode_uses_model_prose_but_data_links(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    captured = {}

    def fake_claude(prompt, config):
        captured["prompt"] = prompt
        return {
            "overview": "A quiet day.",
            "stories": [{
                "story_id": "s1", "headline": "Fed holds rates", "summary": "The Fed held.",
                "key_facts": ["Rates unchanged"],
                "perspectives": [{"lean": "left", "framing": "Labor market"},
                                 {"lean": "right", "framing": "Inflation"}],
                "disputed_or_unclear": [], "why_it_matters": "Mortgages.",
            }],
            "investigations": [],
        }

    monkeypatch.setattr(summ, "summarize_with_claude", fake_claude)
    d = run(cfg, fixtures=FIXTURES, now=NOW, dry_run=True)
    assert d["mode"] == "ai" and d["overview"] == "A quiet day."
    assert len(d["stories"]) == 1  # stories the model skipped are dropped, not invented
    s = d["stories"][0]
    assert s["headline"] == "Fed holds rates" and len(s["links"]) == 5
    assert 'lean="lean-left"' in captured["prompt"] and "<investigation" in captured["prompt"]
    assert "Fed holds rates" in render_markdown(d)


def test_ai_failure_falls_back_to_basic(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    def boom(prompt, config):
        raise RuntimeError("network down")

    monkeypatch.setattr(summ, "summarize_with_claude", boom)
    d = run(cfg, fixtures=FIXTURES, now=NOW, dry_run=True)
    assert d["mode"] == "basic" and d["stories"]
