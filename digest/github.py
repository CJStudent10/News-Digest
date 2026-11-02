"""Use GitHub issues labelled `investigate` as a phone-friendly way to follow a question."""
from __future__ import annotations

import json
import logging
import re
import urllib.request

from .config import Investigation
from .stories import tokenize

log = logging.getLogger(__name__)
API = "https://api.github.com"
LABEL = "investigate"


def _request(method: str, path: str, token: str, body: dict | None = None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "news-digest",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read() or b"null")


def parse_keywords(body: str, title: str) -> list[str]:
    """Keywords from the issue form's 'Keywords' field, or a 'keywords:' line, or the title."""
    body = body or ""
    m = re.search(r"###\s*Keywords\s*\n+(.+?)(?=\n###|\Z)", body, re.S | re.I) or \
        re.search(r"^\s*keywords?\s*:\s*(.+)$", body, re.M | re.I)
    if m and m.group(1).strip() and m.group(1).strip() != "_No response_":
        return [k.strip() for k in re.split(r"[,\n]", m.group(1)) if k.strip()]
    # No keywords given: fall back to the distinctive words in the title.
    return [w for w in tokenize(title) if len(w) >= 5][:6]


def issue_investigations(repo: str, token: str) -> list[Investigation]:
    try:
        issues = _request("GET", f"/repos/{repo}/issues?labels={LABEL}&state=open&per_page=50", token)
    except Exception as exc:
        log.warning("couldn't read investigate issues: %s", exc)
        return []
    out = []
    for i in issues or []:
        if "pull_request" in i:
            continue
        out.append(Investigation(
            id=f"issue-{i['number']}",
            question=i["title"],
            keywords=parse_keywords(i.get("body") or "", i["title"]),
            opened=(i.get("created_at") or "")[:10],
            issue_number=i["number"],
        ))
    return out


def comment_on_issue(repo: str, token: str, number: int, body: str) -> None:
    try:
        _request("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
    except Exception as exc:
        log.warning("couldn't comment on issue #%s: %s", number, exc)
