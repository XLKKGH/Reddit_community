#!/usr/bin/env python3
"""
Step 2: Process raw comments with DeepSeek LLM.
Reads:  raw_comments/batch_YYYY-MM-DD.json
Writes: processed/batch_YYYY-MM-DD.json
"""

import json, os, sys, time, urllib.request, urllib.error, gzip, ssl
from pathlib import Path
from dotenv import load_dotenv

# 跳过 SSL 验证（公司网络代理）
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

load_dotenv()

API_KEY  = os.getenv("DEEPSEEK_API_KEY")
API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
MODEL    = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
OUTPUT_DIR = "processed"

SYSTEM_PROMPT = """你是一个 AI Agent Memory 领域的研究助手。
分析 Reddit 上用户对 agent memory 相关话题的评论，提炼研究价值。"""

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


def call_deepseek(prompt):
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 300,
    }).encode("utf-8")
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


def analyze_comment(comment, post_title):
    prompt = ANALYZE_PROMPT.format(
        post_title=post_title, author=comment["author"],
        score=comment["score"], body=comment["body"],
    )
    try:
        raw = call_deepseek(prompt).lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"    [!] error: {e}")
        return {"summary":"（失败）","takeaway":"（失败）","quality":0,"quality_reason":"error","highlight":False,"tags":[]}


BOTS = {"AutoModerator", "reddit", "BotDefense"}

def process_batch(batch_path):
    with open(batch_path) as f:
        batch = json.load(f)

    results = []
    total = sum(len(x["new_comments"]) for x in batch)
    done  = 0

    for item in batch:
        post, new_comments = item["post"], item["new_comments"]
        print(f"\n📄 r/{post['subreddit']} — {post['title'][:55]}")

        processed = []
        for c in new_comments:
            if c["author"] in BOTS:
                print(f"   ⏭  skip bot: u/{c['author']}")
                continue
            done += 1
            print(f"   [{done}/{total}] u/{c['author']} ...", end=" ", flush=True)
            a = analyze_comment(c, post["title"])
            print(f"{'⭐' if a.get('highlight') else '  '} {a.get('quality')}/5 — {a.get('summary','')[:40]}")
            processed.append({**c, "analysis": a})
            time.sleep(0.5)

        processed.sort(key=lambda x: x["analysis"].get("quality", 0), reverse=True)
        results.append({
            "post": post,
            "processed_comments": processed,
            "total_comments":  len(item["all_comments"]),
            "new_count":       len(processed),
            "highlight_count": sum(1 for c in processed if c["analysis"].get("highlight")),
        })

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = str(Path(OUTPUT_DIR) / Path(batch_path).name)
    with open(out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n✅ Saved → {out}")
    return out


if __name__ == "__main__":
    bp = sys.argv[1] if len(sys.argv) > 1 else str(sorted(Path("raw_comments").glob("batch_*.json"), reverse=True)[0])
    print(f"[+] Processing: {bp}")
    process_batch(bp)
