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
TEST_CONFIG = Path(__file__).parent / "config"  # frozen copy, so editing your real topics never breaks tests
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def cfg():
    return load_config(TEST_CONFIG)


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


def test_topic_sources_and_slots(cfg, articles):
    import copy
    from digest.config import Topic
    c = copy.copy(cfg)
    c.topics = [Topic(name="Space", keywords=[], sources=["nasa", "arstechnica"], slots=2),
                Topic(name="Econ", keywords=["federal reserve"])]
    c.settings = {**cfg.settings, "max_stories": 3}
    tag_topics(articles, c)
    stories = cluster(articles, 0.28)
    score_stories(stories, c, NOW)
    chosen = select_stories(stories, c)
    assert any(a.source_id == "arstechnica" and "Space" in a.topics for a in articles)
    assert sum("Space" in s.topics for s in chosen) >= 1 and any("Econ" in s.topics for s in chosen)


def test_api_key_is_extracted_from_pasted_text():
    k = "sk-ant-usr01-AbC_123-xyz"
    assert summ.clean_api_key(k) == k
    assert summ.clean_api_key(f'  export ANTHROPIC_API_KEY="{k}"\n') == k
    assert summ.clean_api_key(f"'{k}'") == k
    assert summ.clean_api_key(None) is None


def test_one_passing_mention_does_not_tag_a_big_story(cfg, articles):
    import copy
    from digest.config import Topic
    c = copy.copy(cfg)
    c.topics = [Topic(name="Local", keywords=[], sources=["npr"])]  # 1 of the Fed story's 5 outlets
    tag_topics(articles, c)
    stories = cluster(articles, 0.28)
    score_stories(stories, c, NOW)
    fed = _find(stories, "federal reserve")
    assert "Local" not in fed.topics


# ---------- The Lighter Brief (profiles/lighter) ----------

def _write_feeds(folder, items):
    from email.utils import format_datetime
    from xml.sax.saxutils import escape
    folder.mkdir(parents=True, exist_ok=True)
    by_src = {}
    for sid, title, summary in items:
        by_src.setdefault(sid, []).append((title, summary))
    for sid, entries in by_src.items():
        body = "".join(
            f"<item><title>{escape(t)}</title><link>https://example.com/{sid}/{n}</link>"
            f"<description>{escape(d)}</description><pubDate>{format_datetime(NOW)}</pubDate></item>"
            for n, (t, d) in enumerate(entries))
        (folder / f"{sid}.xml").write_text(f'<?xml version="1.0"?><rss version="2.0"><channel><title>{sid}</title>{body}</channel></rss>')


LIGHT_ITEMS = [  # SAMPLE DATA, made up for tests
    ("gleaner", "Kingston hosts Caribbean food festival this weekend", "Vendors from across Jamaica set up in Kingston."),
    ("observer", "Caribbean food festival opens in Kingston", "The Jamaica food festival expects record crowds."),
    ("xxl", "Rapper Latto announces new album and tour dates", "Latto shared the album title on Instagram."),
    ("vibe", "Latto reveals new album, tour dates", "The rapper Latto will tour in the spring."),
    ("goodnews", "Rescue puppies reunited with the firefighters who saved them", "The puppies were adopted by local families."),
    ("medicalxpress", "FDA approves new once-a-month migraine treatment", "The FDA approval follows a large clinical trial."),
    ("biopharmadive", "Migraine treatment wins FDA approval after clinical trial", "Drugmaker says the treatment ships in January."),
    ("wbtv", "Charlotte opens new greenway section in Mecklenburg County", "The greenway connects two Charlotte parks."),
    ("qcnews", "New greenway section opens in Charlotte", "Mecklenburg County parks staff cut the ribbon."),
    ("bbc-us", "Gunman kills four in mall shooting", "Police said the shooting happened Tuesday."),
    ("axios", "Mall shooting: gunman kills four", "Officials are investigating the shooting."),
    ("bbc-us", "Senate passes bill on airport security lines", "The Senate vote sends the bill to the president."),
    ("axios", "Senate approves airport security lines bill", "The president is expected to sign the Senate bill."),
]


@pytest.fixture
def light_cfg():
    from digest.config import profile_dir
    return load_config(profile_dir("lighter"), "lighter")


def test_lighter_profile_loads_with_its_own_paths(light_cfg):
    assert light_cfg.style == "light" and not light_cfg.is_main
    assert light_cfg.archive_dir.name == "archive-lighter" and light_cfg.latest_file.name == "LATEST-lighter.md"
    assert {"Jamaica", "Charlotte", "Hip Hop & Celebrity", "Cute Animals", "Health & Medicine"} <= {t.name for t in light_cfg.topics}
    assert {s.bucket for s in light_cfg.sources} <= {"center", "left"}  # mostly center outlets


def test_light_schema_drops_perspectives_and_adds_too_heavy():
    story = summ.build_schema("light")["properties"]["stories"]["items"]
    assert "perspectives" not in story["properties"] and "too_heavy" in story["required"]
    assert set(story["required"]) == set(story["properties"])
    assert "perspectives" in summ.SCHEMA["properties"]["stories"]["items"]["properties"]  # main schema untouched


def test_lighter_basic_mode_mutes_heavy_news(light_cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    feeds = tmp_path / "feeds"
    _write_feeds(feeds, LIGHT_ITEMS)
    d = run(light_cfg, fixtures=feeds, now=NOW, dry_run=True)
    heads = " ".join(s["headline"] for s in d["stories"]).lower()
    assert "shooting" not in heads and "gunman" not in heads
    topics = {t for s in d["stories"] for t in s["topics"]}
    assert {"Jamaica", "Charlotte", "Hip Hop & Celebrity", "Cute Animals", "Health & Medicine"} <= topics
    assert all(s["perspectives"] == [] for s in d["stories"])
    md = render_markdown(d)
    assert "left · " not in md and "(center)" not in md and "L 0" not in md


def test_lighter_drops_stories_claude_flags_as_too_heavy(light_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    feeds = tmp_path / "feeds"
    _write_feeds(feeds, LIGHT_ITEMS)
    seen = {}

    def fake_claude(prompt, config):
        import re as _re
        ids = _re.findall(r'<story id="(s\d+)"', prompt)
        seen["ids"] = ids
        return {"overview": "Easy day.", "investigations": [], "stories": [
            {"story_id": sid, "headline": f"Story {sid}", "summary": "x", "key_facts": [],
             "disputed_or_unclear": [], "why_it_matters": "", "too_heavy": sid == "s1"} for sid in ids]}

    monkeypatch.setattr(summ, "summarize_with_claude", fake_claude)
    d = run(light_cfg, fixtures=feeds, now=NOW, dry_run=True)
    assert "Story s1" not in [s["headline"] for s in d["stories"]]
    assert len(d["stories"]) == min(len(seen["ids"]) - 1, light_cfg.get("max_stories"))


def test_lighter_site_goes_in_its_own_folder(light_cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    feeds = tmp_path / "feeds"
    _write_feeds(feeds, LIGHT_ITEMS)
    run(light_cfg, fixtures=feeds, now=NOW, archive_dir=tmp_path / "arch", state_file=tmp_path / "s.json")
    n = build_site(tmp_path / "site" / "lighter", "The Lighter Brief", archive_dir=tmp_path / "arch", style="light")
    html = (tmp_path / "site" / "lighter" / "index.html").read_text()
    assert n == 1 and "The Lighter Brief" in html and "How it's being framed" not in html
    assert "political spectrum" not in html
