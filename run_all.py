#!/usr/bin/env python3
"""
One-command pipeline: fetch new comments -> LLM process -> generate HTML report.

Usage:
    python3 run_all.py            # fetch + incremental process + report
    python3 run_all.py --no-fetch # skip fetching, just process + report
    python3 run_all.py --full     # reprocess all comments (ignores seen cache)
"""

import argparse
import subprocess
import sys


def run(cmd: list) -> None:
    print(f"\n$ {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[!] Command failed: {' '.join(cmd)}")
        sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-fetch", action="store_true", help="skip fetch_reddit_playwright.py")
    parser.add_argument("--full", action="store_true", help="reprocess all comments with the LLM")
    args = parser.parse_args()

    py = sys.executable

    if not args.no_fetch:
        run([py, "fetch_reddit_playwright.py"])

    process_cmd = [py, "process_comments.py"]
    if args.full:
        process_cmd.append("--full")
    run(process_cmd)

    run([py, "generate_report.py"])


if __name__ == "__main__":
    main()
