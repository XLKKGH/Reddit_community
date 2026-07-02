#!/usr/bin/env python3
"""
Run this ONCE to export Reddit cookies from your existing Chrome session.
No login needed — reuses the Chrome profile you're already logged into.

Usage:
    python3 login_reddit.py
"""

import json
import os
import sys
import shutil
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

COOKIES_FILE = "reddit_cookies.json"


def get_chrome_user_data_dir() -> str:
    """Find the default Chrome user data directory for this OS."""
    if sys.platform == "darwin":
        return str(Path.home() / "Library/Application Support/Google/Chrome")
    elif sys.platform == "win32":
        return str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data")
    else:  # Linux
        return str(Path.home() / ".config/google-chrome")


def main():
    chrome_data = get_chrome_user_data_dir()

    if not Path(chrome_data).exists():
        print(f"[!] Chrome profile not found at: {chrome_data}")
        print("[!] Make sure Google Chrome is installed and has been run at least once.")
        sys.exit(1)

    print(f"[*] Found Chrome profile: {chrome_data}")
    print("[*] NOTE: Please close all Chrome windows before continuing!")
    print("    (Chrome can't share its profile with another process)\n")
    input("Close Chrome, then press Enter to continue >>> ")

    # Copy profile to a temp dir so we don't corrupt the original
    tmp_dir = tempfile.mkdtemp(prefix="chrome_tmp_")
    tmp_profile = os.path.join(tmp_dir, "profile")
    print(f"[*] Copying profile to temp dir (this may take a few seconds)...")

    # Only copy Default profile folder (much faster than full copy)
    src_default = os.path.join(chrome_data, "Default")
    dst_default = os.path.join(tmp_profile, "Default")
    try:
        shutil.copytree(src_default, dst_default,
                        ignore=shutil.ignore_patterns("Cache", "Code Cache",
                                                       "GPUCache", "Service Worker"))
    except Exception as e:
        print(f"[!] Profile copy failed: {e}")
        print("[!] Falling back to manual login mode...")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        manual_login()
        return

    print("[*] Launching Chrome with your existing session...")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=tmp_profile,
            channel="chrome",
            headless=False,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        page = context.new_page()
        page.goto("https://www.reddit.com", wait_until="domcontentloaded", timeout=20000)

        print("\n[*] Browser opened. Check that you're logged into Reddit.")
        print("    If not logged in, log in now, then come back and press Enter.")
        input("\nPress Enter to save cookies >>> ")

        cookies = context.cookies()
        reddit_cookies = [c for c in cookies if "reddit.com" in c.get("domain", "")]

        with open(COOKIES_FILE, "w") as f:
            json.dump(reddit_cookies, f, indent=2)

        context.close()

    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"\n[✓] {len(reddit_cookies)} cookies saved to {COOKIES_FILE}")
    print("[✓] You can now run: python3 fetch_reddit_playwright.py")


def manual_login():
    """Fallback: open a fresh browser and let user log in manually."""
    print("[*] Opening fresh Chrome window — please log in to Reddit manually.")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36",
        )
        # Hide webdriver flag
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()
        page.goto("https://www.reddit.com/login", wait_until="domcontentloaded")

        input("\nLog in to Reddit, then press Enter here >>> ")

        cookies = context.cookies()
        reddit_cookies = [c for c in cookies if "reddit.com" in c.get("domain", "")]
        with open(COOKIES_FILE, "w") as f:
            json.dump(reddit_cookies, f, indent=2)

        browser.close()

    print(f"\n[✓] {len(reddit_cookies)} cookies saved to {COOKIES_FILE}")
    print("[✓] You can now run: python3 fetch_reddit_playwright.py")


if __name__ == "__main__":
    main()
