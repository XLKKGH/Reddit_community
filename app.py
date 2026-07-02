#!/usr/bin/env python3
"""
Reddit Research Tracker — Flask Web App

两台电脑分工：
  另一台电脑：运行 fetch_reddit_playwright.py → 生成 raw_comments/batch_*.json → git push
  这台 Mac：git pull → 网页点「导入」→ DeepSeek 处理 → 展示
"""

import json, os, re, time, gzip, ssl, urllib.request
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ── config ────────────────────────────────────────────────────────────────────
DATA_FILE    = "data/posts.json"
POSTS_TXT    = "posts.txt"
RAW_DIR      = Path("raw_comments")
API_KEY      = os.getenv("DEEPSEEK_API_KEY")
API_BASE     = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
MODEL        = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode    = ssl.CERT_NONE
# ─────────────────────────────────────────────────────────────────────────────


# ── data helpers ──────────────────────────────────────────────────────────────
def load_data() -> dict:
    """Returns {"posts": [...], "processed_batches": [...]}"""
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
            # 兼容旧格式（list）
            if isinstance(d, list):
                return {"posts": d, "processed_batches": []}
            return d
    return {"posts": [], "processed_batches": []}

def save_data(data: dict):
    Path(DATA_FILE).parent.mkdir(exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def find_post(posts: list, post_id: str):
    return next((p for p in posts if p["id"] == post_id), None)

def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def extract_post_id(url: str) -> str:
    m = re.search(r"/comments/([a-z0-9]+)", url)
    return m.group(1) if m else ""


# ── posts.txt helpers ─────────────────────────────────────────────────────────
def load_tracked_urls() -> list:
    if not Path(POSTS_TXT).exists():
        return []
    with open(POSTS_TXT, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]

def add_url_to_posts_txt(url: str):
    existing = load_tracked_urls()
    if url not in existing:
        with open(POSTS_TXT, "a", encoding="utf-8") as f:
            f.write(url + "\n")


# ── LLM ───────────────────────────────────────────────────────────────────────
BOTS = {"AutoModerator", "reddit", "BotDefense", "Snooful"}

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
        "temperature": 0.3, "max_tokens": 300,
    }).encode()
    req = urllib.request.Request(
        f"{API_BASE}/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {API_KEY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
        raw = resp.read()
        if resp.info().get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw)["choices"][0]["message"]["content"].strip()

def analyze_one(comment: dict, post_title: str, retries=3) -> dict:
    prompt = ANALYZE_PROMPT.format(
        post_title=post_title, author=comment["author"],
        score=comment["score"], body=comment["body"],
    )
    for attempt in range(1, retries + 1):
        try:
            raw = call_llm(prompt).lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(raw)
        except Exception as e:
            if attempt < retries:
                time.sleep(attempt * 3)
    return {"summary":"（分析失败）","takeaway":"（分析失败）",
            "quality":0,"quality_reason":"error","highlight":False,"tags":[]}

def flatten_comments(comments: list) -> list:
    out = []
    def _f(cs):
        for c in cs:
            out.append(c)
            _f(c.get("replies", []))
    _f(comments)
    return out


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/posts", methods=["GET"])
def get_posts():
    data = load_data()
    # 附上 pending_urls（在 posts.txt 里但还没 import 的）
    tracked_ids = {p["id"] for p in data["posts"]}
    pending = [
        u for u in load_tracked_urls()
        if extract_post_id(u) not in tracked_ids
    ]
    return jsonify({"posts": data["posts"], "pending_urls": pending})


@app.route("/api/add", methods=["POST"])
def add_url():
    """
    只把 URL 写入 posts.txt，不连 Reddit。
    另一台电脑下次 git pull + 跑 fetch 脚本后会生成 batch，
    这台 Mac 再 import 就有数据了。
    """
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL 不能为空"}), 400
    if not re.search(r"reddit\.com/r/\w+/comments/", url):
        return jsonify({"error": "请输入有效的 Reddit 帖子 URL"}), 400

    post_id = extract_post_id(url)
    data    = load_data()
    if find_post(data["posts"], post_id):
        return jsonify({"error": "该帖子已在追踪列表中"}), 400

    tracked = load_tracked_urls()
    if url in tracked:
        return jsonify({"error": "该 URL 已在 posts.txt 中"}), 400

    add_url_to_posts_txt(url)
    return jsonify({"ok": True, "post_id": post_id, "url": url})


@app.route("/api/import", methods=["POST"])
def import_batches():
    """
    扫描 raw_comments/batch_*.json，
    只处理新 batch（或已有 batch 里未处理的 comment）。
    对新 comment 调 LLM，更新 data/posts.json。
    """
    data             = load_data()
    posts            = data["posts"]
    processed_batches = set(data.get("processed_batches", []))

    batch_files = sorted(RAW_DIR.glob("batch_*.json"))
    if not batch_files:
        return jsonify({"message": "raw_comments/ 下没有 batch 文件", "total_new": 0})

    total_new    = 0
    updated_posts = []

    for batch_path in batch_files:
        batch_name = batch_path.name

        with open(batch_path, encoding="utf-8") as f:
            batch = json.load(f)

        for item in batch:
            post_meta    = item["post"]
            post_id      = post_meta.get("id") or extract_post_id(post_meta.get("url",""))
            raw_comments = item.get("new_comments") or item.get("all_comments") or []

            existing = find_post(posts, post_id)
            is_first_import = existing is None

            if is_first_import:
                existing = {
                    **post_meta,
                    "added_at":     now_str(),
                    "last_checked": now_str(),
                    "has_new":      False,
                    "new_count":    0,
                    "seen_ids":     [],
                    "comments":     [],
                }
                posts.append(existing)
                # 同步写入 posts.txt
                if post_meta.get("url"):
                    add_url_to_posts_txt(post_meta["url"])

            # 找出还没处理的 comment
            existing_ids = {c["id"] for c in existing["comments"]}
            flat         = flatten_comments(raw_comments)
            new_raw      = [c for c in flat
                            if c.get("id") and c["id"] not in existing_ids
                            and c.get("author") not in BOTS
                            and c.get("body") not in ("", "[deleted]", "[removed]")]

            if not new_raw:
                continue

            # LLM 处理
            processed = []
            for c in new_raw:
                a = analyze_one(c, post_meta.get("title", ""))
                processed.append({**c, "analysis": a, "is_new": not is_first_import})
                time.sleep(0.4)

            existing["comments"]    += processed
            existing["seen_ids"]    += [c["id"] for c in new_raw]
            existing["last_checked"] = now_str()

            if not is_first_import and processed:
                existing["has_new"]   = True
                existing["new_count"] = existing.get("new_count", 0) + len(processed)
                updated_posts.append({
                    "post_id": post_id,
                    "title":   post_meta.get("title",""),
                    "new_count": len(processed),
                })

            total_new += len(processed)

        processed_batches.add(batch_name)

    data["processed_batches"] = list(processed_batches)
    save_data(data)

    msg = f"处理完成：{total_new} 条新评论" if total_new else "没有新内容（已全部处理过）"
    return jsonify({
        "message":      msg,
        "total_new":    total_new,
        "updated_posts": updated_posts,
    })


@app.route("/api/clear-new/<post_id>", methods=["POST"])
def clear_new(post_id):
    data = load_data()
    post = find_post(data["posts"], post_id)
    if post:
        post["has_new"]   = False
        post["new_count"] = 0
        for c in post.get("comments", []):
            c["is_new"] = False
        save_data(data)
    return jsonify({"ok": True})


@app.route("/api/delete/<post_id>", methods=["DELETE"])
def delete_post(post_id):
    data         = load_data()
    data["posts"] = [p for p in data["posts"] if p["id"] != post_id]
    save_data(data)
    # 同步从 posts.txt 移除
    urls = load_tracked_urls()
    urls = [u for u in urls if extract_post_id(u) != post_id]
    with open(POSTS_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(urls) + ("\n" if urls else ""))
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
