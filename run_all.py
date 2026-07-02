#!/usr/bin/env python3
"""
One-command pipeline:
  python3 run_all.py

Flow: fetch → process → generate HTML report
"""
import sys, subprocess
from pathlib import Path

def run(cmd):
    print(f"\n{'='*50}\n▶ {' '.join(cmd)}\n{'='*50}")
    r = subprocess.run(cmd, check=True)
    return r

if __name__ == "__main__":
    # Step 1: fetch new comments
    run([sys.executable, "fetch_reddit_playwright.py"])

    # Step 2: process latest batch with DeepSeek
    batch = sorted(Path("raw_comments").glob("batch_*.json"), reverse=True)[0]
    run([sys.executable, "process_comments.py", str(batch)])

    # Step 3: generate HTML report
    processed = sorted(Path("processed").glob("batch_*.json"), reverse=True)[0]
    run([sys.executable, "generate_report.py", str(processed)])

    report = sorted(Path("reports").glob("report_*.html"), reverse=True)[0]
    print(f"\n🎉 Done! Open your report:\n   open {report}")
