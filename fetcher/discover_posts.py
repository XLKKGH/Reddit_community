#!/usr/bin/env python3
"""
Scan memory_kb_post/*.md for Reddit links and write every unique post URL
into posts.txt, so fetch_reddit_playwright.py can pull full comment trees
for all of them.

Usage:
    python3 discover_posts.py
"""

import re
from pathlib import Path

ROOT       = Path(__file__).parent.parent   # fetcher/ → repo root
MD_DIR     = ROOT / "memory_kb_post"
POSTS_FILE = ROOT / "posts.txt"

URL_RE = re.compile(r"https://(?:www\.)?reddit\.com/r/[^/\s)]+/comments/[a-z0-9]+")


def canonical_url(raw_url: str) -> str:
    m = re.search(r"reddit\.com/(r/[^/]+/comments/[a-z0-9]+)", raw_url)
    return f"https://www.reddit.com/{m.group(1)}/"


def main():
    if not MD_DIR.exists():
        print(f"[!] {MD_DIR}/ not found")
        return

    urls = set()
    for md_path in MD_DIR.glob("*.md"):
        text = md_path.read_text(encoding="utf-8")
        for match in URL_RE.findall(text):
            urls.add(canonical_url(match))

    existing = set()
    header_lines = []
    if POSTS_FILE.exists():
        for line in POSTS_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                header_lines.append(line)
            elif URL_RE.match(stripped):
                existing.add(canonical_url(stripped))

    all_urls = sorted(existing | urls)
    new_count = len(all_urls) - len(existing)

    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        for line in header_lines:
            f.write(line + "\n")
        f.write("\n")
        for u in all_urls:
            f.write(u + "\n")

    print(f"[+] Found {len(urls)} post URL(s) in {MD_DIR}/")
    print(f"[+] posts.txt now has {len(all_urls)} unique URL(s) ({new_count} new)")


if __name__ == "__main__":
    main()
