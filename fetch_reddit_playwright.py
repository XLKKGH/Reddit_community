#!/usr/bin/env python3
"""
Reddit comment fetcher using saved browser cookies (no Playwright needed).

Step 1: Export Reddit cookies from Chrome using "Cookie Editor" extension
        → save as reddit_cookies.json in this directory
Step 2: python3 fetch_reddit_playwright.py
"""

import json
import os
import time
import re
import gzip
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from http.cookiejar import CookieJar
import http.cookiejar

# ── config ──────────────────────────────────────────────────────────────────
COOKIES_FILE = "reddit_cookies.json"
POSTS_FILE   = "posts.txt"
SEEN_FILE    = "seen_comments.json"
OUTPUT_DIR   = "raw_comments"
# ────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def load_cookies_as_header() -> str:
    """Load cookies from reddit_cookies.json and build a Cookie header string."""
    if not Path(COOKIES_FILE).exists():
        print(f"[!] {COOKIES_FILE} not found.")
        print("    Export Reddit cookies from Chrome using 'Cookie Editor' extension")
        print("    (Export → Export as JSON → save as reddit_cookies.json)")
        raise SystemExit(1)

    with open(COOKIES_FILE) as f:
        cookies = json.load(f)

    # Cookie Editor exports as list of {name, value, domain, ...}
    # Some exporters use {name, value} directly, others nest differently
    parts = []
    for c in cookies:
        name  = c.get("name") or c.get("key", "")
        value = c.get("value", "")
        if name and value:
            parts.append(f"{name}={value}")

    if not parts:
        print("[!] No cookies found in reddit_cookies.json — re-export from Chrome")
        raise SystemExit(1)

    print(f"[+] Loaded {len(parts)} cookies from {COOKIES_FILE}")
    return "; ".join(parts)


def fetch_json(url: str, cookie_header: str) -> dict:
    headers = dict(HEADERS)
    headers["Cookie"] = cookie_header

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            if resp.info().get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  [!] HTTP {e.code} for {url}")
        if e.code == 403:
            print("      Cookies may be expired — re-export from Chrome")
        raise


def extract_post_id(url: str) -> str:
    m = re.search(r"/comments/([a-z0-9]+)", url)
    return m.group(1) if m else ""


def fetch_post_comments(url: str, cookie_header: str) -> dict:
    """Fetch a post and all its comments via Reddit's .json endpoint."""
    json_url = url.rstrip("/") + ".json?limit=500&depth=10"
    data = fetch_json(json_url, cookie_header)

    post_data    = data[0]["data"]["children"][0]["data"]
    comments_raw = data[1]["data"]["children"]

    post = {
        "id":          post_data.get("id"),
        "title":       post_data.get("title"),
        "selftext":    post_data.get("selftext", "").strip(),
        "subreddit":   post_data.get("subreddit"),
        "score":       post_data.get("score"),
        "url":         url,
        "created_utc": post_data.get("created_utc"),
        "num_comments": post_data.get("num_comments", 0),
    }

    comments = []
    _extract_comments(comments_raw, comments, depth=0)
    return {"post": post, "comments": comments}


def _extract_comments(children: list, out: list, depth: int):
    for child in children:
        d = child.get("data", {})
        body = d.get("body", "")
        if not body or body in ("[deleted]", "[removed]"):
            continue
        comment = {
            "id":          d.get("id", ""),
            "author":      d.get("author", "unknown"),
            "body":        body.strip(),
            "score":       d.get("score", 0),
            "depth":       depth,
            "created_utc": d.get("created_utc"),
            "replies":     [],
        }
        replies_data = d.get("replies")
        if replies_data and isinstance(replies_data, dict):
            reply_children = replies_data.get("data", {}).get("children", [])
            _extract_comments(reply_children, comment["replies"], depth + 1)
        out.append(comment)


def load_seen() -> dict:
    if Path(SEEN_FILE).exists():
        with open(SEEN_FILE) as f:
            return json.load(f)
    return {}


def save_seen(seen: dict):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def find_new_comments(post_id: str, comments: list, seen: dict) -> list:
    seen_ids = set(seen.get(post_id, []))
    new = []
    _collect_new(comments, seen_ids, new)
    return new


def _collect_new(comments: list, seen_ids: set, new: list):
    for c in comments:
        if c["id"] and c["id"] not in seen_ids:
            new.append(c)
        _collect_new(c.get("replies", []), seen_ids, new)


def mark_seen(post_id: str, comments: list, seen: dict):
    if post_id not in seen:
        seen[post_id] = []
    ids = set(seen[post_id])
    _collect_ids(comments, ids)
    seen[post_id] = list(ids)


def _collect_ids(comments: list, ids: set):
    for c in comments:
        if c["id"]:
            ids.add(c["id"])
        _collect_ids(c.get("replies", []), ids)


def main():
    if not Path(POSTS_FILE).exists():
        print(f"[!] {POSTS_FILE} not found — add your Reddit post URLs (one per line)")
        return

    with open(POSTS_FILE) as f:
        urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    if not urls:
        print("[!] No URLs in posts.txt")
        return

    cookie_header = load_cookies_as_header()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    seen    = load_seen()
    results = []

    print(f"[+] Checking {len(urls)} post(s)...\n")

    for url in urls:
        try:
            result = fetch_post_comments(url, cookie_header)
        except Exception as e:
            print(f"  [!] Skipping {url}: {e}")
            continue

        post         = result["post"]
        post_id      = post["id"] or extract_post_id(url)
        all_comments = result["comments"]
        new_comments = find_new_comments(post_id, all_comments, seen)

        print(f"  ✓ r/{post['subreddit']} — {post['title'][:55]}")
        print(f"    total: {len(all_comments)} comments | 🆕 new: {len(new_comments)}")

        # Save full snapshot
        out_path = Path(OUTPUT_DIR) / f"{post_id}.json"
        with open(out_path, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        if new_comments:
            results.append({
                "post":         post,
                "new_comments": new_comments,
                "all_comments": all_comments,
            })

        mark_seen(post_id, all_comments, seen)
        time.sleep(1.5)

    save_seen(seen)

    if results:
        total_new = sum(len(r["new_comments"]) for r in results)
        print(f"\n[+] {total_new} new comment(s) found across {len(results)} post(s)")

        ts         = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        batch_path = Path(OUTPUT_DIR) / f"batch_{ts}.json"
        with open(batch_path, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"[+] Saved → {batch_path}  (ready for LLM processing)")
    else:
        print("\n[+] No new comments since last run.")


if __name__ == "__main__":
    main()
