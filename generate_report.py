#!/usr/bin/env python3
"""
Step 3: Generate HTML report from processed comments.
Reads:  processed/batch_YYYY-MM-DD.json
Writes: reports/report_YYYY-MM-DD.html
"""

import json, os, sys
from pathlib import Path
from datetime import datetime, timezone

OUTPUT_DIR = "reports"

def ts_to_str(ts):
    if not ts: return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

def quality_stars(q):
    return "★" * q + "☆" * (5 - q)

def tag_html(tags):
    colors = {
        "#用户痛点":  "#ef4444", "#技术方案":  "#3b82f6", "#产品反馈":  "#8b5cf6",
        "#使用场景":  "#10b981", "#哲学思考":  "#f59e0b", "#隐私安全":  "#6366f1",
        "#跨平台":    "#14b8a6", "#记忆管理":  "#ec4899", "#个性化":    "#84cc16",
        "#工作流":    "#f97316", "#反对意见":  "#dc2626", "#类比参考":  "#0ea5e9",
    }
    html = ""
    for t in (tags or []):
        color = colors.get(t, "#6b7280")
        html += f'<span class="tag" style="background:{color}20;color:{color};border:1px solid {color}40">{t}</span>'
    return html

def comment_card(c, is_highlight=False):
    a   = c.get("analysis", {})
    cls = "comment-card highlight-card" if is_highlight else "comment-card"
    q   = a.get("quality", 0)
    return f"""
    <div class="{cls}">
      <div class="comment-header">
        <span class="author">u/{c['author']}</span>
        <span class="score">▲ {c['score']}</span>
        {'<span class="highlight-badge">⭐ 精选</span>' if is_highlight else ''}
        <span class="quality-stars" title="质量评分 {q}/5">{quality_stars(q)}</span>
        <span class="date">{ts_to_str(c.get('created_utc'))}</span>
      </div>
      <div class="tags">{tag_html(a.get('tags', []))}</div>
      <div class="analysis-row">
        <div class="analysis-item"><span class="label">📝 摘要</span><span>{a.get('summary','')}</span></div>
        <div class="analysis-item"><span class="label">💡 价值</span><span>{a.get('takeaway','')}</span></div>
        <div class="analysis-item"><span class="label">🎯 评分</span><span>{q}/5 — {a.get('quality_reason','')}</span></div>
      </div>
      <details class="body-details">
        <summary>查看原文</summary>
        <div class="body-text">{c['body'].replace('<','&lt;').replace('>','&gt;').replace(chr(10),'<br>')}</div>
      </details>
    </div>"""

def generate_html(processed_path):
    with open(processed_path) as f:
        data = json.load(f)

    date_str      = Path(processed_path).stem.replace("batch_", "")
    total_new     = sum(x["new_count"] for x in data)
    total_posts   = len(data)
    total_hl      = sum(x["highlight_count"] for x in data)
    all_highlights = [c for x in data for c in x["processed_comments"] if c["analysis"].get("highlight")]
    all_highlights.sort(key=lambda c: c["analysis"].get("quality", 0), reverse=True)

    # ── Highlights section ──────────────────────────────────
    hl_html = ""
    if all_highlights:
        hl_html = '<section class="section highlights-section"><h2>⭐ 精选评论</h2>'
        for c in all_highlights:
            hl_html += comment_card(c, is_highlight=True)
        hl_html += '</section>'

    # ── Per-post sections ───────────────────────────────────
    posts_html = ""
    for item in data:
        post = item["post"]
        pcs  = item["processed_comments"]
        non_hl = [c for c in pcs if not c["analysis"].get("highlight")]

        posts_html += f"""
        <section class="section post-section">
          <div class="post-header">
            <h2><a href="{post['url']}" target="_blank">{post['title']}</a></h2>
            <div class="post-meta">
              <span>r/{post['subreddit']}</span>
              <span>▲ {post.get('score',0)}</span>
              <span>💬 {item['total_comments']} 条评论</span>
              <span>🆕 {item['new_count']} 条新增</span>
              {'<span class="hl-badge">⭐ ' + str(item['highlight_count']) + ' 条精选</span>' if item['highlight_count'] else ''}
            </div>
          </div>
        """
        if pcs:
            posts_html += '<div class="comments-list">'
            for c in non_hl:
                posts_html += comment_card(c)
            posts_html += '</div>'
        else:
            posts_html += '<p class="no-comments">暂无新评论</p>'
        posts_html += '</section>'

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reddit Research Report — {date_str}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f8fafc; color: #1e293b; line-height: 1.6; }}
  a {{ color: #3b82f6; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  .header {{ background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
             color: white; padding: 40px 48px; }}
  .header h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 8px; }}
  .header-meta {{ display: flex; gap: 24px; margin-top: 16px; flex-wrap: wrap; }}
  .stat {{ background: rgba(255,255,255,0.1); border-radius: 8px;
           padding: 10px 18px; text-align: center; }}
  .stat-num {{ font-size: 1.5rem; font-weight: 700; display: block; }}
  .stat-label {{ font-size: 0.75rem; opacity: 0.8; }}

  .container {{ max-width: 900px; margin: 0 auto; padding: 32px 24px; }}
  .section {{ background: white; border-radius: 12px; padding: 24px;
              margin-bottom: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}

  .highlights-section {{ border: 2px solid #fbbf24; background: #fffbeb; }}
  .highlights-section h2 {{ color: #d97706; margin-bottom: 20px; font-size: 1.1rem; }}

  .post-header {{ margin-bottom: 20px; }}
  .post-header h2 {{ font-size: 1.05rem; font-weight: 600; margin-bottom: 8px; }}
  .post-meta {{ display: flex; gap: 12px; flex-wrap: wrap; font-size: 0.8rem; color: #64748b; }}
  .hl-badge {{ background: #fef3c7; color: #d97706; padding: 2px 8px; border-radius: 12px; }}

  .comment-card {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px;
                   margin-bottom: 12px; background: #f8fafc; }}
  .highlight-card {{ background: #fffbeb; border-color: #fcd34d; }}

  .comment-header {{ display: flex; align-items: center; gap: 10px;
                     flex-wrap: wrap; margin-bottom: 8px; }}
  .author {{ font-weight: 600; color: #1e40af; font-size: 0.9rem; }}
  .score {{ color: #ef4444; font-size: 0.85rem; font-weight: 600; }}
  .highlight-badge {{ background: #fbbf24; color: white; padding: 2px 8px;
                      border-radius: 12px; font-size: 0.75rem; font-weight: 600; }}
  .quality-stars {{ color: #f59e0b; font-size: 0.85rem; letter-spacing: 1px; }}
  .date {{ color: #94a3b8; font-size: 0.78rem; margin-left: auto; }}

  .tags {{ margin-bottom: 10px; display: flex; gap: 6px; flex-wrap: wrap; }}
  .tag {{ padding: 2px 8px; border-radius: 12px; font-size: 0.72rem; font-weight: 500; }}

  .analysis-row {{ display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }}
  .analysis-item {{ display: flex; gap: 8px; font-size: 0.85rem; }}
  .label {{ color: #64748b; min-width: 48px; flex-shrink: 0; }}

  .body-details summary {{ cursor: pointer; color: #6b7280; font-size: 0.82rem;
                            padding: 4px 0; user-select: none; }}
  .body-details summary:hover {{ color: #374151; }}
  .body-text {{ margin-top: 8px; padding: 12px; background: white; border-radius: 6px;
               border: 1px solid #e5e7eb; font-size: 0.85rem; color: #374151;
               white-space: pre-wrap; }}

  .comments-list {{ margin-top: 4px; }}
  .no-comments {{ color: #94a3b8; font-size: 0.88rem; padding: 8px 0; }}
  .footer {{ text-align: center; color: #94a3b8; font-size: 0.78rem; padding: 24px 0; }}
</style>
</head>
<body>

<div class="header">
  <h1>🧠 Reddit Research Report</h1>
  <p style="opacity:0.7;font-size:0.9rem">u/IndependenceGold5902 · {date_str}</p>
  <div class="header-meta">
    <div class="stat"><span class="stat-num">{total_posts}</span><span class="stat-label">帖子</span></div>
    <div class="stat"><span class="stat-num">{total_new}</span><span class="stat-label">新评论</span></div>
    <div class="stat"><span class="stat-num" style="color:#fbbf24">{total_hl}</span><span class="stat-label">⭐ 精选</span></div>
  </div>
</div>

<div class="container">
  {hl_html}
  {posts_html}
</div>

<div class="footer">Auto-generated · {date_str}</div>
</body>
</html>"""

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = str(Path(OUTPUT_DIR) / f"report_{date_str}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Report → {out}")
    return out


if __name__ == "__main__":
    bp = sys.argv[1] if len(sys.argv) > 1 else str(sorted(Path("processed").glob("batch_*.json"), reverse=True)[0])
    generate_html(bp)
