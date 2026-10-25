#!/usr/bin/env python3
"""
refresh_cookies.py - 从 cookies.json 文件刷新 .env 中的 Cookie

使用方法:
    python tools/refresh_cookies.py                    # 同时更新 Bilibili + YouTube
    python tools/refresh_cookies.py --bilibili-only     # 仅更新 Bilibili
    python tools/refresh_cookies.py --youtube-only      # 仅更新 YouTube
"""
import os, sys, json, re

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
BILI_JSON = os.path.join(PROJECT_ROOT, "bilibili_cookies.json")
YT_JSON = os.path.join(PROJECT_ROOT, "youtube_cookies.json")


def json_to_cookie(json_path: str) -> str:
    if not os.path.exists(json_path):
        print(f"  [WARN] 文件不存在: {json_path}")
        return ""
    with open(json_path, "r", encoding="utf-8") as f:
        cookies = json.load(f)
    parts = []
    for c in cookies:
        if isinstance(c, dict) and "name" in c and "value" in c:
            parts.append(f"{c['name']}={c['value']}")
    cookie = ";".join(parts)
    print(f"  [OK] 从 {os.path.basename(json_path)} 读取 {len(parts)} 个 Cookie 项 ({len(cookie)} 字符)")
    return cookie


def update_env(env_path: str, key: str, value: str):
    if not value:
        return False
    lines = []
    updated = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    lines.append(f"{key}={value}\n")
                    updated = True
                else:
                    lines.append(line)
    if not updated:
        lines.append(f"{key}={value}\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True


def main():
    bili_only = "--bilibili-only" in sys.argv
    yt_only = "--youtube-only" in sys.argv
    do_both = not bili_only and not yt_only

    print("=" * 60)
    print("Cookie 刷新工具 - 从 cookies.json → .env")
    print("=" * 60)

    changed = False

    if do_both or bili_only:
        bili_cookie = json_to_cookie(BILI_JSON)
        if bili_cookie and update_env(ENV_PATH, "BILIBILI_COOKIE", bili_cookie):
            print(f"  [OK] .env 中的 BILIBILI_COOKIE 已更新")
            changed = True

    if do_both or yt_only:
        yt_cookie = json_to_cookie(YT_JSON)
        if yt_cookie and update_env(ENV_PATH, "YT_COOKIE", yt_cookie):
            print(f"  [OK] .env 中的 YT_COOKIE 已更新")
            changed = True

    if not changed:
        print("  [SKIP] 未检测到需要更新的 Cookie")

    print("=" * 60)


if __name__ == "__main__":
    main()
