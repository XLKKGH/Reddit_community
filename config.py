#!/usr/bin/env python3
"""Loads DeepSeek API settings from .env."""

import os
from dotenv import load_dotenv

load_dotenv()

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_API_BASE = os.environ.get("DEEPSEEK_API_BASE")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash-tencent")

if not DEEPSEEK_API_KEY or not DEEPSEEK_API_BASE:
    raise RuntimeError(
        "Missing DEEPSEEK_API_KEY / DEEPSEEK_API_BASE — set them in .env"
    )
