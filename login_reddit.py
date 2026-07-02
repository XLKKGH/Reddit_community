#!/usr/bin/env python3
"""
Run this ONCE to log into Reddit and save cookies.
After this, fetch_reddit_playwright.py will reuse the saved session.

Usage:
    python3 login_reddit.py
"""

import json
from playwright.sync_api import sync_playwright

COOKIES_FILE = "reddit_cookies.json"


def main():
    print("[*] Opening browser for Reddit login...")
    print("[*] Log in manually, then press Enter here to save cookies.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)  # use system Chrome
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.goto("https://www.reddit.com/login", wait_until="domcontentloaded")

        print("[*] Browser opened. Please:")
        print("    1. Log in to your Reddit account")
        print("    2. Make sure you're on the Reddit homepage (not login page)")
        print("    3. Come back here and press Enter\n")

        input("Press Enter after you've logged in successfully >>> ")

        # Save cookies
        cookies = context.cookies()
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies, f, indent=2)

        browser.close()

    print(f"\n[✓] Cookies saved to {COOKIES_FILE}")
    print("[✓] You can now run: python3 fetch_reddit_playwright.py")


if __name__ == "__main__":
    main()
