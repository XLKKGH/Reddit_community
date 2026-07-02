#!/usr/bin/env python3
"""
Build a single self-contained HTML report from processed/*.json.
No server needed — open the file directly or send it to your boss.

Usage:
    python3 generate_report.py
"""

import html
import json
from datetime import datetime, timezone
from pathlib import Path

PROCESSED_DIR = Path("processed")
REPORTS_DIR = Path("reports")
MANIFEST_FILE = PROCESSED_DIR / "last_run_new_ids.json"

CSS = """
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       max-width: 900px; margin: 0 auto; padding: 24px; color: #1a1a1a; background: #fafafa; }
header.report-header { background: #1a1a2e; color: white; padding: 24px; border-radius: 10px; margin-bottom: 24px; }
header.report-header h1 { margin: 0 0 8px 0; font-size: 22px; }
header.report-header .stats { color: #c9c9d9; font-size: 14px; }
h2.section-title { border-left: 4px solid #4a4ae0; padding-left: 10px; margin-top: 36px; }
.card { background: white; border: 1px solid #e5e5ea; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
.card.highlight { border-left: 4px solid #f5a623; }
.card .meta { font-size: 13px; color: #888; margin-bottom: 6px; }
.card .meta .author { font-weight: 600; color: #333; }
.card .tag { display: inline-block; background: #eef0ff; color: #4a4ae0; font-size: 12px;
             padding: 2px 8px; border-radius: 10px; margin-right: 4px; }
.card .summary { font-size: 15px; margin: 6px 0; }
.card .takeaway { font-size: 14px; color: #555; background: #fbf7ea; padding: 8px 10px; border-radius: 6px; }
.card details { margin-top: 8px; font-size: 13px; color: #666; }
.card details summary { cursor: pointer; color: #4a4ae0; }
.card .quality-badge { display: inline-block; font-size: 12px; padding: 1px 6px; border-radius: 4px;
                        background: #eee; margin-left: 6px; }
.post-block { margin-top: 28px; }
.post-block .post-title { font-size: 17px; font-weight: 600; }
.post-block .post-meta { font-size: 13px; color: #888; margin-bottom: 12px; }
.new-badge { color: #d9480f; font-weight: 600; }
"""


def load_processed() -> list:
    posts = []
    for path in sorted(PROCESSED_DIR.glob("*.json")):
        if path.name in ("seen_llm.json", "last_run_new_ids.json"):
            continue
        with open(path) as f:
            posts.append(json.load(f))
    return posts


def load_manifest() -> dict:
    if MANIFEST_FILE.exists():
        with open(MANIFEST_FILE) as f:
            return json.load(f)
    return {}


def e(s) -> str:
    return html.escape(str(s), quote=True)


def render_comment_card(c: dict, new_ids: set, show_post_link: bool = False, post=None) -> str:
    is_new = c["id"] in new_ids
    highlight_class = " highlight" if c.get("highlight") else ""
    tags_html = "".join(f'<span class="tag">#{e(t)}</span>' for t in c.get("tags", []))
    new_badge = ' <span class="new-badge">🆕</span>' if is_new else ""
    star = "⭐ " if c.get("highlight") else ""

    post_link_html = ""
    if show_post_link and post:
        post_link_html = f'<div class="meta">来自：<a href="{e(post["url"])}" target="_blank">{e(post["title"][:50])}</a></div>'

    return f"""
    <div class="card{highlight_class}">
      {post_link_html}
      <div class="meta">
        {star}<span class="author">u/{e(c['author'])}</span>
        · score {e(c.get('score', 0))}
        <span class="quality-badge">quality {e(c.get('quality', '-'))}</span>
        {new_badge}
        {tags_html}
      </div>
      <div class="summary">📝 {e(c.get('summary', ''))}</div>
      <div class="takeaway">💡 {e(c.get('takeaway', ''))}</div>
      <details>
        <summary>原文 / 打分理由</summary>
        <p>{e(c.get('body', ''))}</p>
        <p><em>打分理由：{e(c.get('quality_reason', ''))}</em></p>
      </details>
    </div>
    """


def render_report(posts: list, manifest: dict) -> str:
    total_comments = sum(len(p["comments"]) for p in posts)
    all_highlights = []
    for p in posts:
        for c in p["comments"]:
            if c.get("highlight"):
                all_highlights.append((c, p["post"]))
    all_highlights.sort(key=lambda x: (x[0].get("quality", 0), x[0].get("score", 0)), reverse=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    parts = [f"<!DOCTYPE html><html><head><meta charset='utf-8'>",
             f"<title>Reddit Research Report {date_str}</title>",
             f"<style>{CSS}</style></head><body>"]

    parts.append(f"""
    <header class="report-header">
      <h1>📊 Reddit Research Report</h1>
      <div class="stats">{date_str} | {len(posts)} posts | {total_comments} comments | ⭐ {len(all_highlights)} highlighted</div>
    </header>
    """)

    if all_highlights:
        parts.append('<h2 class="section-title">⭐ Highlights</h2>')
        for c, post in all_highlights:
            new_ids = set(manifest.get(post["id"], []))
            parts.append(render_comment_card(c, new_ids, show_post_link=True, post=post))

    for p in posts:
        post = p["post"]
        comments = p["comments"]
        new_ids = set(manifest.get(post["id"], []))
        new_count = len(new_ids)
        highlight_count = sum(1 for c in comments if c.get("highlight"))

        parts.append(f"""
        <div class="post-block">
          <h2 class="section-title">{e(post['title'])}</h2>
          <div class="post-meta">
            <a href="{e(post['url'])}" target="_blank">r/{e(post['subreddit'])}</a>
            · 总评论 {len(comments)} 条
            · 新增 {new_count} 条
            · ⭐ {highlight_count} 条高亮
          </div>
        """)
        for c in sorted(comments, key=lambda x: x.get("score", 0), reverse=True):
            parts.append(render_comment_card(c, new_ids))
        parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts)


def main():
    if not PROCESSED_DIR.exists():
        print(f"[!] {PROCESSED_DIR}/ not found — run process_comments.py first")
        return

    posts = load_processed()
    if not posts:
        print(f"[!] No processed posts found in {PROCESSED_DIR}/")
        return

    manifest = load_manifest()
    report_html = render_report(posts, manifest)

    REPORTS_DIR.mkdir(exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = REPORTS_DIR / f"report_{date_str}.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_html)

    print(f"[+] Report written to {out_path}")


if __name__ == "__main__":
    main()
