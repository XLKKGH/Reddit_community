#!/usr/bin/env python3
"""
Reddit Research Tracker — Flask Web App
Run: python3 app.py
Open: http://localhost:5000
"""

import json, os, re, time, gzip, ssl, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── config ───────────────────────────────────────────────────────────────────
COOKIES_FILE = "reddit_cookies.json"
DATA_FILE    = "data/posts.json"
API_KEY      = os.getenv("DEEPSEEK_API_KEY")
API_BASE     = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
MODEL        = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode    = ssl.CERT_NONE

REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
# ─────────────────────────────────────────────────────────────────────────────


# ── data helpers ─────────────────────────────────────────────────────────────
def load_data() -> list:
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def save_data(posts: list):
    Path(DATA_FILE).parent.mkdir(exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

def find_post(posts: list, post_id: str) -> dict | None:
    return next((p for p in posts if p["id"] == post_id), None)

def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
# ─────────────────────────────────────────────────────────────────────────────


# ── reddit helpers ────────────────────────────────────────────────────────────
def load_cookie_header() -> str:
    if not Path(COOKIES_FILE).exists():
        raise FileNotFoundError(f"{COOKIES_FILE} not found")
    with open(COOKIES_FILE) as f:
        cookies = json.load(f)
    return "; ".join(
        f"{c.get('name','')  }={c.get('value','')}"
        for c in cookies if c.get("name") and c.get("value")
    )

def extract_post_id(url: str) -> str:
    m = re.search(r"/comments/([a-z0-9]+)", url)
    return m.group(1) if m else ""

def fetch_reddit_json(url: str, cookie_header: str) -> dict:
    headers = {**REDDIT_HEADERS, "Cookie": cookie_header}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
        raw = resp.read()
        if resp.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)

def fetch_post_and_comments(url: str, cookie_header: str) -> tuple[dict, list]:
    json_url = url.rstrip("/") + ".json?limit=500&depth=10"
    data     = fetch_reddit_json(json_url, cookie_header)
    pd       = data[0]["data"]["children"][0]["data"]
    post = {
        "id":          pd.get("id"),
        "title":       pd.get("title"),
        "selftext":    pd.get("selftext", "").strip(),
        "subreddit":   pd.get("subreddit"),
        "score":       pd.get("score"),
        "url":         url,
        "num_comments": pd.get("num_comments", 0),
        "created_utc": pd.get("created_utc"),
    }
    comments = []
    _parse_comments(data[1]["data"]["children"], comments, depth=0)
    return post, comments

def _parse_comments(children: list, out: list, depth: int):
    for child in children:
        d    = child.get("data", {})
        body = d.get("body", "")
        if not body or body in ("[deleted]", "[removed]"):
            continue
        c = {
            "id":          d.get("id", ""),
            "author":      d.get("author", "unknown"),
            "body":        body.strip(),
            "score":       d.get("score", 0),
            "depth":       depth,
            "created_utc": d.get("created_utc"),
            "replies":     [],
        }
        rep = d.get("replies")
        if rep and isinstance(rep, dict):
            _parse_comments(rep["data"]["children"], c["replies"], depth + 1)
        out.append(c)

def flatten_comments(comments: list) -> list:
    """Flatten nested comments into a single list."""
    result = []
    def _flatten(cs):
        for c in cs:
            result.append(c)
            _flatten(c.get("replies", []))
    _flatten(comments)
    return result
# ─────────────────────────────────────────────────────────────────────────────


# ── LLM helpers ───────────────────────────────────────────────────────────────
BOTS = {"AutoModerator", "reddit", "BotDefense", "Snooful", "AutoModerator"}

SYSTEM_PROMPT = "你是 AI Agent Memory 领域研究助手，分析 Reddit 评论，提炼研究价值。"

ANALYZE_PROMPT = """分析以下 Reddit 评论，返回 JSON（直接输出，不加代码块）：

帖子：{post_title}
作者：u/{author}（score: {score}）
原文：
{body}

返回格式：
{{
  "summary": "一句话概括核心观点（中文，30字以内）",
  "takeaway": "对 agent memory 研究的核心价值（中文，50字以内）",
  "quality": 评分整数1-5,
  "quality_reason": "评分理由（中文，20字以内）",
  "highlight": true或false,
  "tags": ["标签"]
}}

评分：5=深度洞察+真实场景 4=观点新颖或有深度 3=一般参考 2=泛泛而谈 1=无意义
标签选1-3个：#用户痛点 #技术方案 #产品反馈 #使用场景 #哲学思考 #隐私安全 #跨平台 #记忆管理 #个性化 #工作流 #反对意见 #类比参考"""

def call_llm(prompt: str) -> str:
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens":  300,
    }).encode()
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
        raw = resp.read()
        if resp.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)["choices"][0]["message"]["content"].strip()

def analyze_comment(comment: dict, post_title: str, retries: int = 3) -> dict:
    if comment["author"] in BOTS:
        return None
    prompt = ANALYZE_PROMPT.format(
        post_title=post_title, author=comment["author"],
        score=comment["score"], body=comment["body"],
    )
    for attempt in range(1, retries + 1):
        try:
            raw = call_llm(prompt).lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(raw)
            return result
        except Exception as e:
            if attempt < retries:
                time.sleep(attempt * 3)
            else:
                return {"summary": "（分析失败）", "takeaway": "（分析失败）",
                        "quality": 0, "quality_reason": "error",
                        "highlight": False, "tags": []}

def process_comments(comments: list, post_title: str) -> list:
    """Run LLM analysis on a list of flat comments. Returns processed list."""
    processed = []
    for c in comments:
        if c["author"] in BOTS:
            continue
        analysis = analyze_comment(c, post_title)
        if analysis is None:
            continue
        processed.append({**c, "analysis": analysis, "is_new": True})
        time.sleep(0.5)
    return processed
# ─────────────────────────────────────────────────────────────────────────────


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/posts", methods=["GET"])
def get_posts():
    return jsonify(load_data())

@app.route("/api/add", methods=["POST"])
def add_post():
    """Add a new post URL: fetch all comments + LLM process all (one-time init)."""
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    post_id = extract_post_id(url)
    if not post_id:
        return jsonify({"error": "Invalid Reddit URL"}), 400

    posts = load_data()
    if find_post(posts, post_id):
        return jsonify({"error": "This post is already being tracked"}), 400

    try:
        cookie_header = load_cookie_header()
        post_meta, raw_comments = fetch_post_and_comments(url, cookie_header)
    except Exception as e:
        return jsonify({"error": f"Failed to fetch post: {e}"}), 500

    flat = flatten_comments(raw_comments)
    processed = process_comments(flat, post_meta["title"])

    # Mark all as NOT new (they're the initial state)
    for c in processed:
        c["is_new"] = False

    seen_ids = [c["id"] for c in flat if c.get("id")]

    new_post = {
        **post_meta,
        "added_at":     now_str(),
        "last_checked": now_str(),
        "has_new":      False,
        "new_count":    0,
        "seen_ids":     seen_ids,
        "comments":     processed,
    }
    posts.append(new_post)
    save_data(posts)
    return jsonify(new_post)


@app.route("/api/refresh", methods=["POST"])
def refresh():
    """Check all tracked posts for new comments, process only new ones."""
    posts = load_data()
    if not posts:
        return jsonify({"message": "No posts tracked yet", "updated": []})

    try:
        cookie_header = load_cookie_header()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    updated = []

    for post in posts:
        try:
            _, raw_comments = fetch_post_and_comments(post["url"], cookie_header)
        except Exception as e:
            continue

        flat     = flatten_comments(raw_comments)
        seen_ids = set(post.get("seen_ids", []))
        new_raw  = [c for c in flat if c.get("id") and c["id"] not in seen_ids]

        if not new_raw:
            post["has_new"]   = False
            post["new_count"] = 0
            continue

        # Process only new comments
        new_processed = process_comments(new_raw, post["title"])

        # Mark them as new
        for c in new_processed:
            c["is_new"] = True

        # Update post data
        post["comments"]    += new_processed
        post["seen_ids"]    += [c["id"] for c in new_raw if c.get("id")]
        post["has_new"]      = True
        post["new_count"]    = len(new_processed)
        post["last_checked"] = now_str()

        updated.append({
            "post_id":    post["id"],
            "title":      post["title"],
            "new_count":  len(new_processed),
        })
        time.sleep(1)

    save_data(posts)
    return jsonify({
        "message": f"{len(updated)} post(s) have new comments" if updated else "No new comments",
        "updated": updated,
    })


@app.route("/api/clear-new/<post_id>", methods=["POST"])
def clear_new(post_id):
    """Mark a post's new comments as read."""
    posts = load_data()
    post  = find_post(posts, post_id)
    if post:
        post["has_new"]   = False
        post["new_count"] = 0
        for c in post.get("comments", []):
            c["is_new"] = False
        save_data(posts)
    return jsonify({"ok": True})


@app.route("/api/delete/<post_id>", methods=["DELETE"])
def delete_post(post_id):
    """Remove a post from tracking."""
    posts = load_data()
    posts = [p for p in posts if p["id"] != post_id]
    save_data(posts)
    return jsonify({"ok": True})
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5001)
