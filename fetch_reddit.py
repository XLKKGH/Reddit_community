#!/usr/bin/env python3
"""
Fetch Reddit posts and comments for u/IndependenceGold5902
and generate daily markdown summaries.
"""

import gzip
import json
import os
import time
from datetime import datetime, timezone
import urllib.request
import urllib.error

USERNAME = "IndependenceGold5902"
POSTS_DIR = "memory_kb_post"
README_PATH = "README.md"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
}


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()
        if resp.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def fetch_all_posts():
    posts = []
    after = None
    while True:
        url = f"https://www.reddit.com/user/{USERNAME}/submitted.json?limit=100"
        if after:
            url += f"&after={after}"
        data = fetch_json(url)
        children = data["data"]["children"]
        if not children:
            break
        for child in children:
            posts.append(child["data"])
        after = data["data"].get("after")
        if not after:
            break
        time.sleep(1)
    return posts


def fetch_comments_for_post(post_id, subreddit):
    url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?limit=50"
    try:
        data = fetch_json(url)
        comments = []
        if len(data) < 2:
            return comments
        for child in data[1]["data"]["children"]:
            c = child["data"]
            if c.get("body") and c["body"] != "[deleted]" and c["body"] != "[removed]":
                comments.append({
                    "author": c.get("author", "unknown"),
                    "body": c["body"].strip(),
                    "score": c.get("score", 0),
                    "replies": extract_replies(c)
                })
        # Sort by score descending
        comments.sort(key=lambda x: x["score"], reverse=True)
        return comments
    except Exception as e:
        print(f"  Warning: could not fetch comments for {post_id}: {e}")
        return []


def extract_replies(comment):
    replies = []
    try:
        if not comment.get("replies") or comment["replies"] == "":
            return replies
        children = comment["replies"]["data"]["children"]
        for child in children:
            c = child["data"]
            if c.get("body") and c["body"] != "[deleted]" and c["body"] != "[removed]":
                replies.append({
                    "author": c.get("author", "unknown"),
                    "body": c["body"].strip(),
                    "score": c.get("score", 0)
                })
    except Exception:
        pass
    return replies


def ts_to_date(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def generate_post_md(post, comments):
    date = ts_to_date(post["created_utc"])
    title = post["title"]
    subreddit = post["subreddit"]
    selftext = post.get("selftext", "").strip()
    url = f"https://www.reddit.com{post['permalink']}"
    score = post.get("score", 0)
    num_comments = post.get("num_comments", 0)

    lines = []
    lines.append(f"# {title}")
    lines.append(f"")
    lines.append(f"- **Date**: {date}")
    lines.append(f"- **Subreddit**: r/{subreddit}")
    lines.append(f"- **Score**: {score} | **Comments**: {num_comments}")
    lines.append(f"- **Link**: {url}")
    lines.append(f"")

    if selftext:
        lines.append(f"## My Post")
        lines.append(f"")
        lines.append(selftext)
        lines.append(f"")

    lines.append(f"## Comments ({len(comments)} fetched)")
    lines.append(f"")

    if not comments:
        lines.append("_No comments yet._")
    else:
        for i, c in enumerate(comments, 1):
            lines.append(f"### Comment {i} — u/{c['author']} (score: {c['score']})")
            lines.append(f"")
            lines.append(c["body"])
            lines.append(f"")
            for r in c["replies"]:
                lines.append(f"> **u/{r['author']}** (score: {r['score']}): {r['body']}")
                lines.append(f">")
            lines.append(f"")

    lines.append(f"---")
    lines.append(f"")
    lines.append(f"## Key Takeaways")
    lines.append(f"")
    lines.append(f"_TODO: summarize after reading comments_")
    lines.append(f"")

    return "\n".join(lines)


def generate_readme(posts_meta):
    lines = []
    lines.append("# 🧠 AI Agent Memory Research — Reddit Tracker")
    lines.append("")
    lines.append(f"**User**: u/{USERNAME}  ")
    lines.append(f"**Last updated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ")
    lines.append(f"**Total posts tracked**: {len(posts_meta)}")
    lines.append("")
    lines.append("## Posts Index")
    lines.append("")
    lines.append("| Date | Subreddit | Title | Score | Comments |")
    lines.append("|------|-----------|-------|-------|----------|")

    # Sort by date descending
    posts_meta.sort(key=lambda x: x["created_utc"], reverse=True)

    for p in posts_meta:
        date = ts_to_date(p["created_utc"])
        subreddit = p["subreddit"]
        title = p["title"][:60] + ("..." if len(p["title"]) > 60 else "")
        score = p["score"]
        num_comments = p["num_comments"]
        filename = f"{POSTS_DIR}/{p['id']}_{date}.md"
        lines.append(f"| {date} | r/{subreddit} | [{title}]({filename}) | {score} | {num_comments} |")

    lines.append("")
    lines.append("---")
    lines.append("*Auto-generated by fetch_reddit.py*")

    return "\n".join(lines)


def main():
    print(f"Fetching posts for u/{USERNAME}...")
    posts = fetch_all_posts()
    print(f"Found {len(posts)} posts.")

    os.makedirs(POSTS_DIR, exist_ok=True)

    posts_meta = []
    for post in posts:
        post_id = post["id"]
        date = ts_to_date(post["created_utc"])
        subreddit = post["subreddit"]
        filename = f"{POSTS_DIR}/{post_id}_{date}.md"

        print(f"  Processing: [{subreddit}] {post['title'][:50]}...")

        comments = fetch_comments_for_post(post_id, subreddit)
        md_content = generate_post_md(post, comments)

        with open(filename, "w", encoding="utf-8") as f:
            f.write(md_content)

        posts_meta.append({
            "id": post_id,
            "title": post["title"],
            "subreddit": subreddit,
            "created_utc": post["created_utc"],
            "score": post["score"],
            "num_comments": post["num_comments"]
        })

        time.sleep(1)  # Be polite to Reddit's servers

    # Update README index
    readme_content = generate_readme(posts_meta)
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(readme_content)

    print(f"\nDone! README updated, {len(posts)} post files written to {POSTS_DIR}/")


if __name__ == "__main__":
    main()
