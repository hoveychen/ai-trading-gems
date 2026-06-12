#!/usr/bin/env python3
"""Heuristic prefilter: drop obviously-empty posts so the LLM only reads candidates.

Reads posts.jsonl + comments.jsonl, writes candidates.jsonl with one line per post:
    {"id": ..., "candidate": true/false, "reasons": [...], "signals": {...}}

Philosophy: this layer only kills posts that clearly carry no recoverable substance
(no body, no author follow-up, no discussion). Judging "real insight vs hype" is the
LLM's job in classify.py — when in doubt, pass it through.
"""
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS = os.path.join(HERE, "posts.jsonl")
COMMENTS = os.path.join(HERE, "comments.jsonl")
OUT = os.path.join(HERE, "candidates.jsonl")

EMPTY_BODIES = {"", "[removed]", "[deleted]"}
PROMO_RE = re.compile(
    r"(discord\.gg|t\.me/|telegram|dm me|join my (free )?(group|channel|server)"
    r"|link in bio|whatsapp)", re.I)

# Thresholds — tune freely; reasons are recorded per post so effects are auditable.
MIN_BODY = 300            # chars of selftext that count as a substantive body
MIN_AUTHOR_FOLLOWUP = 300  # chars the author wrote in their own comment section
MIN_SCORE = 20
MIN_COMMENTS = 15


def load_author_followups():
    """post id -> total chars of comments the post author wrote under their post."""
    post_author = {}
    with open(POSTS) as f:
        for line in f:
            p = json.loads(line)
            post_author[p["id"]] = p.get("author") or ""

    followup = defaultdict(int)
    followup_count = defaultdict(int)
    if os.path.exists(COMMENTS):
        with open(COMMENTS) as f:
            for line in f:
                c = json.loads(line)
                pid = (c.get("link_id") or "")[3:]  # strip t3_
                author = c.get("author") or ""
                if not pid or author in ("[deleted]", "AutoModerator"):
                    continue
                if post_author.get(pid) == author:
                    body = c.get("body") or ""
                    if body not in EMPTY_BODIES:
                        followup[pid] += len(body)
                        followup_count[pid] += 1
    return followup, followup_count


def evaluate(p, followup_chars, followup_count):
    body = (p.get("selftext") or "").strip()
    body_len = 0 if body in EMPTY_BODIES else len(body)
    score = p.get("score") or 0
    n_comments = p.get("num_comments") or 0
    title = p.get("title") or ""

    signals = {
        "body_len": body_len,
        "author_followup_chars": followup_chars,
        "author_followup_count": followup_count,
        "score": score,
        "num_comments": n_comments,
    }
    reasons = []

    if PROMO_RE.search(title + " " + body[:2000]) and body_len < MIN_BODY \
            and followup_chars < MIN_AUTHOR_FOLLOWUP:
        return False, ["promo_link_no_substance"], signals

    if body_len >= MIN_BODY:
        reasons.append(f"body>={MIN_BODY}")
    if followup_chars >= MIN_AUTHOR_FOLLOWUP:
        reasons.append(f"author_followup>={MIN_AUTHOR_FOLLOWUP}")
    if score >= MIN_SCORE:
        reasons.append(f"score>={MIN_SCORE}")
    if n_comments >= MIN_COMMENTS:
        reasons.append(f"comments>={MIN_COMMENTS}")

    return bool(reasons), reasons or ["no_substance_signal"], signals


def main():
    if not os.path.exists(POSTS):
        sys.exit("posts.jsonl not found — run scrape.py first")
    followup, followup_count = load_author_followups()

    total = kept = 0
    with open(POSTS) as f, open(OUT, "w") as out:
        for line in f:
            p = json.loads(line)
            pid = p["id"]
            candidate, reasons, signals = evaluate(
                p, followup.get(pid, 0), followup_count.get(pid, 0))
            total += 1
            kept += candidate
            out.write(json.dumps({
                "id": pid, "candidate": candidate,
                "reasons": reasons, "signals": signals,
            }, ensure_ascii=False) + "\n")

    print(f"{kept}/{total} posts pass prefilter ({kept * 100 // max(total, 1)}%)")


if __name__ == "__main__":
    main()
