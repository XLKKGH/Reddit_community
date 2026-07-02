#!/usr/bin/env python3
"""
Reddit Research Tracker — Flask + SQLite

架构：
  另一台电脑：fetch → raw_comments/*.json → git push
  这台 Mac：  git pull → 「导入 DB」→ 「LLM 分析」→ 网页展示
"""

import json, os, re, time, gzip, ssl, sqlite3, urllib.request
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ── config ────────────────────────────────────────────────────────────────────
DB_FILE   = "data/research.db"
POSTS_TXT = "posts.txt"
RAW_DIR   = Path("raw_comments")
API_KEY   = os.getenv("DEEPSEEK_API_KEY")
API_BASE  = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
MODEL     = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode    = ssl.CERT_NONE
# ─────────────────────────────────────────────────────────────────────────────


# ── SQLite helpers ────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    Path(DB_FILE).parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id           TEXT PRIMARY KEY,
            title        TEXT,
            subreddit    TEXT,
            category     TEXT DEFAULT 'memory',
            url          TEXT,
            score        INTEGER,
            selftext     TEXT,
            num_comments INTEGER,
            created_utc  REAL,
            added_at     TEXT,
            last_checked TEXT
        );

        CREATE TABLE IF NOT EXISTS comments (
            id             TEXT PRIMARY KEY,
            post_id        TEXT NOT NULL,
            author         TEXT,
            body           TEXT,
            score          INTEGER,
            depth          INTEGER DEFAULT 0,
            created_utc    REAL,
            is_new         INTEGER DEFAULT 0,
            analyzed       INTEGER DEFAULT 0,
            summary        TEXT,
            takeaway       TEXT,
            quality        INTEGER,
            quality_reason TEXT,
            highlight      INTEGER DEFAULT 0,
            tags           TEXT,
            FOREIGN KEY (post_id) REFERENCES posts(id)
        );

        CREATE TABLE IF NOT EXISTS imported_files (
            filename    TEXT PRIMARY KEY,
            imported_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_comments_post   ON comments(post_id);
        CREATE INDEX IF NOT EXISTS idx_comments_unanalyzed ON comments(analyzed);
        """)
        # migration: add category if missing
        cols = [r[1] for r in db.execute("PRAGMA table_info(posts)").fetchall()]
        if "category" not in cols:
            db.execute("ALTER TABLE posts ADD COLUMN category TEXT DEFAULT 'memory'")

def auto_classify(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["validate", "dimensions", "what a user already",
                              "what they already", "model what", "user's knowledge"]):
        return "skills"
    if any(k in t for k in ["knowledge base", "knowledge graph", "incremental update",
                              "combining memory and knowledge"]):
        return "knowledge_base"
    return "memory"

def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
# ─────────────────────────────────────────────────────────────────────────────


# ── posts.txt helpers ─────────────────────────────────────────────────────────
def load_tracked_urls() -> list:
    if not Path(POSTS_TXT).exists():
        return []
    with open(POSTS_TXT, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]

def add_url_to_posts_txt(url: str):
    if url not in load_tracked_urls():
        with open(POSTS_TXT, "a", encoding="utf-8") as f:
            f.write(url + "\n")

def extract_post_id(url: str) -> str:
    m = re.search(r"/comments/([a-z0-9]+)", url)
    return m.group(1) if m else ""
# ─────────────────────────────────────────────────────────────────────────────


# ── JSON → DB import ──────────────────────────────────────────────────────────
BOTS = {"AutoModerator", "reddit", "BotDefense", "Snooful"}

def flatten_comments(comments: list) -> list:
    out = []
    def _f(cs):
        for c in cs:
            out.append(c)
            _f(c.get("replies", []))
    _f(comments)
    return out

def import_item(db, item: dict) -> int:
    """Insert one post + its comments into DB. Returns number of new comments added."""
    post_meta = item["post"]
    post_id   = post_meta.get("id") or extract_post_id(post_meta.get("url", ""))
    if not post_id:
        return 0

    raw = item.get("new_comments") or item.get("all_comments") or item.get("comments") or []

    # Upsert post (keep existing added_at if already there)
    existing = db.execute("SELECT added_at FROM posts WHERE id=?", (post_id,)).fetchone()
    added_at  = existing["added_at"] if existing else now_str()
    title = post_meta.get("title", "")
    db.execute("""
        INSERT INTO posts (id, title, subreddit, url, score, selftext, num_comments,
                           created_utc, added_at, last_checked, category)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            score=excluded.score, num_comments=excluded.num_comments,
            last_checked=excluded.last_checked
    """, (
        post_id, title,
        post_meta.get("subreddit", ""),
        post_meta.get("url", ""),
        post_meta.get("score", 0),
        post_meta.get("selftext", ""),
        post_meta.get("num_comments", 0),
        post_meta.get("created_utc"),
        added_at, now_str(),
        auto_classify(title),
    ))

    if post_meta.get("url"):
        add_url_to_posts_txt(post_meta["url"])

    # Check if this post already had comments (determines is_new flag)
    has_existing = db.execute(
        "SELECT 1 FROM comments WHERE post_id=? LIMIT 1", (post_id,)
    ).fetchone() is not None

    flat    = flatten_comments(raw)
    new_cnt = 0
    for c in flat:
        cid    = c.get("id", "")
        body   = c.get("body", "").strip()
        author = c.get("author", "unknown")

        if not cid or not body or author in BOTS:
            continue
        if body in ("[deleted]", "[removed]"):
            continue

        rows = db.execute("SELECT 1 FROM comments WHERE id=?", (cid,)).fetchone()
        if rows:
            continue  # already imported

        is_new = 1 if has_existing else 0
        db.execute("""
            INSERT INTO comments (id, post_id, author, body, score, depth,
                                  created_utc, is_new, analyzed)
            VALUES (?,?,?,?,?,?,?,?,0)
        """, (
            cid, post_id, author, body,
            c.get("score", 0), c.get("depth", 0),
            c.get("created_utc"),
            is_new,
        ))
        new_cnt += 1

    return new_cnt
# ─────────────────────────────────────────────────────────────────────────────


# ── LLM ───────────────────────────────────────────────────────────────────────
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

def analyze_one(comment: dict, post_title: str, retries: int = 3) -> dict:
    prompt = ANALYZE_PROMPT.format(
        post_title=post_title,
        author=comment["author"],
        score=comment["score"],
        body=comment["body"],
    )
    for attempt in range(1, retries + 1):
        try:
            raw = call_llm(prompt).lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(raw)
        except Exception:
            if attempt < retries:
                time.sleep(attempt * 3)
    return {"summary":"（分析失败）","takeaway":"（分析失败）",
            "quality":0,"quality_reason":"error","highlight":False,"tags":[]}
# ─────────────────────────────────────────────────────────────────────────────


# ── API data helpers ──────────────────────────────────────────────────────────
def post_to_dict(db, row) -> dict:
    """Build full post dict with comments for API response."""
    post_id = row["id"]
    comments_rows = db.execute("""
        SELECT * FROM comments WHERE post_id=?
        ORDER BY is_new DESC, quality DESC, score DESC
    """, (post_id,)).fetchall()

    comments = []
    for c in comments_rows:
        tags = json.loads(c["tags"]) if c["tags"] else []
        comments.append({
            "id":          c["id"],
            "author":      c["author"],
            "body":        c["body"],
            "score":       c["score"],
            "depth":       c["depth"],
            "created_utc": c["created_utc"],
            "is_new":      bool(c["is_new"]),
            "analyzed":    bool(c["analyzed"]),
            "analysis": {
                "summary":        c["summary"] or "",
                "takeaway":       c["takeaway"] or "",
                "quality":        c["quality"] or 0,
                "quality_reason": c["quality_reason"] or "",
                "highlight":      bool(c["highlight"]),
                "tags":           tags,
            } if c["analyzed"] else None,
        })

    new_count = sum(1 for c in comments if c["is_new"])
    hl_count  = sum(1 for c in comments if c.get("analysis") and c["analysis"]["highlight"])
    unanalyzed = sum(1 for c in comments if not c["analyzed"])

    return {
        "id":           row["id"],
        "title":        row["title"],
        "subreddit":    row["subreddit"],
        "url":          row["url"],
        "score":        row["score"],
        "num_comments": row["num_comments"],
        "created_utc":  row["created_utc"],
        "added_at":     row["added_at"],
        "last_checked": row["last_checked"],
        "category":     row["category"] or "memory",
        "has_new":      new_count > 0,
        "new_count":    new_count,
        "hl_count":     hl_count,
        "unanalyzed":   unanalyzed,
        "comments":     comments,
    }
# ─────────────────────────────────────────────────────────────────────────────


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/posts", methods=["GET"])
def get_posts():
    with get_db() as db:
        posts_rows = db.execute(
            "SELECT * FROM posts ORDER BY category, created_utc ASC"
        ).fetchall()
        posts = [post_to_dict(db, r) for r in posts_rows]

    tracked_ids = {p["id"] for p in posts}
    pending     = [u for u in load_tracked_urls()
                   if extract_post_id(u) not in tracked_ids]

    total_unanalyzed = sum(p["unanalyzed"] for p in posts)
    return jsonify({"posts": posts, "pending_urls": pending,
                    "total_unanalyzed": total_unanalyzed})


@app.route("/api/add", methods=["POST"])
def add_url():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL 不能为空"}), 400
    if not re.search(r"reddit\.com/r/\w+/comments/", url):
        return jsonify({"error": "请输入有效的 Reddit 帖子 URL"}), 400

    post_id = extract_post_id(url)
    with get_db() as db:
        if db.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
            return jsonify({"error": "该帖子已在追踪列表中"}), 400

    if url in load_tracked_urls():
        return jsonify({"error": "该 URL 已在 posts.txt 中"}), 400

    add_url_to_posts_txt(url)
    return jsonify({"ok": True, "post_id": post_id})


@app.route("/api/import", methods=["POST"])
def import_json():
    """
    扫描 raw_comments/*.json → 写入 SQLite（增量，已导入的跳过）。
    快速操作，不调 LLM。
    """
    all_files = sorted(RAW_DIR.glob("*.json")) if RAW_DIR.exists() else []
    if not all_files:
        return jsonify({"message": "raw_comments/ 下没有文件", "new_comments": 0})

    total_new = 0
    new_posts  = 0

    with get_db() as db:
        imported = {r[0] for r in db.execute("SELECT filename FROM imported_files").fetchall()}

        for fpath in all_files:
            fname = fpath.name
            if fname in imported:
                continue  # already done

            with open(fpath, encoding="utf-8") as f:
                content = json.load(f)

            items = content if isinstance(content, list) else [content]
            for item in items:
                if "post" not in item:
                    continue
                pid     = item["post"].get("id","")
                is_first = not db.execute("SELECT 1 FROM posts WHERE id=?", (pid,)).fetchone()
                cnt     = import_item(db, item)
                total_new += cnt
                if is_first and cnt > 0:
                    new_posts += 1

            db.execute("INSERT OR IGNORE INTO imported_files VALUES (?,?)",
                       (fname, now_str()))

    unanalyzed = 0
    with get_db() as db:
        unanalyzed = db.execute(
            "SELECT COUNT(*) FROM comments WHERE analyzed=0"
        ).fetchone()[0]

    return jsonify({
        "message":    f"导入完成：新增 {total_new} 条评论，{new_posts} 个新帖子",
        "new_comments": total_new,
        "new_posts":    new_posts,
        "unanalyzed":   unanalyzed,
    })


@app.route("/api/analyze", methods=["POST"])
def analyze():
    """
    SELECT analyzed=0 → LLM → UPDATE。
    慢操作，仅处理未分析的。
    """
    with get_db() as db:
        rows = db.execute("""
            SELECT c.*, p.title as post_title
            FROM comments c JOIN posts p ON c.post_id = p.id
            WHERE c.analyzed = 0
            ORDER BY c.is_new DESC, c.score DESC
        """).fetchall()

    if not rows:
        return jsonify({"message": "没有待分析的评论", "analyzed": 0})

    done = 0
    for row in rows:
        a = analyze_one(
            {"author": row["author"], "score": row["score"], "body": row["body"]},
            row["post_title"],
        )
        tags_json = json.dumps(a.get("tags", []), ensure_ascii=False)
        with get_db() as db:
            db.execute("""
                UPDATE comments SET
                    analyzed=1, summary=?, takeaway=?, quality=?,
                    quality_reason=?, highlight=?, tags=?
                WHERE id=?
            """, (
                a.get("summary"), a.get("takeaway"), a.get("quality"),
                a.get("quality_reason"), 1 if a.get("highlight") else 0,
                tags_json, row["id"],
            ))
        done += 1
        time.sleep(0.4)

    return jsonify({"message": f"分析完成：{done} 条评论", "analyzed": done})


@app.route("/api/post/<post_id>/category", methods=["PUT"])
def set_category(post_id):
    cat = request.json.get("category", "").strip()
    if cat not in ("memory", "knowledge_base", "skills"):
        return jsonify({"error": "category must be memory / knowledge_base / skills"}), 400
    with get_db() as db:
        db.execute("UPDATE posts SET category=? WHERE id=?", (cat, post_id))
    return jsonify({"ok": True})


@app.route("/api/clear-new/<post_id>", methods=["POST"])
def clear_new(post_id):
    with get_db() as db:
        db.execute("UPDATE comments SET is_new=0 WHERE post_id=?", (post_id,))
    return jsonify({"ok": True})


@app.route("/api/delete/<post_id>", methods=["DELETE"])
def delete_post(post_id):
    with get_db() as db:
        db.execute("DELETE FROM comments WHERE post_id=?", (post_id,))
        db.execute("DELETE FROM posts WHERE id=?", (post_id,))
    # 从 posts.txt 移除
    urls = [u for u in load_tracked_urls() if extract_post_id(u) != post_id]
    with open(POSTS_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(urls) + ("\n" if urls else ""))
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
