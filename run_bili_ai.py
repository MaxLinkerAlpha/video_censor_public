#!/usr/bin/env python3
"""
Quick script: Bilibili-only subtitle fetch + AI review + Feishu write (v1.0)
"""
import sys, os, time
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'core'))

from config import CONFIG
from main import load_videos_from_feishu, sync_results_to_feishu
from video_processor import submit_ai_tasks, process_ai_results
from platforms.bilibili_client import BilibiliSubtitleFetcher
from feishu_fields import (
    BILI_F_SUBTITLE_STATUS, BILI_F_AI_REVIEW, BILI_F_TOTAL_SCORE,
    BILI_F_D1, BILI_F_D2, BILI_F_D3, BILI_F_D4, BILI_F_RATING,
    BILI_F_BVID, BILI_F_TITLE,
    BILI_SUBTITLE_SUCCESS, BILI_SUBTITLE_FAILED,
    BILI_AI_RESULT_PASS, BILI_AI_RESULT_FAIL,
    map_subtitle_status_to_bili, map_censor_result_to_bili,
)

cookie = CONFIG.get('bili_cookie', '')
fetcher = BilibiliSubtitleFetcher(cookie)

print("="*60)
print("Bilibili 字幕获取 + AI 审核 + 飞书回写")
print("="*60)

print("\n[*] 从飞书获取7天内B站视频...")
bili_videos, _ = load_videos_from_feishu(days_filter=7)
print(f"[*] 找到 {len(bili_videos)} 个B站视频")

if not bili_videos:
    print("[WARN] 没有找到B站视频，检查飞书标记是否正确")
    sys.exit(0)

# Step 1: Fetch subtitles
print(f"\n[*] 步骤1: 获取字幕 ({len(bili_videos)} 个视频)")
results = []
for v in bili_videos:
    url = v.get('url', '')
    if 'bilibili.com/video/BV' not in url and v.get('video_id'):
        url = f"https://www.bilibili.com/video/{v['video_id']}"
    if 'bilibili.com' not in url:
        print(f"  [SKIP] 非B站视频: {url[:50]}")
        continue
    
    vid = v.get('video_id', '')
    print(f"\n  [{len(results)+1}/{len(bili_videos)}] {vid} - {v.get('title','')[:40]}")
    
    try:
        subtitle_data, sub_err = fetcher.fetch(vid)
        time.sleep(0.6)
    except Exception as e:
        print(f"    [ERROR] 获取字幕异常: {e}")
        subtitle_data, sub_err = None, str(e)
    
    if subtitle_data:
        has_sub = subtitle_data.get('has_subtitle', False)
        sub_len = len(subtitle_data.get('subtitle_text', ''))
        print(f"    [OK] has_subtitle={has_sub}, subtitle_len={sub_len}")
        
        video_info = {
            'url': url,
            'video_id': vid,
            'title': subtitle_data.get('title', v.get('title', '')),
            'platform': 'bilibili',
            'duration': subtitle_data.get('duration', 0),
            'publish_time': subtitle_data.get('publish_time', 0),
            'views': subtitle_data.get('views', 0),
            'likes': subtitle_data.get('likes', 0),
            'comments': subtitle_data.get('comments', 0),
            'danmaku': subtitle_data.get('danmaku', 0),
            'has_subtitle': has_sub,
            'subtitle_text': subtitle_data.get('subtitle_text', ''),
            'mid': subtitle_data.get('mid', 0),
            'bvid': vid,
        }
    else:
        print(f"    [FAIL] subtitle_data is None, err={sub_err}")
        video_info = {
            'url': url,
            'video_id': vid,
            'title': v.get('title', ''),
            'platform': 'bilibili',
            'duration': 0,
            'publish_time': 0,
            'has_subtitle': False,
            'subtitle_text': '',
        }
    
    result_info = submit_ai_tasks(video_info, 'bilibili')
    results.append(result_info)

# Step 2: Process AI results
print(f"\n[*] 步骤2: 等待AI审核结果...")
process_ai_results(results)

# Step 3: Sync to Feishu
print(f"\n[*] 步骤3: 同步到飞书")
sync_count = sync_results_to_feishu(results)
print(f"\n[完成] 成功同步 {sync_count}/{len(results)} 条记录到飞书")
