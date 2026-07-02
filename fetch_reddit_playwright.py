#!/usr/bin/env python3
"""
Reddit comment fetcher using Playwright.
Step 1: login_reddit.py  — log in once, save cookies
Step 2: this script      — load cookies, fetch comments from post URLs
"""

import json
import os
import time
import re
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

# ── config ──────────────────────────────────────────────────────────────────
COOKIES_FILE  = "reddit_cookies.json"
POSTS_FILE    = "posts.txt"          # one Reddit post URL per line
SEEN_FILE     = "seen_comments.json" # tracks already-processed comment IDs
OUTPUT_DIR    = "raw_comments"       # raw JSON output per post
# ────────────────────────────────────────────────────────────────────────────


def load_cookies(context):
    if not Path(COOKIES_FILE).exists():
        print(f"[!] {COOKIES_FILE} not found — run login_reddit.py first")
        return False
    with open(COOKIES_FILE) as f:
        cookies = json.load(f)
    context.add_cookies(cookies)
    return True


def load_seen() -> dict:
    if Path(SEEN_FILE).exists():
        with open(SEEN_FILE) as f:
            return json.load(f)
    return {}


def save_seen(seen: dict):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


def extract_post_id(url: str) -> str:
    """Extract Reddit post ID from URL like /comments/abc123/..."""
    m = re.search(r"/comments/([a-z0-9]+)", url)
    return m.group(1) if m else url.split("/")[-1]


def fetch_post_comments(page, url: str) -> dict:
    """Navigate to a Reddit post and extract post + comments."""
    print(f"  → fetching: {url}")

    # Use .json API endpoint — works when logged in via browser session
    json_url = url.rstrip("/") + ".json?limit=500&depth=10"
    page.goto(json_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)

    try:
        raw = page.locator("pre").first.inner_text()
        data = json.loads(raw)
    except Exception as e:
        print(f"  [!] JSON parse failed: {e}")
        # Fallback: try the HTML page
        return fetch_post_comments_html(page, url)

    post_data  = data[0]["data"]["children"][0]["data"]
    comments_raw = data[1]["data"]["children"]

    post = {
        "id":        post_data.get("id"),
        "title":     post_data.get("title"),
        "selftext":  post_data.get("selftext", "").strip(),
        "subreddit": post_data.get("subreddit"),
        "score":     post_data.get("score"),
        "url":       url,
        "created_utc": post_data.get("created_utc"),
    }

    comments = []
    _extract_comments(comments_raw, comments, depth=0)

    return {"post": post, "comments": comments}


def fetch_post_comments_html(page, url: str) -> dict:
    """Fallback: scrape rendered HTML page."""
    print(f"  → HTML fallback for: {url}")
    page.goto(url, wait_until="networkidle", timeout=30000)
    time.sleep(3)

    # Grab title
    title = ""
    try:
        title = page.locator("h1").first.inner_text()
    except Exception:
        pass

    # Try to get comments via shreddit-comment elements (new Reddit)
    comments = []
    try:
        els = page.locator("shreddit-comment").all()
        for el in els:
            try:
                author = el.get_attribute("author") or "unknown"
                score  = int(el.get_attribute("score") or 0)
                body_el = el.locator("div[slot='text-body']").first
                body   = body_el.inner_text() if body_el else ""
                cid    = el.get_attribute("thingid") or el.get_attribute("id") or ""
                depth  = int(el.get_attribute("depth") or 0)
                if body.strip():
                    comments.append({
                        "id":     cid,
                        "author": author,
                        "body":   body.strip(),
                        "score":  score,
                        "depth":  depth,
                        "replies": []
                    })
            except Exception:
                continue
    except Exception as e:
        print(f"  [!] HTML scrape error: {e}")

    post_id = extract_post_id(url)
    return {
        "post": {"id": post_id, "title": title, "url": url, "selftext": "", "subreddit": "", "score": 0},
        "comments": comments
    }


def _extract_comments(children, out: list, depth: int):
    for child in children:
        d = child.get("data", {})
        body = d.get("body", "")
        if not body or body in ("[deleted]", "[removed]"):
            continue
        comment = {
            "id":      d.get("id", ""),
            "author":  d.get("author", "unknown"),
            "body":    body.strip(),
            "score":   d.get("score", 0),
            "depth":   depth,
            "created_utc": d.get("created_utc"),
            "replies": []
        }
        # Recurse into replies
        replies_data = d.get("replies")
        if replies_data and isinstance(replies_data, dict):
            reply_children = replies_data.get("data", {}).get("children", [])
            _extract_comments(reply_children, comment["replies"], depth + 1)
        out.append(comment)


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
    existing = set(seen[post_id])
    _collect_ids(comments, existing)
    seen[post_id] = list(existing)


def _collect_ids(comments: list, ids: set):
    for c in comments:
        if c["id"]:
            ids.add(c["id"])
        _collect_ids(c.get("replies", []), ids)


def main():
    if not Path(POSTS_FILE).exists():
        print(f"[!] Create {POSTS_FILE} with one Reddit post URL per line")
        return

    with open(POSTS_FILE) as f:
        urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]

    if not urls:
        print("[!] No URLs found in posts.txt")
        return

    print(f"[+] {len(urls)} post(s) to check")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    seen = load_seen()
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)  # use system Chrome
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"
        )

        logged_in = load_cookies(context)
        if not logged_in:
            browser.close()
            return

        page = context.new_page()

        for url in urls:
            try:
                result = fetch_post_comments(page, url)
            except Exception as e:
                print(f"  [!] Error on {url}: {e}")
                continue

            post    = result["post"]
            post_id = post["id"] or extract_post_id(url)
            all_comments = result["comments"]
            new_comments = find_new_comments(post_id, all_comments, seen)

            print(f"  ✓ [{post['subreddit']}] {post['title'][:50]}")
            print(f"    total: {len(all_comments)} comments | new: {len(new_comments)}")

            # Save raw JSON
            out_path = Path(OUTPUT_DIR) / f"{post_id}.json"
            with open(out_path, "w") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)

            if new_comments:
                results.append({
                    "post": post,
                    "new_comments": new_comments,
                    "all_comments": all_comments,
                })

            mark_seen(post_id, all_comments, seen)
            time.sleep(2)

        browser.close()

    save_seen(seen)

    if results:
        print(f"\n[+] {sum(len(r['new_comments']) for r in results)} new comment(s) found")
        # Save for LLM processing
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        batch_path = Path(OUTPUT_DIR) / f"batch_{ts}.json"
        with open(batch_path, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"[+] Saved to {batch_path} — ready for LLM processing")
    else:
        print("\n[+] No new comments since last run")


if __name__ == "__main__":
    main()
