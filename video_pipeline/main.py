#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video_pipeline/main.py — YouTube 视频 + 字幕下载工具

功能:
  - 单个 YouTube 视频下载（视频 + 字幕）
  - 批量下载（从 URL 列表文件读取）
  - 仅下载字幕模式（跳过视频，节省带宽）
  - 多语言字幕并行下载

用法:
    # 单个视频下载（默认下载英文+中文自动字幕）
    python main.py "https://www.youtube.com/watch?v=CQJKxkozQfA"

    # 指定字幕语言
    python main.py "https://www.youtube.com/watch?v=xxx" --sub-lang en,zh-Hans

    # 仅下载字幕（不下载视频）
    python main.py "https://www.youtube.com/watch?v=xxx" --subs-only

    # 批量下载（urls.txt 每行一个 URL）
    python main.py --batch urls.txt

依赖:
    pip install yt-dlp
"""

import os
import sys
import json
import subprocess
import argparse
from pathlib import Path
from typing import Optional, List


# ============================================================
# 路径配置
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent
REFACTOR_DIR = SCRIPT_DIR.parent
OUTPUT_DIR = SCRIPT_DIR / "output" / "raw"

COOKIES_JSON = REFACTOR_DIR / "youtube_cookies.json"
COOKIES_TXT = SCRIPT_DIR / "cookies.txt"


# ============================================================
# Cookie 格式转换（JSON → Netscape）
# ============================================================

def convert_cookies_json_to_netscape() -> bool:
    """
    将 youtube_cookies.json 转换为 Netscape 格式 cookies.txt
    yt-dlp 的 --cookies 参数只接受 Netscape 格式

    返回: True 表示转换成功
    """
    if not COOKIES_JSON.exists():
        print(f"  [WARN] 未找到 cookies 文件: {COOKIES_JSON}")
        print(f"  [INFO] 将尝试无 cookie 下载（可能无法下载受限视频）")
        return False

    try:
        with open(COOKIES_JSON, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except Exception as e:
        print(f"  [ERROR] 读取 cookies JSON 失败: {e}")
        return False

    lines = ["# Netscape HTTP Cookie File", "# Auto-generated from youtube_cookies.json", ""]

    for c in cookies:
        if not isinstance(c, dict):
            continue
        domain = c.get("domain", "")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        expiry = str(int(c.get("expirationDate", 0))) if c.get("expirationDate") else "0"
        name = c.get("name", "")
        value = c.get("value", "")

        if name and domain:
            lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}")

    with open(COOKIES_TXT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))

    print(f"  [OK] Cookie 转换完成: {len(cookies)} 条 → {COOKIES_TXT}")
    return True


# ============================================================
# YouTube 视频 + 字幕下载
# ============================================================

def download_video(
    url: str,
    sub_lang: str = "en,zh-Hans",
    subs_only: bool = False,
) -> Optional[Path]:
    """
    使用 yt-dlp 下载 YouTube 视频 + 字幕

    参数:
        url: YouTube 视频 URL
        sub_lang: 字幕语言代码（逗号分隔，如 "en,zh-Hans"）
        subs_only: True = 仅下载字幕，跳过视频

    返回: 下载的视频文件路径，subs_only 模式返回 None
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 构建 yt-dlp 命令
    cmd = ["yt-dlp"]

    if subs_only:
        # 仅下载字幕
        cmd.extend([
            "--write-auto-subs",
            "--sub-lang", sub_lang,
            "--skip-download",
            "--convert-subs", "srt",
        ])
    else:
        # 视频 + 字幕（默认下载 mp4 格式）
        cmd.extend([
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",  # 优先 mp4，回退任意格式
            "--merge-output-format", "mp4",  # 合并后输出 mp4
            "--write-subs",
            "--write-auto-subs",
            "--sub-lang", sub_lang,
            "--convert-subs", "srt",
            "--embed-metadata",
        ])

    cmd.extend([
        "--no-playlist",
        "-o", str(OUTPUT_DIR / "%(title).100s_%(id)s.%(ext)s"),
    ])

    if COOKIES_TXT.exists():
        cmd.extend(["--cookies", str(COOKIES_TXT)])

    cmd.append(url)

    label = "仅下载字幕" if subs_only else "下载视频 + 字幕"
    print(f"\n{'='*60}")
    print(f"  {label}...")
    print(f"  字幕语言: {sub_lang}")
    print(f"  输出目录: {OUTPUT_DIR}")
    print(f"{'='*60}\n")

    try:
        result = subprocess.run(cmd, text=True, encoding="utf-8")
        if result.returncode != 0:
            print(f"\n  [ERROR] yt-dlp 返回错误码: {result.returncode}")
            return None
    except FileNotFoundError:
        print(f"\n  [ERROR] 未找到 yt-dlp，请先安装: pip install yt-dlp")
        return None
    except Exception as e:
        print(f"\n  [ERROR] 下载失败: {e}")
        return None

    if subs_only:
        return None

    # 找到下载的视频文件
    video_files = (
        list(OUTPUT_DIR.glob("*.mp4")) +
        list(OUTPUT_DIR.glob("*.mkv")) +
        list(OUTPUT_DIR.glob("*.webm"))
    )
    if not video_files:
        print(f"\n  [ERROR] 下载完成但未找到视频文件")
        return None

    video_path = video_files[0]
    size_mb = video_path.stat().st_size / 1024 / 1024
    print(f"\n  [OK] 下载完成: {video_path.name} ({size_mb:.1f} MB)")

    # 列出配套字幕
    _list_subtitle_files(video_path)

    return video_path


def _list_subtitle_files(video_path: Path):
    """列出下载的视频配套字幕文件"""
    stem = video_path.stem
    subs = list(OUTPUT_DIR.glob(f"{stem}*.srt")) + list(OUTPUT_DIR.glob(f"{stem}*.vtt"))
    if subs:
        print(f"\n  配套字幕:")
        for s in subs:
            size_kb = s.stat().st_size / 1024
            print(f"    - {s.name} ({size_kb:.0f} KB)")


# ============================================================
# 批量下载
# ============================================================

def batch_download(
    urls_file: Path,
    sub_lang: str = "en,zh-Hans",
    subs_only: bool = False,
    delay: float = 2.0,
):
    """
    从 URL 列表文件批量下载 YouTube 视频

    参数:
        urls_file: 包含 URL 列表的文本文件（每行一个 URL，空行和 # 注释行忽略）
        sub_lang: 字幕语言
        subs_only: 仅下载字幕
        delay: 每次下载间隔（秒），避免触发反爬
    """
    if not urls_file.exists():
        print(f"[ERROR] URL 列表文件不存在: {urls_file}")
        sys.exit(1)

    urls = []
    with open(urls_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    if not urls:
        print("[ERROR] URL 列表为空")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  批量下载")
    print(f"  共 {len(urls)} 个视频")
    print(f"  字幕语言: {sub_lang}")
    print(f"  下载间隔: {delay} 秒")
    print(f"{'='*60}")

    success = 0
    fail = 0

    for i, url in enumerate(urls, 1):
        print(f"\n\n{'#'*60}")
        print(f"  [{i}/{len(urls)}] {url}")
        print(f"{'#'*60}")

        result = download_video(url, sub_lang=sub_lang, subs_only=subs_only)
        if result:
            success += 1
        else:
            fail += 1

        if i < len(urls) and delay > 0:
            import time
            time.sleep(delay)

    print(f"\n\n{'='*60}")
    print(f"  批量下载完成: {success} 成功, {fail} 失败 (共 {len(urls)} 个)")
    print(f"{'='*60}")


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="YouTube 视频 + 字幕下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单个视频下载（默认下载英文+简体中文自动字幕）
  python main.py "https://www.youtube.com/watch?v=CQJKxkozQfA"

  # 指定字幕语言（仅下载英文）
  python main.py "https://www.youtube.com/watch?v=xxx" --sub-lang en

  # 指定多语言字幕
  python main.py "https://www.youtube.com/watch?v=xxx" --sub-lang en,zh-Hans,ja

  # 仅下载字幕（不下载视频）
  python main.py "https://www.youtube.com/watch?v=xxx" --subs-only --sub-lang zh-Hans

  # 批量下载
  python main.py --batch urls.txt
  python main.py --batch urls.txt --subs-only --sub-lang en,zh-Hans
        """,
    )
    parser.add_argument(
        "url", nargs="?",
        help="YouTube 视频 URL（批量模式 --batch 时不需提供）"
    )
    parser.add_argument(
        "--sub-lang", default="en,zh-Hans",
        help="字幕语言代码，逗号分隔（默认: en,zh-Hans）"
    )
    parser.add_argument(
        "--subs-only", action="store_true",
        help="仅下载字幕，不下载视频"
    )
    parser.add_argument(
        "--batch", metavar="FILE",
        help="批量下载模式：从文本文件读取 URL 列表（每行一个 URL）"
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="批量下载时每次下载间隔（秒，默认: 2.0）"
    )

    args = parser.parse_args()

    # 验证参数
    if not args.batch and not args.url:
        parser.error("请提供 YouTube URL 或使用 --batch 指定批量文件")

    if args.batch and args.url:
        parser.error("--batch 批量模式不需要单独提供 URL")

    # 转换 Cookie
    print(f"\n[准备] 转换 Cookie...")
    convert_cookies_json_to_netscape()

    if args.batch:
        # 批量模式
        batch_download(
            Path(args.batch),
            sub_lang=args.sub_lang,
            subs_only=args.subs_only,
            delay=args.delay,
        )
    else:
        # 单视频模式
        download_video(
            args.url,
            sub_lang=args.sub_lang,
            subs_only=args.subs_only,
        )


if __name__ == "__main__":
    main()