#!/usr/bin/env python3
"""
把 memory_kb_post/*.md 里的分析导入 SQLite。
用法：python3 import_markdown.py
"""
import re, sqlite3
from pathlib import Path

ROOT    = Path(__file__).parent.parent   # backend/ → repo root
MD_DIR  = ROOT / "memory_kb_post"
DB_FILE = str(ROOT / "data/research.db")

# ── text helpers ──────────────────────────────────────────────────────────────
def clean(t: str) -> str:
    t = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return " ".join(t.split()).strip()

def short(t: str, n=60) -> str:
    t = clean(t)
    for sep in ["。","；","！","？",".","; "]:
        i = t.find(sep)
        if 0 < i < n: return t[:i+1]
    return t[:n] + ("…" if len(t) > n else "")

# ── per-file parser ───────────────────────────────────────────────────────────
def parse_md(path: Path) -> list[dict]:
    """返回 [{post_ids, takeaways, comments:[{author,text,highlight}]}]"""
    text = path.read_text(encoding="utf-8")

    # 所有 Reddit post IDs（全文）
    def find_ids(src):
        return list(dict.fromkeys(
            re.findall(r"reddit\.com/r/[^/]+/comments/([a-z0-9]+)", src)
        ))
    all_ids = find_ids(text)

    # Summary Table: 行号 → post_ids
    table_map: dict[int, list[str]] = {}
    for row in re.finditer(r"^\|\s*(\d+)\s*\|(.+)$", text, re.MULTILINE):
        ids = find_ids(row.group(0))
        if ids:
            table_map[int(row.group(1))] = ids

    # Key Takeaways（全局）
    km = re.search(r"##\s*[✅]?\s*Key Takeaways\s*\n(.*?)(?:\n##|\Z)", text, re.DOTALL)
    global_kt = clean(km.group(1)) if km else ""

    # 提取 **u/author** ⭐? 块
    def get_comments(src):
        out = []
        for m in re.finditer(r"\*\*u/([^*\n]+?)\*\*\s*(⭐)?\s*\n((?:>.*\n?)+)", src):
            author = m.group(1).strip()
            hi     = bool(m.group(2))
            body   = re.sub(r"^>\s?", "", m.group(3), flags=re.MULTILINE)
            body   = " ".join(body.split())
            if author and body.strip():
                out.append({"author": author, "text": body, "highlight": hi})
        return out

    # 找 Q sections
    qs = list(re.finditer(r"\n###\s+Q(\d+):", text))
    if not qs:
        # 没有 Q section，把全文作为一个 section
        comments = get_comments(text)
        return [{"post_ids": all_ids, "takeaways": global_kt, "comments": comments}] if all_ids else []

    sections = []
    for i, q in enumerate(qs):
        qnum  = int(q.group(1))
        start = q.start()
        end   = qs[i+1].start() if i+1 < len(qs) else len(text)
        part  = text[start:end]

        # post_ids: section 内 URL > table_map > all_ids
        ids = find_ids(part) or table_map.get(qnum, []) or all_ids
        if not ids:
            continue

        # local Key Takeaways
        lk = re.search(r"##\s*[✅]?\s*Key Takeaways\s*\n(.*?)(?:\n##|\Z)", part, re.DOTALL)
        kt = clean(lk.group(1)) if lk else global_kt

        sections.append({"post_ids": ids, "takeaways": kt, "comments": get_comments(part)})

    return sections

# ── write to DB ───────────────────────────────────────────────────────────────
def import_to_db(sections):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row

    # migration: add notes if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()]
    if "notes" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN notes TEXT")
        conn.commit()

    uc = up = 0
    for sec in sections:
        # posts notes
        for pid in sec["post_ids"]:
            if not conn.execute("SELECT 1 FROM posts WHERE id=?", (pid,)).fetchone():
                continue
            if sec["takeaways"]:
                conn.execute("UPDATE posts SET notes=? WHERE id=?",
                             (sec["takeaways"], pid))
                up += 1

        # comments
        for pid in sec["post_ids"]:
            for c in sec["comments"]:
                rows = conn.execute(
                    "SELECT id FROM comments WHERE post_id=? AND author=?",
                    (pid, c["author"])
                ).fetchall()
                if not rows:
                    continue

                s = short(c["text"])
                t = clean(c["text"])[:250]
                q = 5 if c["highlight"] else 3
                r = "研究笔记重点标注" if c["highlight"] else "来自研究笔记"

                for row in rows:
                    conn.execute("""
                        UPDATE comments
                        SET analyzed=1, summary=?, takeaway=?,
                            quality=?, quality_reason=?, highlight=?, tags=?
                        WHERE id=?
                    """, (s, t, q, r, int(c["highlight"]), "[]", row["id"]))
                    uc += 1

    conn.commit()
    conn.close()
    return uc, up

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    all_secs = []
    for f in sorted(MD_DIR.glob("*.md")):
        secs = parse_md(f)
        nc   = sum(len(s["comments"]) for s in secs)
        print(f"{f.name}: {len(secs)} sections, {nc} comments")
        all_secs.extend(secs)

    print(f"\n导入中…")
    uc, up = import_to_db(all_secs)
    print(f"更新: {uc} 条评论分析, {up} 个帖子 notes")

    conn = sqlite3.connect(DB_FILE)
    analyzed = conn.execute("SELECT COUNT(*) FROM comments WHERE analyzed=1").fetchone()[0]
    total    = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    print(f"DB: {analyzed}/{total} 条已分析")
    conn.close()

if __name__ == "__main__":
    main()
