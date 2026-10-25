#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video_parser.py - 视频URL解析模块 (v4.3.0)

功能：
- 解析视频URL，识别平台和视频ID
- 支持多种URL格式（完整链接、短链接、纯ID）

使用方法：
    from platforms.video_parser import parse_video_url

    video_id, platform, error = parse_video_url("https://www.bilibili.com/video/BV1234567890")
"""

import re
from typing import Optional, Tuple


def parse_video_url(raw_input: str) -> Tuple[Optional[str], Optional[str], str]:
    """
    解析视频URL

    参数：
        raw_input: 原始输入字符串（可以是完整URL、短链接或纯ID）

    返回：
        (视频ID, 平台, 错误信息)
        - 视频ID: B站为BV号，YouTube为11位视频ID
        - 平台: "bilibili" 或 "youtube"
        - 错误信息: 解析失败时的错误描述

    支持的格式：
        B站:
        - https://www.bilibili.com/video/BV1234567890
        - BV1234567890

        YouTube:
        - https://www.youtube.com/watch?v=dQw4w9WgXcQ
        - https://youtu.be/dQw4w9WgXcQ
        - https://www.youtube.com/shorts/dQw4w9WgXcQ
        - dQw4w9WgXcQ
    """
    raw_input = raw_input.strip()
    if not raw_input:
        return None, None, "输入不能为空"

    raw_lower = raw_input.lower()

    if 'youtube.com' in raw_lower or 'youtu.be' in raw_lower:
        match = re.search(r'(?:v=|youtu\.be/|embed/|shorts/)([a-zA-Z0-9_-]{11})', raw_input, re.IGNORECASE)
        if match:
            return match.group(1), 'youtube', ""
        return None, None, "无法从YouTube链接中提取视频ID"

    if 'bilibili.com' in raw_lower:
        match = re.search(r'(BV[a-zA-Z0-9]{10})', raw_input, re.IGNORECASE)
        if match:
            bvid = match.group(1)
            return f"BV{bvid[2:]}", 'bilibili', ""
        return None, None, "无法从B站链接中提取BV号"

    if re.match(r'^BV[a-zA-Z0-9]{10}$', raw_input, re.IGNORECASE):
        return f"BV{raw_input[2:]}", 'bilibili', ""

    if re.match(r'^[a-zA-Z0-9_-]{11}$', raw_input):
        return raw_input, 'youtube', ""

    return None, None, f"无法识别的链接格式: {raw_input}"
