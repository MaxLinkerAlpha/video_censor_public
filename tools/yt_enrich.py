#!/usr/bin/env python3
"""
yt_enrich.py - YouTube视频数据回填工具 (v1.0)

功能:
1. 从昨日总报告中提取YouTube视频ID列表
2. 调用YouTube Data API v3获取缺失的发布时间和统计数据
3. 计算D1/D2/D3分数
4. 写入飞书YouTube表

使用方法:
    python tools/yt_enrich.py
    python tools/yt_enrich.py --video-id VIDEO_ID  # 仅处理单个视频
    python tools/yt_enrich.py --dry-run            # 预览，不写入
"""
import os
import sys
import re
import json
import time
import isodate
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "core"))

import requests
from config import CONFIG
from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID
from feishu_fields import (
    YT_F_VIDEO_ID, YT_F_PUBLISHED_AT, YT_F_VIEWS, YT_F_LIKES,
    YT_F_COMMENTS, YT_F_D1, YT_F_D2, YT_F_D3,
)


# 从环境变量读取代理设置（支持 HTTP_PROXY/HTTPS_PROXY）
_PROXIES = {}
for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    val = os.environ.get(key, "")
    if val:
        _PROXIES[key.lower().replace("http_proxy", "http").replace("https_proxy", "https")] = val
if _PROXIES:
    print(f"[INFO] 使用代理: {_PROXIES.get('https', _PROXIES.get('http', 'N/A'))}")


YT_API_KEY = CONFIG.get("yt_api_key", "")
YT_API_URL = "https://www.googleapis.com/youtube/v3/videos"


def parse_report_for_youtube(report_path: str) -> List[Dict]:
    """从报告中提取YouTube视频列表"""
    results = []
    with open(report_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = re.split(r"\n## \d+\.\s+", content)
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        is_youtube = False
        video_id = ""

        url_match = re.search(r"https?://www\.youtube\.com/watch\?v=([\w-]{11})", block)
        if url_match:
            video_id = url_match.group(1)
            is_youtube = True
        else:
            title_match = re.search(r"YouTube Video ([\w-]{11})", block)
            if title_match:
                video_id = title_match.group(1)
                is_youtube = True

        if is_youtube and video_id:
            status = "ok"
            if "状态: ERROR" in block or "错误:" in block:
                status = "error"
            results.append({"video_id": video_id, "status": status})

    return results


def fetch_youtube_stats(video_ids: List[str]) -> Dict[str, Dict]:
    """调用YouTube Data API v3批量获取视频统计数据"""
    if not YT_API_KEY:
        print("[ERROR] YT_API_KEY 未配置")
        return {}

    results = {}
    batch_size = 50
    total = len(video_ids)

    for i in range(0, total, batch_size):
        batch = video_ids[i:i + batch_size]
        ids_param = ",".join(batch)
        params = {"part": "snippet,contentDetails,statistics", "id": ids_param, "key": YT_API_KEY}

        try:
            resp = requests.get(YT_API_URL, params=params, timeout=30, proxies=_PROXIES or None)
            data = resp.json()

            if "items" not in data:
                print(f"  [WARN] API返回无items (batch {i//batch_size + 1}): {data.get('error', {}).get('message', '')}")
                time.sleep(1)
                continue

            for item in data["items"]:
                vid = item["id"]
                snippet = item.get("snippet", {})
                stats = item.get("statistics", {})
                content = item.get("contentDetails", {})

                published_at = snippet.get("publishedAt", "")

                duration_iso = content.get("duration", "PT0S")
                try:
                    duration_sec = int(isodate.parse_duration(duration_iso).total_seconds())
                except Exception:
                    duration_sec = 0

                results[vid] = {
                    "published_at": published_at,
                    "duration_sec": duration_sec,
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                    "title": snippet.get("title", ""),
                }

            print(f"  [OK] API batch {i//batch_size + 1}/{(total + batch_size - 1)//batch_size}: {len(data.get('items', []))} videos")
            if i + batch_size < total:
                time.sleep(1)
        except Exception as e:
            print(f"  [ERROR] API调用失败 (batch {i//batch_size + 1}): {e}")
            time.sleep(2)

    return results


def calc_d1_score(duration_sec: int) -> Tuple[float, str]:
    from scoring import calc_d1_duration_score
    return calc_d1_duration_score(duration_sec)


def calc_d2_score(published_at: str) -> Tuple[float, str]:
    from scoring import calc_d2_freshness_score
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        ts = int(dt.timestamp())
        return calc_d2_freshness_score(ts)
    except (ValueError, AttributeError):
        return 0.0, "无法解析发布时间"


def calc_d3_score(views: int, likes: int, comments: int) -> Tuple[float, str]:
    from scoring import calc_d3_engagement_score
    return calc_d3_engagement_score(views, likes, comments, 0)


def find_existing_records(feishu: FeishuSync, yt_videos: List[Dict]) -> Dict[str, Dict]:
    """从飞书查询现存记录数据"""
    existing = {}
    for v in yt_videos:
        record = feishu.get_record_by_field(YT_TABLE_ID, YT_F_VIDEO_ID, v["video_id"])
        if record:
            fields = record.get("fields", {})
            existing[v["video_id"]] = fields
    return existing


def main():
    dry_run = "--dry-run" in sys.argv

    report_path = os.path.join(PROJECT_ROOT, "core", "Total_Review_Report.md")
    if not os.path.exists(report_path):
        print(f"[ERROR] 报告文件不存在: {report_path}")
        return

    print("=" * 70)
    print("YouTube数据回填工具 v1.0")
    print("=" * 70)

    print(f"[*] 读取报告: {report_path}")
    yt_videos = parse_report_for_youtube(report_path)
    print(f"[*] 从报告解析到 {len(yt_videos)} 个YouTube视频")

    if not yt_videos:
        print("[ERROR] 未找到YouTube视频")
        return

    video_ids = [v["video_id"] for v in yt_videos]

    print(f"\n[*] 调用YouTube Data API v3...")
    api_data = fetch_youtube_stats(video_ids)
    print(f"[*] API返回 {len(api_data)} 个视频数据")

    if not api_data:
        print("[ERROR] API未返回任何数据")
        return

    print(f"\n[*] 连接飞书...")
    feishu = FeishuSync()
    feishu.set_bitable_token(BILI_BITABLE_TOKEN)

    success_count = 0
    skip_count = 0
    fail_count = 0

    for v in yt_videos:
        vid = v["video_id"]
        if vid not in api_data:
            print(f"  [SKIP] {vid}: API无数据")
            skip_count += 1
            continue

        data = api_data[vid]

        updates = {}
        if data["published_at"]:
            updates[YT_F_PUBLISHED_AT] = data["published_at"]

        if data["views"] > 0:
            updates[YT_F_VIEWS] = str(data["views"])

        if data["likes"] > 0:
            updates[YT_F_LIKES] = str(data["likes"])

        if data["comments"] > 0:
            updates[YT_F_COMMENTS] = str(data["comments"])

        d1_score, d1_desc = calc_d1_score(data["duration_sec"])
        updates[YT_F_D1] = round(d1_score, 2)

        d2_score, d2_desc = calc_d2_score(data["published_at"])
        updates[YT_F_D2] = round(d2_score, 2)

        d3_score, d3_desc = calc_d3_score(data["views"], data["likes"], data["comments"])
        updates[YT_F_D3] = round(d3_score, 2)

        if not updates:
            print(f"  [SKIP] {vid}: 无新数据")
            skip_count += 1
            continue

        if dry_run:
            print(f"  [DRY]   {vid}: Published={data['published_at'][:10]}, "
                  f"Views={data['views']}, Likes={data['likes']}, "
                  f"D1={d1_score}, D2={d2_score}, D3={d3_score}")
            success_count += 1
            continue

        result = feishu.update_video_record(vid, "youtube", updates)
        if result:
            print(f"  [OK]    {vid}: Published={data['published_at'][:10]}, "
                  f"Views={data['views']}, Likes={data['likes']}, "
                  f"D1={d1_score}, D2={d2_score}, D3={d3_score}")
            success_count += 1
        else:
            print(f"  [FAIL]  {vid}: 飞书写入失败")
            fail_count += 1

        time.sleep(0.3)

    print(f"\n{'='*70}")
    if dry_run:
        print(f"[预览] 可更新: {success_count}, 跳过: {skip_count}")
    else:
        print(f"[完成] 成功: {success_count}, 跳过: {skip_count}, 失败: {fail_count}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
