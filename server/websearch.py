#!/usr/bin/env python3
"""Hub-native free web search (stdlib only, no keys in code).

Primary: Brave Search API free tier (2,000 queries/month, no card) when
BRAVE_SEARCH_API_KEY is set.
Fallback: DuckDuckGo Instant Answer API — no key needed, used when no
Brave key is configured.

Returns a list of {title, url, snippet}. Used by the Sonar adapter for
free search grounding, and exposed directly as POST /v1/web_search.
"""
import json
import os
import urllib.parse
import urllib.request
import urllib.error

UA = "medic-model-hub/1.2.2"


def brave_key():
    return os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()


def source():
    """Which backend will be used right now: 'brave' or 'duckduckgo'."""
    return "brave" if brave_key() else "duckduckgo"


def _get(url, headers, timeout=15):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": f"search HTTP {e.code}"}
    except Exception as e:  # network / timeout
        return 0, {"error": f"search unreachable: {type(e).__name__}"}


def _brave(query, count):
    """(status, results, err) via Brave Search API."""
    qs = urllib.parse.urlencode({"q": query, "count": max(1, min(count, 10))})
    status, body = _get("https://api.search.brave.com/res/v1/web/search?" + qs,
                        {"X-Subscription-Token": brave_key(),
                         "Accept": "application/json", "User-Agent": UA})
    if status != 200:
        return status, None, {"provider_error": body}
    out = []
    for r in ((body.get("web") or {}).get("results") or [])[:count]:
        out.append({"title": r.get("title", "") or "",
                    "url": r.get("url", "") or "",
                    "snippet": r.get("description", "") or ""})
    return 200, out, None


def _ddg_flatten(topics, acc):
    for t in topics or []:
        if "Topics" in t:  # category group
            _ddg_flatten(t["Topics"], acc)
        elif t.get("FirstURL"):
            acc.append(t)


def _ddg(query, count):
    """(status, results, err) via DuckDuckGo Instant Answer API (keyless)."""
    qs = urllib.parse.urlencode({"q": query, "format": "json",
                                 "no_html": "1", "skip_disambig": "1"})
    status, body = _get("https://api.duckduckgo.com/?" + qs,
                        {"Accept": "application/json", "User-Agent": UA})
    if status != 200:
        return status, None, {"provider_error": body}
    flat = []
    _ddg_flatten(body.get("RelatedTopics"), flat)
    out = []
    for t in flat[:count]:
        text = (t.get("Text") or "").strip()
        out.append({"title": text[:120], "url": t.get("FirstURL", ""),
                    "snippet": text[:400]})
    return 200, out, None


def web_search(query, count=5):
    """(status, results, err). Results: [{title, url, snippet}]."""
    if not isinstance(query, str) or not query.strip():
        return 400, None, {"error": "need a non-empty 'query'"}
    count = max(1, min(int(count or 5), 10))
    if brave_key():
        return _brave(query, count)
    return _ddg(query, count)
