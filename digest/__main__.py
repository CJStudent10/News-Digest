"""Command line for the digest.

    python -m digest run                       # build an edition (uses Claude if ANTHROPIC_API_KEY is set)
    python -m digest run --no-llm --dry-run    # quick free test, prints Markdown, saves nothing
    python -m digest site                      # build the static website into _site/
    python -m digest sources check             # test every feed
    python -m digest topics list | add | remove
    python -m digest investigate "question" --keywords "a, b"
    python -m digest investigations list | close <id>
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path

from .config import CONFIG_DIR, ROOT, load_config, save_yaml, _load


def _split(s: str | None) -> list[str]:
    return [k.strip() for k in (s or "").split(",") if k.strip()]


def cmd_run(args) -> int:
    from .pipeline import run
    from .render import render_markdown

    cfg = load_config()
    d = run(cfg, use_llm=not args.no_llm, fixtures=Path(args.fixtures) if args.fixtures else None,
            dry_run=args.dry_run)
    if args.dry_run:
        print(render_markdown(d))
    else:
        print(f"Saved {d['edition']} edition ({len(d['stories'])} stories, mode={d['mode']}) → archive/…/{d['slug']}")
    return 0


def cmd_site(args) -> int:
    from .render import build_site

    cfg = load_config()
    n = build_site(Path(args.out), cfg.get("site_title", "My Daily Brief"), limit=int(cfg.get("site_archive_size", 60)))
    print(f"Built site with {n} digest(s) in {args.out}/")
    return 0


def cmd_sources_check(args) -> int:
    from .fetch import fetch_all

    cfg = load_config()
    ok = 0
    for src in cfg.sources:
        arts, failed = fetch_all([src], lookback_hours=48)
        status = f"FAIL  {failed[src.id]}" if failed else f"ok    {len(arts)} articles (48h)"
        ok += not failed
        print(f"{src.name:<28} {src.lean:<11} {status}")
    print(f"\n{ok}/{len(cfg.sources)} feeds working")
    return 0 if ok else 1


def cmd_topics(args) -> int:
    data = _load("topics.yaml", CONFIG_DIR)
    topics = data.setdefault("topics", [])
    if args.action == "list":
        for t in topics:
            print(f"- {t['name']} (weight {t.get('weight', 1.0)}): {', '.join(t.get('keywords', []))}")
        print(f"\nmuted: {', '.join(data.get('mute', []) or []) or '(none)'}")
        return 0
    if args.action == "add":
        if not args.name or not args.keywords:
            sys.exit("topics add needs a NAME and --keywords")
        topics[:] = [t for t in topics if t["name"].lower() != args.name.lower()]
        entry = {"name": args.name, "keywords": _split(args.keywords), "weight": args.weight}
        if args.exclude:
            entry["exclude"] = _split(args.exclude)
        topics.append(entry)
        print(f"Saved topic {args.name!r}")
    elif args.action == "remove":
        before = len(topics)
        topics[:] = [t for t in topics if t["name"].lower() != (args.name or "").lower()]
        print("Removed." if len(topics) < before else f"No topic named {args.name!r}")
    elif args.action == "mute":
        data["mute"] = sorted(set((data.get("mute") or []) + _split(args.name)))
        print(f"Muted: {', '.join(data['mute'])}")
    save_yaml("topics.yaml", data)
    return 0


def cmd_investigate(args) -> int:
    data = _load("investigations.yaml", CONFIG_DIR)
    items = data.setdefault("investigations", []) or []
    data["investigations"] = items
    slug = re.sub(r"[^a-z0-9]+", "-", args.question.lower()).strip("-")[:50]
    keywords = _split(args.keywords)
    if not keywords:
        sys.exit("Please pass --keywords, e.g. --keywords \"federal reserve, rate cut\"")
    items[:] = [i for i in items if i["id"] != slug]
    items.append({"id": slug, "question": args.question, "keywords": keywords, "opened": date.today().isoformat()})
    save_yaml("investigations.yaml", data)
    print(f"Following {args.question!r} (id: {slug})")
    return 0


def cmd_investigations(args) -> int:
    data = _load("investigations.yaml", CONFIG_DIR)
    items = data.get("investigations", []) or []
    if args.action == "list":
        for i in items:
            print(f"- [{i['id']}] {i['question']}  ({', '.join(i['keywords'])})")
        return 0
    data["investigations"] = [i for i in items if i["id"] != args.id]
    save_yaml("investigations.yaml", data)
    print("Closed." if len(data["investigations"]) < len(items) else f"No investigation with id {args.id!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m digest", description="Balanced personal news digest")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="build a digest edition")
    r.add_argument("--no-llm", action="store_true", help="skip Claude; use outlet blurbs (free)")
    r.add_argument("--dry-run", action="store_true", help="print the digest; don't save or notify")
    r.add_argument("--fixtures", help="read feeds from <dir>/<source_id>.xml instead of the internet")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("site", help="build the static website")
    s.add_argument("--out", default=str(ROOT / "_site"))
    s.set_defaults(fn=cmd_site)

    src = sub.add_parser("sources", help="feed tools")
    src.add_argument("action", choices=["check"])
    src.set_defaults(fn=cmd_sources_check)

    t = sub.add_parser("topics", help="list/add/remove/mute topics")
    t.add_argument("action", choices=["list", "add", "remove", "mute"])
    t.add_argument("name", nargs="?", help="topic name (or comma-separated words for mute)")
    t.add_argument("--keywords")
    t.add_argument("--exclude")
    t.add_argument("--weight", type=float, default=1.0)
    t.set_defaults(fn=cmd_topics)

    i = sub.add_parser("investigate", help="start following a question")
    i.add_argument("question")
    i.add_argument("--keywords", required=True)
    i.set_defaults(fn=cmd_investigate)

    il = sub.add_parser("investigations", help="list or close investigations")
    il.add_argument("action", choices=["list", "close"])
    il.add_argument("id", nargs="?")
    il.set_defaults(fn=cmd_investigations)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
