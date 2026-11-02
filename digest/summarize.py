"""Write the briefs: Claude when an API key is set, basic mode otherwise.

Claude only writes prose. Links, outlet names and coverage counts always come
from the fetched data, so the model can't make up a source.
"""
from __future__ import annotations

import json
import logging
import os
import re
from xml.sax.saxutils import escape, quoteattr

from .config import Config, Investigation
from .fetch import Article
from .stories import Story

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the editor of a personal news brief. The reader wants to understand each story quickly and fairly, \
and to make up their own mind.

For every story you receive articles from outlets labelled with their political lean (left, lean-left, center, \
lean-right, right). Use only the provided articles; don't add facts from memory. If the articles disagree, say so \
instead of choosing a side.

For each story:
- headline: a plain, neutral headline. No loaded words, nothing the articles don't support.
- summary: 3-5 sentences on what happened, who is involved, and what happens next. Attribute contested claims \
("the White House said...", "critics argue...") rather than stating them as fact.
- key_facts: 2-5 short facts that the articles agree on or that are on the record (numbers, dates, votes, quotes).
- perspectives: one entry for each side of the spectrum (left, center, right) that covered it, describing how \
those outlets framed the story: what they emphasised, which voices they quoted, what they left out. Describe the \
framing; don't judge it. Skip a side that has no articles. If outlets on different sides framed it the same way, \
say that.
- disputed_or_unclear: claims that are contested, unverified, or still developing. Use an empty list if there are none.
- why_it_matters: one sentence on why a reader might care.

overview: 2-3 sentences that sum up this edition in a neutral voice.

For each investigation, report what the matching articles add on the reader's question. Use status \
"no_new_developments" and a brief note if they don't address it.

Write in plain, clear English for a smart general reader. Don't include URLs; links are added separately."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overview", "stories", "investigations"],
    "properties": {
        "overview": {"type": "string"},
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["story_id", "headline", "summary", "key_facts", "perspectives",
                             "disputed_or_unclear", "why_it_matters"],
                "properties": {
                    "story_id": {"type": "string"},
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "key_facts": {"type": "array", "items": {"type": "string"}},
                    "perspectives": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["lean", "framing"],
                            "properties": {
                                "lean": {"type": "string", "enum": ["left", "center", "right"]},
                                "framing": {"type": "string"},
                            },
                        },
                    },
                    "disputed_or_unclear": {"type": "array", "items": {"type": "string"}},
                    "why_it_matters": {"type": "string"},
                },
            },
        },
        "investigations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "status", "update"],
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": ["new_developments", "no_new_developments"]},
                    "update": {"type": "string"},
                },
            },
        },
    },
}


def _article_xml(a: Article, with_text: bool = True) -> str:
    body = a.text if (with_text and a.text) else a.summary
    when = a.published.isoformat() if a.published else "unknown"
    return (f"  <article source={quoteattr(a.source_name)} lean={quoteattr(a.lean)} published={quoteattr(when)}>\n"
            f"    <title>{escape(a.title)}</title>\n    <text>{escape(body)}</text>\n  </article>")


def build_prompt(stories: list[tuple[Story, list[Article]]],
                 investigations: list[tuple[Investigation, list[Article]]]) -> str:
    parts = ["<stories>"]
    for story, arts in stories:
        cov = ", ".join(f"{k}: {v}" for k, v in story.coverage.items())
        parts.append(f"<story id={quoteattr(story.id)} topics={quoteattr(', '.join(story.topics) or 'general')} "
                     f"outlets_covering={quoteattr(cov)}>")
        parts.extend(_article_xml(a) for a in arts)
        parts.append("</story>")
    parts.append("</stories>\n\n<investigations>")
    for inv, arts in investigations:
        parts.append(f"<investigation id={quoteattr(inv.id)} question={quoteattr(inv.question)}>")
        if arts:
            parts.extend(_article_xml(a, with_text=False) for a in arts)
        else:
            parts.append("  (no matching articles)")
        parts.append("</investigation>")
    parts.append("</investigations>\n\nWrite this edition's brief.")
    return "\n".join(parts)


def clean_api_key(raw: str | None) -> str | None:
    """Pull the key out of whatever was pasted into the secret.

    The Console shows keys inside an example command (export ANTHROPIC_API_KEY="sk-ant-...")
    and it's easy to paste the whole line, quotes or stray spaces included.
    """
    if not raw:
        return None
    m = re.search(r"sk-ant-[A-Za-z0-9_\-]+", raw)
    if not m:
        log.warning("ANTHROPIC_API_KEY doesn't contain an Anthropic key (they start with 'sk-ant-'). "
                    "Create one at https://console.anthropic.com/settings/keys")
        return raw.strip()
    if m.group(0) != raw.strip():
        log.warning("ANTHROPIC_API_KEY had extra text around the key; using just the key. "
                    "Consider re-saving the secret with only the sk-ant-... part.")
    return m.group(0)


def summarize_with_claude(prompt: str, config: Config) -> dict:
    import anthropic

    llm = config.get("llm", {}) or {}
    key = clean_api_key(os.environ.get("ANTHROPIC_API_KEY"))
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    with client.beta.messages.stream(
        model=llm.get("model", "claude-opus-5-5"),
        max_tokens=int(llm.get("max_tokens", 32000)),
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        thinking={"type": "adaptive"},
        output_config={"effort": llm.get("effort", "medium"),
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        # If a safety classifier declines (rare for news, but possible on violent
        # stories), the API retries on a suitable fallback model instead of failing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        msg = stream.get_final_message()

    u = msg.usage
    log.info("Claude (%s) finished: stop=%s, input=%s, output=%s tokens", msg.model, msg.stop_reason,
             u.input_tokens, u.output_tokens)
    if msg.stop_reason == "refusal":
        raise RuntimeError("the request was declined by the model")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("the response hit max_tokens; raise llm.max_tokens in settings.yaml")
    text = next(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def summarize_basic(stories: list[tuple[Story, list[Article]]],
                    investigations: list[tuple[Investigation, list[Article]]]) -> dict:
    """No API key: use the outlets' own blurbs, attributed, so the digest still works for free."""
    out_stories = []
    for story, arts in stories:
        lead = max(arts, key=lambda a: len(a.summary))
        perspectives = []
        for bucket in ("left", "center", "right"):
            side = [a for a in arts if a.bucket == bucket]
            if side:
                perspectives.append({"lean": bucket, "framing": " · ".join(f"{a.source_name}: “{a.title}”" for a in side)})
        out_stories.append({
            "story_id": story.id,
            "headline": lead.title,
            "summary": f"{lead.summary} ({lead.source_name})" if lead.summary else "",
            "key_facts": [],
            "perspectives": perspectives,
            "disputed_or_unclear": [],
            "why_it_matters": "",
        })
    out_inv = [{
        "id": inv.id,
        "status": "new_developments" if arts else "no_new_developments",
        "update": (f"{len(arts)} matching article(s), latest: “{arts[0].title}” ({arts[0].source_name})"
                   if arts else "No matching coverage this edition."),
    } for inv, arts in investigations]
    return {"overview": "", "stories": out_stories, "investigations": out_inv}


def summarize(stories, investigations, config: Config, use_llm: bool = True) -> tuple[dict, str]:
    """Returns (result, mode) where mode is 'ai' or 'basic'."""
    llm_on = use_llm and (config.get("llm", {}) or {}).get("enabled", True)
    if llm_on and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        try:
            return summarize_with_claude(build_prompt(stories, investigations), config), "ai"
        except Exception as exc:
            hint = ""
            if getattr(exc, "status_code", None) == 401:
                hint = (" -> The API key was rejected. Use a key from https://console.anthropic.com/settings/keys "
                        "(it starts with sk-ant-), saved as the repository secret ANTHROPIC_API_KEY. Check the key "
                        "is active and your Console organization has credits under Settings -> Billing.")
            log.error("AI summary failed, using basic mode instead: %s%s", exc, hint)
    elif llm_on:
        log.info("No ANTHROPIC_API_KEY set; running in basic mode")
    return summarize_basic(stories, investigations), "basic"
