#!/usr/bin/env python3
"""Classify candidate posts with a local `claude -p` call (LOCAL-ONLY step).

Separates substantive ideas/practices ("gem") from hype/promo ("hype"), with "ok"
in between. Resumable: results append to classifications.jsonl keyed by post id;
already-classified posts are skipped, so daily incremental runs are just:

    python3 scrape.py && python3 prefilter.py && python3 classify.py

Options:
    --limit N      classify at most N posts this run (pilot batches)
    --model M      claude model alias (default: leave to claude CLI default)
    --batch N      posts per claude call (default 12)
    --workers N    concurrent claude calls (default 4)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS = os.path.join(HERE, "posts.jsonl")
COMMENTS = os.path.join(HERE, "comments.jsonl")
CANDIDATES = os.path.join(HERE, "candidates.jsonl")
OUT = os.path.join(HERE, "classifications.jsonl")
ERRLOG = os.path.join(HERE, "classify_errors.log")

VERDICTS = {"gem", "ok", "hype"}
TAGS = {"strategy", "backtest", "results", "tooling", "lesson",
        "question", "discussion", "news", "promo"}

PROMPT_HEADER = """\
You are vetting posts from r/ai_trading. The reader wants REAL ideas and practices \
for AI-assisted trading (concrete strategies, backtests with numbers, tooling \
write-ups, honest post-mortems) and wants to skip empty hype (vague success claims \
with no method, engagement bait, course/discord/bot promotion).

For EACH post below, judge mainly: is there a concrete, reusable idea or practice, \
or verifiable detail (numbers, code, method)? Author follow-up comments count as \
part of the content.

Return ONLY a JSON array, one object per post, no other text:
[{"id": "<id>", "verdict": "gem|ok|hype", "tags": ["strategy|backtest|results|tooling|lesson|question|discussion|news|promo", ...], "summary_zh": "一句话中文摘要，说清这帖有什么（或为什么没料）", "confidence": 0.0-1.0}]

verdict guide: gem = concrete reusable substance; ok = some content but thin or \
unverifiable; hype = promotion, vague bragging, or no substance.

POSTS:
"""


def clip(s, n):
    s = (s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


def load_inputs(limit):
    candidates = set()
    with open(CANDIDATES) as f:
        for line in f:
            c = json.loads(line)
            if c["candidate"]:
                candidates.add(c["id"])

    done = set()
    if os.path.exists(OUT):
        with open(OUT) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    continue

    todo_ids = candidates - done

    posts, authors = [], {}
    with open(POSTS) as f:
        for line in f:
            p = json.loads(line)
            if p["id"] in todo_ids:
                posts.append(p)
                authors[p["id"]] = p.get("author") or ""

    posts.sort(key=lambda p: -(p.get("score") or 0))  # most-voted first
    if limit:
        posts = posts[:limit]
        keep = {p["id"] for p in posts}
        authors = {k: v for k, v in authors.items() if k in keep}

    followups = defaultdict(list)
    if os.path.exists(COMMENTS) and posts:
        keep = {p["id"] for p in posts}
        with open(COMMENTS) as f:
            for line in f:
                c = json.loads(line)
                pid = (c.get("link_id") or "")[3:]
                if pid in keep and (c.get("author") or "") == authors.get(pid):
                    body = (c.get("body") or "").strip()
                    if body and body not in ("[removed]", "[deleted]"):
                        followups[pid].append(body)

    return posts, followups, len(candidates), len(done)


def render_post(p, followups):
    parts = [f'--- POST id={p["id"]} score={p.get("score", 0)} '
             f'comments={p.get("num_comments", 0)}',
             f'TITLE: {clip(p.get("title"), 300)}']
    body = clip(p.get("selftext"), 2500)
    if body and body not in ("[removed]", "[deleted]"):
        parts.append(f"BODY: {body}")
    fu = followups.get(p["id"])
    if fu:
        joined = clip("\n".join(fu), 1500)
        parts.append(f"AUTHOR FOLLOW-UP COMMENTS: {joined}")
    return "\n".join(parts)


def parse_response(text, expected_ids):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in response")
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        # Malformed array (usually an unescaped quote in one summary).
        # Salvage the individual objects that do parse.
        arr = []
        for om in re.finditer(r"\{[^{}]*\}", m.group(0)):
            try:
                arr.append(json.loads(om.group(0)))
            except json.JSONDecodeError:
                continue
        if not arr:
            raise
    results = []
    for obj in arr:
        if not isinstance(obj, dict) or obj.get("id") not in expected_ids:
            continue
        if obj.get("verdict") not in VERDICTS:
            continue
        obj["tags"] = [t for t in obj.get("tags", []) if t in TAGS]
        try:
            obj["confidence"] = max(0.0, min(1.0, float(obj.get("confidence", 0.5))))
        except (TypeError, ValueError):
            obj["confidence"] = 0.5
        results.append({k: obj[k] for k in
                        ("id", "verdict", "tags", "summary_zh", "confidence")
                        if k in obj})
    return results


class RateLimited(Exception):
    """Session-level failure (rate limit / quota / auth) — retrying other
    batches is pointless until it clears; abort the run, resume later."""


RATE_LIMIT_RE = re.compile(
    r"(rate.?limit|usage limit|session limit|hit your .{0,20}limit|limit reached"
    r"|resets \d|too many requests|429|overloaded|quota|credit balance"
    r"|login|authenticat)", re.I)


def run_claude(prompt, model):
    cmd = ["claude", "-p", "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          timeout=600)
    if proc.returncode != 0:
        blob = (proc.stderr + " " + proc.stdout)[:1000]
        if RATE_LIMIT_RE.search(blob):
            raise RateLimited(blob.strip()[:300])
        raise RuntimeError(f"claude exited {proc.returncode}: {blob[:500]}")
    if RATE_LIMIT_RE.search(proc.stdout[:300]) and "[" not in proc.stdout[:300]:
        # exit 0 but the "response" is an error banner, not JSON
        raise RateLimited(proc.stdout.strip()[:300])
    return proc.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--model", default="")
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    posts, followups, n_cand, n_done = load_inputs(args.limit)
    print(f"candidates={n_cand} classified={n_done} this_run={len(posts)}", flush=True)
    if not posts:
        return

    batches = [posts[i:i + args.batch] for i in range(0, len(posts), args.batch)]
    out = open(OUT, "a")
    lock = threading.Lock()
    stop = threading.Event()      # set on rate limit / repeated failures
    stop_reason = []
    ok = failed = done_batches = skipped = 0
    consec_failures = 0
    MAX_CONSEC_FAILURES = 3

    def work(batch):
        if stop.is_set():
            return None, None     # session is dead, don't burn more calls
        ids = {p["id"] for p in batch}
        prompt = PROMPT_HEADER + "\n\n".join(render_post(p, followups) for p in batch)
        for attempt in (1, 2):
            if stop.is_set():
                return None, None
            try:
                return ids, parse_response(run_claude(prompt, args.model), ids)
            except RateLimited as e:
                if not stop.is_set():
                    stop.set()
                    stop_reason.append(f"rate limited / session error: {e}")
                return None, None
            except Exception as e:
                print(f"  batch attempt {attempt} failed: {e}",
                      file=sys.stderr, flush=True)
        return ids, []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, b) for b in batches]
        for fut in as_completed(futures):
            ids, results = fut.result()
            with lock:
                if ids is None:   # skipped after stop
                    skipped += 1
                    continue
                done_batches += 1
                if not results:
                    failed += len(ids)
                    consec_failures += 1
                    if consec_failures >= MAX_CONSEC_FAILURES and not stop.is_set():
                        stop.set()
                        stop_reason.append(
                            f"{consec_failures} consecutive batch failures")
                    with open(ERRLOG, "a") as elog:
                        elog.write(f"batch failed: {sorted(ids)}\n")
                else:
                    consec_failures = 0
                    for r in results:
                        out.write(json.dumps(r, ensure_ascii=False) + "\n")
                    out.flush()
                    ok += len(results)
                    missing = ids - {r["id"] for r in results}
                    if missing:
                        print(f"  warn: missing {sorted(missing)} (retry next run)",
                              flush=True)
                print(f"progress: {done_batches}/{len(batches)} batches, "
                      f"{ok} classified, {failed} failed", flush=True)

    out.close()
    if stop.is_set():
        print(f"ABORTED EARLY: {'; '.join(stop_reason)}", flush=True)
        print(f"  +{ok} classified, {failed} failed, ~{skipped} batches skipped. "
              f"Progress is saved — rerun classify.py later to resume.", flush=True)
        sys.exit(2)
    print(f"done: +{ok} classifications ({failed} failed; rerun to retry)", flush=True)


if __name__ == "__main__":
    main()
