#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
platforms - 平台抓取层

包含：
- bilibili_client: B站字幕获取
- youtube_client: YouTube字幕获取
- video_parser: URL解析路由
"""

from .bilibili_client import BilibiliSubtitleFetcher
from .youtube_client import fetch_youtube_subtitle
from .video_parser import parse_video_url

__all__ = [
    "BilibiliSubtitleFetcher",
    "fetch_youtube_subtitle",
    "parse_video_url",
]
