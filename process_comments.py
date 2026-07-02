#!/usr/bin/env python3
"""
Process raw Reddit comments with DeepSeek: per-comment summary / takeaway /
quality score / highlight / tags (all in Chinese).

Usage:
    python3 process_comments.py            # only process comments not seen before
    python3 process_comments.py --full     # reprocess everything

Requires network access to DEEPSEEK_API_BASE (see config.py / .env).
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI

from config import DEEPSEEK_API_KEY, DEEPSEEK_API_BASE, DEEPSEEK_MODEL

RAW_DIR = Path("raw_comments")
PROCESSED_DIR = Path("processed")
SEEN_FILE = PROCESSED_DIR / "seen_llm.json"
MANIFEST_FILE = PROCESSED_DIR / "last_run_new_ids.json"

SYSTEM_PROMPT = """你是一名 AI Agent Memory 领域的研究助理，帮用户整理 Reddit 上收集到的评论。
针对给你的每一条评论，输出严格的 JSON（不要多余文字），字段如下：
{
  "summary": "一句话中文概括这条评论说了什么",
  "takeaway": "这条评论对 agent memory 研究的核心价值点（中文，没有价值就写“无明显价值”）",
  "quality": 1-5 的整数分,
  "quality_reason": "打这个分的一句话理由（中文）",
  "tags": ["从：用户痛点/技术方案/产品反馈/观点分歧/案例分享 中选0-2个，也可以自己起贴切的标签"]
}

评分标准（quality）：
- 5分：有具体真实使用场景 + 提出了新颖观点或有力反驳，技术/产品洞察很深
- 4分：满足上面标准中的大部分，有明显参考价值
- 3分：有一定信息量，但比较泛泛
- 1-2分：灌水、跑题、纯情绪化吐槽，没有实质内容
"""


def load_client() -> OpenAI:
    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_API_BASE)


def flatten_comments(comments: list, out: list, depth: int = 0):
    for c in comments:
        out.append({
            "id": c.get("id", ""),
            "author": c.get("author", "unknown"),
            "body": c.get("body", ""),
            "score": c.get("score", 0),
            "depth": c.get("depth", depth),
            "created_utc": c.get("created_utc"),
        })
        flatten_comments(c.get("replies", []), out, depth + 1)


def load_seen() -> dict:
    if SEEN_FILE.exists():
        with open(SEEN_FILE) as f:
            return json.load(f)
    return {}


def save_seen(seen: dict):
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2, ensure_ascii=False)


def load_existing_processed(post_id: str) -> dict:
    path = PROCESSED_DIR / f"{post_id}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def call_llm(client: OpenAI, comment: dict, post_title: str, retries: int = 3) -> dict:
    user_content = (
        f"帖子标题：{post_title}\n"
        f"评论作者：u/{comment['author']}\n"
        f"评论内容：{comment['body']}"
    )
    for attempt in range(1, retries + 1):
        try:
            resp = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.3,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            quality = int(data.get("quality", 1))
            return {
                "summary": data.get("summary", ""),
                "takeaway": data.get("takeaway", ""),
                "quality": quality,
                "quality_reason": data.get("quality_reason", ""),
                "highlight": quality >= 4,
                "tags": data.get("tags", []),
            }
        except Exception as e:
            print(f"    [!] LLM call failed (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
    # Fallback so the pipeline never crashes on a single bad comment
    return {
        "summary": "(处理失败)",
        "takeaway": "",
        "quality": 1,
        "quality_reason": "LLM 调用失败",
        "highlight": False,
        "tags": [],
    }


def process_post(client: OpenAI, raw_path: Path, seen: dict, full: bool, manifest: dict) -> None:
    with open(raw_path) as f:
        raw = json.load(f)

    post = raw["post"]
    post_id = post["id"]

    flat = []
    flatten_comments(raw.get("comments", []), flat)

    existing = load_existing_processed(post_id)
    existing_comments = {c["id"]: c for c in existing["comments"]} if existing and not full else {}
    seen_ids = set(seen.get(post_id, [])) if not full else set()

    new_ids = []
    processed_comments = []
    run_ts = datetime.now(timezone.utc).isoformat()

    print(f"  [{post_id}] {post['title'][:50]}... ({len(flat)} comments total)")

    for c in flat:
        if c["id"] in existing_comments:
            processed_comments.append(existing_comments[c["id"]])
            continue
        if not full and c["id"] in seen_ids:
            continue

        result = call_llm(client, c, post["title"])
        merged = {**c, **result, "processed_at": run_ts}
        processed_comments.append(merged)
        new_ids.append(c["id"])
        seen_ids.add(c["id"])
        time.sleep(0.5)  # be polite to the API

    if new_ids:
        print(f"    -> processed {len(new_ids)} new comment(s)")
    else:
        print(f"    -> no new comments")

    out = {
        "post": post,
        "comments": processed_comments,
        "last_processed_at": run_ts,
    }
    PROCESSED_DIR.mkdir(exist_ok=True)
    with open(PROCESSED_DIR / f"{post_id}.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    seen[post_id] = list(seen_ids)
    manifest[post_id] = new_ids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="reprocess all comments, ignore seen cache")
    args = parser.parse_args()

    if not RAW_DIR.exists():
        print(f"[!] {RAW_DIR}/ not found — run fetch_reddit_playwright.py first")
        sys.exit(1)

    raw_files = sorted(p for p in RAW_DIR.glob("*.json") if not p.name.startswith("batch_"))
    if not raw_files:
        print(f"[!] No post files found in {RAW_DIR}/")
        sys.exit(1)

    client = load_client()
    seen = load_seen()
    manifest = {}

    print(f"[+] Processing {len(raw_files)} post(s){' (full reprocess)' if args.full else ''}...\n")

    for raw_path in raw_files:
        process_post(client, raw_path, seen, args.full, manifest)

    PROCESSED_DIR.mkdir(exist_ok=True)
    save_seen(seen)
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    total_new = sum(len(v) for v in manifest.values())
    print(f"\n[+] Done. {total_new} new comment(s) processed across {len(raw_files)} post(s).")
    print(f"[+] Output in {PROCESSED_DIR}/")


if __name__ == "__main__":
    main()
