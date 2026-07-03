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

ROOT = Path(__file__).parent.parent   # backend/ → repo root
load_dotenv(ROOT / ".env")
app = Flask(__name__, template_folder=str(ROOT / "frontend"))

# ── config ────────────────────────────────────────────────────────────────────
DB_FILE   = str(ROOT / "data/research.db")
POSTS_TXT = str(ROOT / "posts.txt")
RAW_DIR   = ROOT / "raw_comments"
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
        # migrations
        cols = [r[1] for r in db.execute("PRAGMA table_info(posts)").fetchall()]
        if "category" not in cols:
            db.execute("ALTER TABLE posts ADD COLUMN category TEXT DEFAULT 'memory'")
        if "notes" not in cols:
            db.execute("ALTER TABLE posts ADD COLUMN notes TEXT")
        if "question_summary" not in cols:
            db.execute("ALTER TABLE posts ADD COLUMN question_summary TEXT")
        if "post_summary" not in cols:
            db.execute("ALTER TABLE posts ADD COLUMN post_summary TEXT")

def auto_classify(title: str) -> str:
    t = title.lower()
    if any(k in t for k in ["validate", "dimensions", "what a user already",
                              "what they already", "model what", "user's knowledge",
                              "skill", "capability", "capabilities", "tool use",
                              "never keeps", "agent skill"]):
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
SYSTEM_PROMPT = """你是 AI Agent Memory 领域的资深研究助手。
你的任务是帮助研究者快速理解 Reddit 社区对 agent memory 相关问题的真实反馈。
你的分析要有深度、有细节，不要为了简洁而牺牲信息量。"""

ANALYZE_PROMPT = """请分析以下 Reddit 评论，返回 JSON（直接输出，不加任何代码块标记）。

帖子标题：{post_title}
作者：u/{author}（score: {score}）
评论原文（英文）：
{body}

返回如下格式的 JSON：
{{
  "translation": "将原文完整翻译成中文，保留所有细节和技术术语，不要省略",
  "summary": "对这条评论内容的完整总结，可以是段落，也可以用 • 开头的 bullet points，把作者的核心观点、具体例子、技术细节都涵盖进来，不限字数",
  "takeaway": "这条评论对 AI Agent Memory 研究的价值与启发，请用 • 开头的 bullet points，每条是一个独立洞察或值得关注的点，不限条数不限字数",
  "quality": 评分整数1-5,
  "quality_reason": "评分理由，说明为什么给这个分",
  "highlight": true或false,
  "tags": ["标签1", "标签2"]
}}

评分标准：
5 = 有深度洞察 + 真实使用场景，对研究极有价值
4 = 观点新颖、有技术深度或产品洞察，值得重点关注
3 = 一般性观点，有一定参考价值
2 = 泛泛而谈，内容浅薄
1 = 无实质内容（bot 回复、纯广告、一句话废话）

highlight=true 的条件：质量>=4 且 有具体洞察或真实场景

标签从以下选 1-3 个：
#用户痛点 #技术方案 #产品反馈 #使用场景 #哲学思考 #隐私安全 #跨平台 #记忆管理 #个性化 #工作流 #反对意见 #类比参考"""

def call_llm(prompt: str) -> str:
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.3, "max_tokens": 1500,
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
            result = json.loads(raw)
            # 兼容：takeaway/summary 可能是 list（bullet point 数组）
            for field in ("takeaway", "summary"):
                if isinstance(result.get(field), list):
                    result[field] = "\n".join(result[field])
            return result
        except Exception:
            if attempt < retries:
                time.sleep(attempt * 3)
    return {"translation":"","summary":"（分析失败）","takeaway":"（分析失败）",
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
                "translation":    c["translation"] or "",
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
        "category":         row["category"] or "memory",
        "question_summary": row["question_summary"] or "",
        "post_summary":     row["post_summary"] or "",
        "notes":            row["notes"] or "",
        "has_new":          new_count > 0,
        "new_count":        new_count,
        "hl_count":         hl_count,
        "unanalyzed":       unanalyzed,
        "comments":         comments,
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
            "SELECT * FROM posts ORDER BY category, created_utc DESC"
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


@app.route("/api/import/markdown", methods=["POST"])
def import_markdown():
    """
    从 memory_kb_post/*.md 提取分析 → 写入 DB。
    比 LLM 快，用已有研究笔记作为 summary/takeaway。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "import_markdown", Path(__file__).parent / "import_markdown.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    md_dir = ROOT / "memory_kb_post"
    if not md_dir.exists():
        return jsonify({"error": "memory_kb_post/ 目录不存在"}), 400

    all_secs = []
    for f in sorted(md_dir.glob("*.md")):
        all_secs.extend(mod.parse_md(f))

    uc, up = mod.import_to_db(all_secs)
    with get_db() as db:
        analyzed   = db.execute("SELECT COUNT(*) FROM comments WHERE analyzed=1").fetchone()[0]
        unanalyzed = db.execute("SELECT COUNT(*) FROM comments WHERE analyzed=0").fetchone()[0]

    return jsonify({
        "message":    f"从研究笔记导入完成：{uc} 条评论，{up} 个帖子摘要",
        "updated":    uc,
        "unanalyzed": unanalyzed,
        "analyzed":   analyzed,
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
                    analyzed=1, translation=?, summary=?, takeaway=?, quality=?,
                    quality_reason=?, highlight=?, tags=?
                WHERE id=?
            """, (
                a.get("translation"), a.get("summary"), a.get("takeaway"), a.get("quality"),
                a.get("quality_reason"), 1 if a.get("highlight") else 0,
                tags_json, row["id"],
            ))
        done += 1
        time.sleep(0.4)

    return jsonify({"message": f"分析完成：{done} 条评论", "analyzed": done})


POST_SUMMARIZE_PROMPT = """你是 AI Agent Memory 领域的研究助手。
请根据以下帖子信息，生成两部分内容，返回 JSON（直接输出，不加代码块）：

帖子标题：{title}
帖子内容：{selftext}

该帖子下的评论摘要：
{comment_summaries}

返回格式：
{{
  "question_summary": "这个帖子在问什么？用2-3句中文说清楚：问题的背景是什么、核心疑问是什么、为什么这个问题值得关注",
  "post_summary": "综合所有评论，给出整体总结和 key takeaway，格式：\\n【整体观点】\\n一段话概括社区的主要看法\\n\\n【Key Takeaways】\\n• 第一条洞察\\n• 第二条洞察\\n• （更多条目，不限数量）"
}}"""


@app.route("/api/summarize/posts", methods=["POST"])
def summarize_posts():
    """
    为没有 question_summary 的帖子生成摘要。
    有 notes（研究笔记）的帖子直接复用 notes 作为 post_summary，只生成 question_summary。
    """
    with get_db() as db:
        posts = db.execute("""
            SELECT id, title, selftext, notes, question_summary
            FROM posts
            WHERE question_summary IS NULL OR question_summary = ''
        """).fetchall()

    if not posts:
        return jsonify({"message": "所有帖子已有摘要", "done": 0})

    done = 0
    for post in posts:
        pid   = post["id"]
        title = post["title"] or ""

        # 收集该帖子的 comment summaries
        with get_db() as db:
            c_rows = db.execute("""
                SELECT summary FROM comments
                WHERE post_id=? AND analyzed=1 AND summary IS NOT NULL AND summary != ''
                ORDER BY quality DESC, score DESC
                LIMIT 20
            """, (pid,)).fetchall()
        comment_summaries = "\n".join(f"• {r['summary']}" for r in c_rows) or "（暂无已分析评论）"

        selftext = (post["selftext"] or "").strip()[:500]

        prompt = POST_SUMMARIZE_PROMPT.format(
            title=title,
            selftext=selftext if selftext else "（无正文）",
            comment_summaries=comment_summaries,
        )

        try:
            raw = call_llm(prompt).lstrip("```json").lstrip("```").rstrip("```").strip()
            result = json.loads(raw)

            question_summary = result.get("question_summary", "")
            # 如果已有 notes（研究笔记），用 notes 作为 post_summary；否则用 LLM 生成的
            post_summary = post["notes"] or result.get("post_summary", "")

            with get_db() as db:
                db.execute(
                    "UPDATE posts SET question_summary=?, post_summary=? WHERE id=?",
                    (question_summary, post_summary, pid)
                )
            done += 1
        except Exception as e:
            print(f"  [!] {pid}: {e}")

        time.sleep(0.5)

    return jsonify({"message": f"生成完成：{done} 个帖子", "done": done})


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


@app.route("/api/export/html", methods=["GET"])
def export_html():
    """生成自包含静态 HTML 报告，可直接发送给他人查看。"""
    from flask import make_response
    from datetime import datetime, timezone

    with get_db() as db:
        posts_rows = db.execute(
            "SELECT * FROM posts ORDER BY category, created_utc DESC"
        ).fetchall()
        posts = [post_to_dict(db, r) for r in posts_rows]

    cat_labels = {"memory": "🧠 Memory", "knowledge_base": "📚 Knowledge Base", "skills": "🛠 Skills"}
    tag_colors = {
        "#用户痛点":"#ef4444","#技术方案":"#3b82f6","#产品反馈":"#8b5cf6",
        "#使用场景":"#10b981","#哲学思考":"#f59e0b","#隐私安全":"#6366f1",
        "#跨平台":"#14b8a6","#记忆管理":"#ec4899","#个性化":"#84cc16",
        "#工作流":"#f97316","#反对意见":"#dc2626","#类比参考":"#0ea5e9",
    }

    def esc(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    def ts(t):  return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d") if t else ""
    def stars(q): return "★"*q + "☆"*(5-q)
    def rich(t):  return esc(t or "").replace("\n","<br>")
    def tag_html(tags):
        h = ""
        for t in (tags or []):
            c = tag_colors.get(t, "#6b7280")
            h += f'<span style="padding:2px 7px;border-radius:10px;font-size:.7rem;font-weight:500;background:{c}20;color:{c};border:1px solid {c}40">{esc(t)}</span> '
        return h

    BLOCK = '<div style="margin:8px 0;padding:10px 12px;border-radius:6px;border:1px solid {b};background:{bg}"><div style="font-size:.68rem;font-weight:700;text-transform:uppercase;color:{tc};margin-bottom:4px">{label}</div><div style="font-size:.85rem;line-height:1.7">{body}</div></div>'

    def block(label, content, bg="white", border="#e2e8f0", tc="#94a3b8"):
        return BLOCK.format(b=border, bg=bg, tc=tc, label=label, body=rich(content)) if content else ""

    def render_comment(c, highlight=False):
        a    = c.get("analysis") or {}
        bg   = "#fffbeb" if highlight else "#f8fafc"
        bdr  = "#fcd34d" if highlight else "#e2e8f0"
        hl_s = '<span style="background:#fbbf24;color:white;font-size:.7rem;padding:2px 6px;border-radius:10px">⭐ 精选</span>' if highlight else ""
        tr   = block("中文翻译", a.get("translation",""))
        su   = block("评论总结", a.get("summary",""))
        tk   = block("Key Takeaway", a.get("takeaway",""), bg="#f0fdf4", border="#bbf7d0", tc="#16a34a")
        return (
            f'<div style="border:1px solid {bdr};border-radius:8px;padding:14px;margin-bottom:10px;background:{bg}">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">'
            f'<b style="color:#1e40af">u/{esc(c["author"])}</b>'
            f'<span style="color:#ef4444;font-size:.8rem">▲ {c["score"]}</span>'
            f'{hl_s}'
            f'<span style="color:#f59e0b">{stars(a.get("quality") or 0)}</span>'
            f'<span style="color:#64748b;font-size:.75rem">{ts(c.get("created_utc"))}</span>'
            f'<span style="color:#64748b;font-size:.75rem">{esc(a.get("quality_reason",""))}</span>'
            f'</div>'
            f'<div style="margin-bottom:8px">{tag_html(a.get("tags"))}</div>'
            f'{tr}{su}{tk}'
            f'<details><summary style="cursor:pointer;color:#6b7280;font-size:.8rem">查看英文原文</summary>'
            f'<div style="margin-top:6px;padding:10px;background:white;border-radius:6px;border:1px solid #e5e7eb;font-size:.82rem;white-space:pre-wrap">{esc(c["body"])}</div>'
            f'</details></div>'
        )

    def render_post(p):
        highlights    = [c for c in p["comments"] if c.get("analysis") and c["analysis"]["highlight"]]
        others        = [c for c in p["comments"] if not (c.get("analysis") and c["analysis"]["highlight"])]
        comments_html = ""
        if highlights:
            comments_html += '<div style="font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#94a3b8;margin:12px 0 8px">⭐ 精选</div>'
            comments_html += "".join(render_comment(c, True) for c in highlights)
        if others:
            comments_html += '<div style="font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#94a3b8;margin:12px 0 8px">全部评论</div>'
            comments_html += "".join(render_comment(c) for c in others)
        if not comments_html:
            comments_html = '<p style="color:#94a3b8;font-size:.85rem">暂无已分析评论</p>'

        hl_badge = ('<span style="background:#fef3c7;color:#d97706;padding:2px 8px;border-radius:10px;font-size:.72rem">⭐ '
                    + str(p["hl_count"]) + ' 精选</span>') if p["hl_count"] else ""
        q_block = block("💭 这帖在问什么", p.get("question_summary",""),
                        bg="#f0f9ff", border="#bae6fd", tc="#0ea5e9")
        s_block = block("📋 社区观点总结", p.get("post_summary",""),
                        bg="#fafafa", border="#e2e8f0", tc="#64748b")

        return (
            '<div style="background:white;border-radius:12px;margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,.06);overflow:hidden">'
            f'<div style="padding:18px 20px;border-bottom:1px solid #f1f5f9">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
            f'<span style="font-size:.75rem;color:#64748b;font-weight:500">r/{esc(p["subreddit"])}</span>'
            f'<span style="font-size:.75rem;color:#94a3b8">{ts(p.get("created_utc"))}</span>'
            f'<span style="font-size:.78rem;color:#64748b">▲ {p.get("score",0)}</span>'
            f'{hl_badge}</div>'
            f'<h3 style="font-size:1rem;font-weight:700;margin-bottom:6px">'
            f'<a href="{p["url"]}" target="_blank" style="color:#1e293b;text-decoration:none">{esc(p["title"])}</a></h3>'
            f'{q_block}{s_block}'
            f'</div>'
            f'<div style="padding:16px 20px">{comments_html}</div>'
            '</div>'
        )

    body = ""
    for cat in ["memory","knowledge_base","skills"]:
        cat_posts = [p for p in posts if p["category"]==cat and p["comments"]]
        if not cat_posts: continue
        body += f'<h2 style="font-size:1rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:1px;margin:32px 0 16px;padding-bottom:8px;border-bottom:2px solid #e2e8f0">{cat_labels[cat]}  <span style="font-weight:400;font-size:.85rem">({len(cat_posts)} 个帖子)</span></h2>'
        body += "".join(render_post(p) for p in cat_posts)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_posts = len([p for p in posts if p["comments"]])
    total_hl    = sum(p["hl_count"] for p in posts)

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Reddit Research Report · {date_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f1f5f9;color:#1e293b}}
a{{color:#3b82f6}}
</style>
</head><body>
<div style="background:linear-gradient(135deg,#0f172a,#1e3a5f);color:white;padding:32px 40px">
  <h1 style="font-size:1.6rem;font-weight:700;margin-bottom:8px">🧠 Reddit Research Report</h1>
  <p style="color:rgba(255,255,255,.6);font-size:.9rem">u/IndependenceGold5902 · {date_str}</p>
  <div style="display:flex;gap:20px;margin-top:16px">
    <div style="background:rgba(255,255,255,.1);border-radius:8px;padding:10px 18px;text-align:center">
      <div style="font-size:1.5rem;font-weight:700">{total_posts}</div>
      <div style="font-size:.75rem;opacity:.8">帖子</div>
    </div>
    <div style="background:rgba(255,255,255,.1);border-radius:8px;padding:10px 18px;text-align:center">
      <div style="font-size:1.5rem;font-weight:700">{sum(len(p['comments']) for p in posts)}</div>
      <div style="font-size:.75rem;opacity:.8">评论</div>
    </div>
    <div style="background:rgba(255,255,255,.1);border-radius:8px;padding:10px 18px;text-align:center">
      <div style="font-size:1.5rem;font-weight:700;color:#fbbf24">{total_hl}</div>
      <div style="font-size:.75rem;opacity:.8">⭐ 精选</div>
    </div>
  </div>
</div>
<div style="max-width:900px;margin:0 auto;padding:32px 20px">
{body}
</div>
<div style="text-align:center;color:#94a3b8;font-size:.78rem;padding:24px">Auto-generated · {date_str}</div>
</body></html>"""

    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Content-Disposition"] = f"attachment; filename=reddit-report-{date_str}.html"
    return resp


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)
