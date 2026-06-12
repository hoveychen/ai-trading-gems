#!/usr/bin/env python3
"""Build the browser dataset: data.json (post index) + threads/<id>.json (full threads).

Inputs:  posts.jsonl, comments.jsonl, candidates.jsonl, classifications.jsonl
Outputs: data.json          — candidate/classified posts with preview, newest first
         filtered.json      — prefilter-rejected posts, minimal fields (the UI
                              lazy-loads this only when the filtered chip is on)
         threads/<id>.json  — full post body + nested comment tree, one file per
                              prefilter-passed post (fetched on demand by the UI)

Post status: "filtered"  failed prefilter (still listed, de-emphasized)
             "pending"   passed prefilter, awaiting local LLM classification
             "gem"/"ok"/"hype"  LLM verdicts from classifications.jsonl
"""
import json
import os
import re
import shutil
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS = os.path.join(HERE, "posts.jsonl")
COMMENTS = os.path.join(HERE, "comments.jsonl")
CANDIDATES = os.path.join(HERE, "candidates.jsonl")
CLASSIFICATIONS = os.path.join(HERE, "classifications.jsonl")
DATA_OUT = os.path.join(HERE, "data.json")
FILTERED_OUT = os.path.join(HERE, "filtered.json")
THREADS_DIR = os.path.join(HERE, "threads")

PREVIEW_LEN = 400
PREVIEW_LEN_FILTERED = 160   # filtered posts are ~95% of the index; keep them light
EMPTY = {"", "[removed]", "[deleted]"}
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
URL_RE = re.compile(r"https?://\S+")


def preview_text(body):
    """Plain-text preview: markdown links -> text, raw URLs dropped, md noise out."""
    t = MD_LINK_RE.sub(r"\1", body)
    t = URL_RE.sub("", t)
    t = re.sub(r"[#*`>|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def load_jsonl_by_id(path):
    d = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    d[obj["id"]] = obj
                except (json.JSONDecodeError, KeyError):
                    continue
    return d


def build_comment_tree(comments, post_author):
    """comments (flat, ascending) -> list of nested root comments."""
    nodes = {}
    for c in comments:
        nodes[c["id"]] = {
            "id": c["id"],
            "author": c.get("author") or "[deleted]",
            "body": c.get("body") or "",
            "score": c.get("score") or 0,
            "created_utc": c.get("created_utc") or 0,
            "is_op": (c.get("author") or "") == post_author and post_author != "",
            "replies": [],
        }
    roots = []
    for c in comments:
        node = nodes[c["id"]]
        parent = (c.get("parent_id") or "")
        if parent.startswith("t1_") and parent[3:] in nodes:
            nodes[parent[3:]]["replies"].append(node)
        else:
            roots.append(node)
    return roots


def main():
    posts = load_jsonl_by_id(POSTS)              # dedup by id, last wins
    candidates = load_jsonl_by_id(CANDIDATES)
    classifications = load_jsonl_by_id(CLASSIFICATIONS)

    comments_by_post = defaultdict(list)
    if os.path.exists(COMMENTS):
        seen = set()
        with open(COMMENTS) as f:
            for line in f:
                c = json.loads(line)
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                pid = (c.get("link_id") or "")[3:]
                if pid in posts:
                    comments_by_post[pid].append(c)

    if os.path.isdir(THREADS_DIR):
        shutil.rmtree(THREADS_DIR)
    os.makedirs(THREADS_DIR)

    index = []
    filtered = []
    n_threads = 0
    status_counts = defaultdict(int)
    for pid, p in posts.items():
        cand = candidates.get(pid, {})
        cls = classifications.get(pid)
        is_candidate = bool(cand.get("candidate"))

        if cls:
            status = cls["verdict"]
        elif is_candidate:
            status = "pending"
        else:
            status = "filtered"
        status_counts[status] += 1

        body = (p.get("selftext") or "").strip()
        if body in EMPTY:
            body = ""
        body = preview_text(body)
        signals = cand.get("signals", {})

        if not is_candidate and not cls:
            filtered.append({
                "id": pid,
                "title": p.get("title") or "",
                "author": p.get("author") or "[deleted]",
                "created_utc": p.get("created_utc") or 0,
                "score": p.get("score") or 0,
                "num_comments": p.get("num_comments") or 0,
                "preview": body[:PREVIEW_LEN_FILTERED],
                "status": "filtered",
                "author_followups": 0,
            })
            continue

        entry = {
            "id": pid,
            "title": p.get("title") or "",
            "author": p.get("author") or "[deleted]",
            "created_utc": p.get("created_utc") or 0,
            "score": p.get("score") or 0,
            "num_comments": p.get("num_comments") or 0,
            "preview": body[:PREVIEW_LEN],
            "body_len": len(body),
            "status": status,
            "flair": p.get("link_flair_text") or "",
            "url": "" if (p.get("is_self") or not p.get("url")) else p["url"],
            "author_followups": signals.get("author_followup_count", 0),
        }
        if cls:
            entry["tags"] = cls.get("tags", [])
            entry["summary_zh"] = cls.get("summary_zh", "")
            entry["confidence"] = cls.get("confidence", 0.5)
        index.append(entry)

        if is_candidate:
            comments = sorted(comments_by_post.get(pid, []),
                              key=lambda c: c.get("created_utc") or 0)
            thread = {
                "id": pid,
                "title": p.get("title") or "",
                "author": p.get("author") or "[deleted]",
                "created_utc": p.get("created_utc") or 0,
                "score": p.get("score") or 0,
                "selftext": (p.get("selftext") or ""),
                "url": p.get("url") or "",
                "comments": build_comment_tree(comments, p.get("author") or ""),
            }
            with open(os.path.join(THREADS_DIR, f"{pid}.json"), "w") as f:
                json.dump(thread, f, ensure_ascii=False, separators=(",", ":"))
            n_threads += 1

    index.sort(key=lambda e: -e["created_utc"])
    filtered.sort(key=lambda e: -e["created_utc"])
    import datetime as dt
    payload = {
        "subreddit": "ai_trading",
        "built_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "counts": dict(status_counts),
        "posts": index,
    }
    with open(DATA_OUT, "w") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    with open(FILTERED_OUT, "w") as f:
        json.dump(filtered, f, ensure_ascii=False, separators=(",", ":"))

    print(f"data.json: {len(index)} posts, {os.path.getsize(DATA_OUT) / 1e6:.1f} MB")
    print(f"filtered.json: {len(filtered)} posts, "
          f"{os.path.getsize(FILTERED_OUT) / 1e6:.1f} MB")
    print(f"threads/: {n_threads} files")
    print("status:", dict(status_counts))


if __name__ == "__main__":
    main()
