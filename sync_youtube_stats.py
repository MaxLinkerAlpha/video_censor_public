#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_youtube_stats.py - YouTube 统计数据补充与评分同步 (v1.0)

功能:
1. 从飞书读取所有 YouTube 视频记录
2. 对于缺少 Published At/Views/Likes/Comments 的视频，通过 YouTube Data API v3 获取
3. 从已有数据计算 D2 Timeliness 和 D3 Engagement
4. 使用 scoring.py 重新计算 Total Score 和 Rating
5. 将结果写入飞书

使用方法:
    python sync_youtube_stats.py                  # 全量执行
    python sync_youtube_stats.py --dry-run         # 仅预览
    python sync_youtube_stats.py --api-only        # 仅调用YouTube API补充缺失数据
"""

import os
import sys
import re
import json
import time
import datetime
from typing import Dict, List, Any, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "core"))

from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID
from config import CONFIG


def parse_published_at(text_val) -> Optional[int]:
    """
    解析飞书的 Published At 字段为 Unix 时间戳（秒）
    支持格式: "2025/11/14 23:01:00", "2025-11-14 00:00:00", 时间戳等
    """
    if not text_val:
        return None
    if isinstance(text_val, (int, float)):
        return int(text_val)
    if isinstance(text_val, str):
        text_val = text_val.strip()
        for fmt in [
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d",
            "%Y-%m-%d",
        ]:
            try:
                dt = datetime.datetime.strptime(text_val, fmt)
                return int(dt.timestamp())
            except ValueError:
                continue
    return None


def calc_d2_score(publish_timestamp: int) -> Tuple[float, str]:
    """
    D2: 时效得分（同步 scoring.py 中的 calc_d2_freshness_score 逻辑）
    """
    if publish_timestamp <= 0:
        return 0.0, "未知发布时间"

    current_time = int(time.time())
    days_ago = (current_time - publish_timestamp) / 86400

    if days_ago < 0:
        return 0.0, "发布时间异常"
    elif days_ago <= 3:
        return 10.0, f"{days_ago:.1f}天前 (最新)"
    elif days_ago <= 7:
        score = 10.0 * (0.86 ** (days_ago - 3))
        return round(score, 1), f"{days_ago:.1f}天前 (次新)"
    elif days_ago <= 14:
        score = 6.0 * (0.92 ** (days_ago - 7))
        return round(score, 1), f"{days_ago:.1f}天前 (近期)"
    elif days_ago <= 21:
        score = 3.0 * (0.91 ** (days_ago - 14))
        return round(score, 1), f"{days_ago:.1f}天前 (较旧)"
    else:
        return 0.0, f"{days_ago:.1f}天前 (不推荐)"


def calc_d3_score(views: int, likes: int = 0, comments: int = 0) -> Tuple[float, str]:
    """
    D3: 互动得分（同步 scoring.py 中的 calc_d3_engagement_score 逻辑）
    YouTube 没有弹幕，只使用 likes + comments
    """
    if views <= 0:
        return 0.0, "无播放数据"

    total_engagement = likes + comments
    engagement_rate = (total_engagement / views) * 100

    if engagement_rate > 10.0:
        return 10.0, f"互动率{engagement_rate:.2f}% (极高)"
    elif engagement_rate > 7.0:
        return 9.0, f"互动率{engagement_rate:.2f}% (高)"
    elif engagement_rate > 5.0:
        return 7.0, f"互动率{engagement_rate:.2f}% (中高)"
    elif engagement_rate > 3.0:
        return 5.0, f"互动率{engagement_rate:.2f}% (中等)"
    elif engagement_rate > 1.0:
        return 3.0, f"互动率{engagement_rate:.2f}% (低)"
    else:
        return 1.0, f"互动率{engagement_rate:.2f}% (极低)"


def parse_int(val) -> int:
    """安全解析整数值"""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        val = val.strip()
        # 移除可能的千位分隔符等
        val = val.replace(",", "").replace(" ", "")
        try:
            return int(float(val))
        except ValueError:
            return 0
    return 0


def get_grade_from_score(score: float) -> str:
    """从分数推断等级"""
    if score >= 10.0:
        return 'A'
    elif score >= 8.0:
        return 'B'
    elif score >= 6.0:
        return 'C'
    elif score >= 4.0:
        return 'D'
    else:
        return 'E'


class YouTubeStatsSync:
    """YouTube 统计数据同步器"""

    def __init__(self, dry_run: bool = False):
        self.feishu = FeishuSync()
        self.feishu.set_bitable_token(BILI_BITABLE_TOKEN)
        self.dry_run = dry_run
        self.yt_api_key = CONFIG.get('yt_api_key', '')
        self.stats = {
            'total': 0,
            'api_fetch': 0,
            'd2_updated': 0,
            'd3_updated': 0,
            'score_updated': 0,
            'failed': 0,
            'skipped': 0,
        }

    def fetch_youtube_stats(self, video_id: str) -> Optional[Dict]:
        """通过 YouTube Data API v3 获取视频统计数据"""
        if not self.yt_api_key:
            print(f"  [WARN] YT_API_KEY 未配置，无法调用 YouTube API")
            return None

        url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&id={video_id}&key={self.yt_api_key}"

        for attempt in range(3):
            try:
                import requests
                res = requests.get(url, timeout=15)
                data = res.json()

                if 'items' not in data or len(data['items']) == 0:
                    if attempt < 2:
                        time.sleep(1)
                        continue
                    return None

                item = data['items'][0]
                snippet = item.get('snippet', {})
                statistics = item.get('statistics', {})

                result = {
                    'published_at': snippet.get('publishedAt', ''),
                    'view_count': parse_int(statistics.get('viewCount', 0)),
                    'like_count': parse_int(statistics.get('likeCount', 0)),
                    'comment_count': parse_int(statistics.get('commentCount', 0)),
                }
                return result

            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                    continue
                print(f"  [ERROR] YouTube API 调用失败 ({video_id}): {e}")
                return None

        return None

    def process_video(self, fields: Dict) -> Dict[str, Any]:
        """处理单个 YouTube 视频，返回需要更新的字段"""
        updates = {}
        video_id = fields.get('Video ID', '')

        if not video_id:
            return updates

        # 检查现有数据
        existing_views = fields.get('Views', None)
        existing_likes = fields.get('Likes', None)
        existing_comments = fields.get('Comments', None)
        existing_published = fields.get('Published At', None)
        existing_d2 = fields.get('D2 Timeliness', None)
        existing_d3 = fields.get('D3 Engagement', None)
        existing_total = fields.get('Total Score', None)

        # 步骤 1: 如果缺少基础统计数据，调用 YouTube API 获取
        need_api = not existing_views or not existing_likes or not existing_comments or not existing_published
        if need_api:
            api_data = self.fetch_youtube_stats(video_id)
            if api_data:
                # 不覆盖已有数据
                if not existing_views and api_data['view_count'] > 0:
                    updates['Views'] = api_data['view_count']
                if not existing_likes and api_data['like_count'] > 0:
                    updates['Likes'] = api_data['like_count']
                if not existing_comments and api_data['comment_count'] > 0:
                    updates['Comments'] = api_data['comment_count']
                if not existing_published and api_data['published_at']:
                    # 格式化 Published At
                    try:
                        dt = datetime.datetime.strptime(
                            api_data['published_at'].replace('T', ' ').replace('Z', ''),
                            "%Y-%m-%d %H:%M:%S"
                        )
                        updates['Published At'] = dt.strftime("%Y/%m/%d %H:%M:%S")
                    except ValueError:
                        updates['Published At'] = api_data['published_at']

                self.stats['api_fetch'] += 1

        # 重新读取数据（包括可能刚刚获取的）
        views = updates.get('Views', existing_views or 0)
        likes = updates.get('Likes', existing_likes or 0)
        comments = updates.get('Comments', existing_comments or 0)
        published = updates.get('Published At', existing_published or '')

        views_int = parse_int(views)
        likes_int = parse_int(likes)
        comments_int = parse_int(comments)

        # 步骤 2: 计算 D2 Timeliness（如果缺失且有发布时间数据）
        if not existing_d2 and published:
            ts = parse_published_at(published)
            if ts:
                d2_score, _ = calc_d2_score(ts)
                updates['D2 Timeliness'] = round(d2_score, 2)
                self.stats['d2_updated'] += 1

        # 步骤 3: 计算 D3 Engagement（如果缺失且有播放数据）
        if not existing_d3 and views_int > 0:
            d3_score, _ = calc_d3_score(views_int, likes_int, comments_int)
            updates['D3 Engagement'] = round(d3_score, 2)
            self.stats['d3_updated'] += 1

        # 步骤 4: 计算 Total Score
        # D4 Appeal 存储的是原始 AI 评分（0-100），需归一化到 0-10
        d1_val = float(fields.get('D1 Duration', 0) or 0)
        d2_val = updates.get('D2 Timeliness', existing_d2 or 0) or 0
        d3_val = updates.get('D3 Engagement', existing_d3 or 0) or 0
        raw_d4 = float(fields.get('D4 Appeal', 0) or 0)
        if raw_d4 > 0:
            d4_val = min(raw_d4 / 10.0, 10.0)  # 归一化: 0-100 -> 0-10
        else:
            d4_val = 0.0

        if not existing_total:
            # 使用动态维度平均（与 scoring.py 的 YouTube 逻辑一致）
            valid_dims = []
            if d1_val > 0:
                valid_dims.append(d1_val)
            if d2_val > 0:
                valid_dims.append(d2_val)
            if d3_val > 0:
                valid_dims.append(d3_val)
            if d4_val > 0:
                valid_dims.append(d4_val)

            if valid_dims:
                content_score = sum(valid_dims) / len(valid_dims)
                # YouTube 通常 M1=1.0（无选择历史数据）
                final_score = content_score
                updates['Total Score'] = round(final_score, 2)
                updates['Rating'] = get_grade_from_score(final_score)
                self.stats['score_updated'] += 1

        return updates

    def run(self):
        """主执行流程"""
        print('=' * 70)
        print('  YouTube 统计数据补充与评分同步')
        print('=' * 70)

        if not self.yt_api_key:
            print('\n  [WARN] YT_API_KEY 未配置，将跳过 YouTube API 调用')
            print('  但仍会从已有数据计算 D2/D3 分数\n')

        if self.dry_run:
            print('  *** DRY RUN 模式 - 仅预览不写入 ***\n')

        # 1. 获取所有 YouTube 记录
        print('[*] 正在读取 YouTube 表格...')
        records = self.feishu.get_records(YT_TABLE_ID)
        print(f'  读取到 {len(records)} 条记录')

        # 2. 筛选有 Video ID 的记录
        valid_records = []
        for r in records:
            fields = r.get('fields', {})
            vid = str(fields.get('Video ID', '') or '').strip()
            if vid:
                valid_records.append(r)

        print(f'  有效记录（有 Video ID）: {len(valid_records)}/{len(records)}')
        self.stats['total'] = len(valid_records)

        # 3. 逐个处理
        print(f'\n[*] 开始处理...\n')

        for i, r in enumerate(valid_records, 1):
            fields = r.get('fields', {})
            video_id = fields.get('Video ID', '')
            record_id = r.get('record_id', '')
            title = str(fields.get('Title', ''))[:30]

            updates = self.process_video(fields)

            if not updates:
                if i <= 3 or i % 100 == 0:
                    print(f'  [{i}/{len(valid_records)}] [SKIP] {video_id} (无需更新)')
                self.stats['skipped'] += 1
                continue

            if self.dry_run:
                print(f'  [{i}/{len(valid_records)}] [DRY]  {video_id} {title}')
                for k, v in updates.items():
                    print(f'      {k}: {v}')
                self.stats['skipped'] += 1
                continue

            # 执行写入
            ok = self.feishu.update_record(YT_TABLE_ID, record_id, updates)
            if ok:
                if i <= 3 or i % 50 == 0:
                    print(f'  [{i}/{len(valid_records)}] [OK]  {video_id} {title}  -> {list(updates.keys())}')
                self.stats['score_updated'] += 1  # 计入写入成功
            else:
                print(f'  [{i}/{len(valid_records)}] [FAIL] {video_id} (写入失败)')
                self.stats['failed'] += 1

            # 请求间隔
            if i < len(valid_records):
                time.sleep(0.3)

        # 4. 统计
        print(f'\n{"=" * 70}')
        print(f'  处理完成统计')
        print(f'{"=" * 70}')
        print(f'  总记录: {self.stats["total"]} 条')
        print(f'  API获取: {self.stats["api_fetch"]} 条')
        print(f'  D2更新: {self.stats["d2_updated"]} 条')
        print(f'  D3更新: {self.stats["d3_updated"]} 条')
        print(f'  评分更新: {self.stats["score_updated"]} 条')
        print(f'  失败: {self.stats["failed"]} 条')
        print(f'  跳过: {self.stats["skipped"]} 条')
        print(f'{"=" * 70}\n')


def main():
    import argparse

    parser = argparse.ArgumentParser(description='YouTube 统计数据补充与评分同步')
    parser.add_argument('--dry-run', action='store_true', help='仅预览不写入')
    parser.add_argument('--api-only', action='store_true', help='仅调用 API 补充缺失数据')
    args = parser.parse_args()

    syncer = YouTubeStatsSync(dry_run=args.dry_run)
    syncer.run()


if __name__ == '__main__':
    main()
