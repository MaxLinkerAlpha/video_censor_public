#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_pending_results.py - 从 Total_Review_Report.md 同步评分结果到飞书 (v1.0)

功能:
1. 读取 core/Total_Review_Report.md 解析每个视频的结果
2. 使用 FeishuSync 将结果推送到飞书多维表格
3. B站写入: 综合评分、评分等级、AI审核结果、字幕获取状态
4. YouTube写入: Total Score, Rating, AI Review Result, Subtitle Status, D1 Duration, D2 Timeliness, D3 Engagement, D4 Appeal

使用方法:
    python sync_pending_results.py              # 同步全部
    python sync_pending_results.py --dry-run     # 仅预览不同步
"""

import os
import sys
import re
import json
import time
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime

# 路径配置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "core"))

from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID, YT_TABLE_ID
from feishu_fields import (
    BILI_F_D1, BILI_F_D2, BILI_F_D3, BILI_F_D4, BILI_F_TOTAL_SCORE,
    BILI_F_RATING, BILI_F_AI_REVIEW, BILI_F_AI_REVIEW_REPORT, BILI_F_SUBTITLE_STATUS,
    YT_F_D1, YT_F_D2, YT_F_D3, YT_F_D4, YT_F_TOTAL_SCORE,
    YT_F_RATING, YT_F_AI_REVIEW, YT_F_AI_REVIEW_REPORT, YT_F_SUBTITLE_STATUS,
    BILI_SUBTITLE_SUCCESS, BILI_SUBTITLE_FAILED,
    YT_SUBTITLE_SUCCESS, YT_SUBTITLE_FAILED, YT_SUBTITLE_PENDING,
    map_censor_result_to_bili, map_censor_result_to_yt,
)


def parse_report(report_path: str) -> List[Dict]:
    """
    解析 Total_Review_Report.md

    返回:
        视频结果列表，每项包含:
        {
            'video_id': str,
            'platform': str ('bilibili'/'youtube'),
            'title': str,
            'url': str,
            'success': bool,
            'has_subtitle': bool,
            'total_score': float,
            'grade': str,
            'd1_score': float,
            'd2_score': float,
            'd3_score': float,
            'd4_score': float,
            'censor_result': str,
            'error': str (only if failed),
        }
    """
    with open(report_path, 'r', encoding='utf-8') as f:
        content = f.read()

    results = []

    # 按视频块分割 (## 数字. 标题)
    blocks = re.split(r'\n## \d+\.\s+', content)

    for block in blocks:
        if not block.strip():
            continue

        result = _parse_video_block(block)
        if result and result.get('video_id'):
            results.append(result)

    return results


def _parse_video_block(block: str) -> Optional[Dict]:
    """解析单个视频块"""
    lines = block.split('\n')

    result = {
        'success': False,
        'has_subtitle': False,
        'total_score': 0.0,
        'grade': '',
        'd1_score': 0.0,
        'd2_score': 0.0,
        'd3_score': 0.0,
        'd4_score': 0.0,
        'censor_result': '',
        'error': '',
    }

    # 提取标题 (第一行)
    title_line = lines[0].strip() if lines else ''

    # 提取URL (去除尾部的反引号和括号)
    url_match = re.search(r'https?://[^\s`\)\]\>]+', block)
    url = url_match.group(0) if url_match else ''
    result['url'] = url

    # 提取平台 (支持 **平台**: 或 平台: 格式)
    platform_match = re.search(r'\**平台\**:\s*(BILIBILI|YOUTUBE|bilibili|youtube)', block)
    if platform_match:
        plat_str = platform_match.group(1).upper()
        result['platform'] = 'bilibili' if plat_str == 'BILIBILI' else 'youtube'
    elif 'bilibili.com' in url.lower():
        result['platform'] = 'bilibili'
    elif 'youtube.com' in url.lower() or 'youtu.be' in url.lower():
        result['platform'] = 'youtube'
    else:
        print(f"  [WARN] 无法识别平台，跳过: {url[:50]}")
        return None

    # 提取 video_id
    if result['platform'] == 'bilibili':
        bv_match = re.search(r'BV[\w]{10}', url)
        result['video_id'] = bv_match.group(0) if bv_match else ''
    else:
        yt_match = re.search(r'[?&]v=([\w\-]{11})', url)
        if yt_match:
            result['video_id'] = yt_match.group(1)
        else:
            # 从标题行提取: "YouTube Video {id}"
            vid_line_match = re.search(r'YouTube Video\s+([\w\-]{11})', block)
            result['video_id'] = vid_line_match.group(1) if vid_line_match else ''

    if not result['video_id']:
        print(f"  [WARN] 无法提取 video_id，跳过: {url[:50]}")
        return None

    # 提取状态 (支持 **状态**: 或 状态: 格式)
    status_match = re.search(r'\**状态\**:\s*(OK|ERROR|失败|成功)', block)
    if status_match:
        status_str = status_match.group(1).strip()
        if status_str in ('ERROR', '失败'):
            error_match = re.search(r'\**错误\**:\s*(.+)', block)
            result['error'] = error_match.group(1).strip() if error_match else '未知错误'
            return result  # 失败记录也返回，但 success=False
        else:
            result['success'] = True

    # 检查是否有字幕获取成功
    if result.get('success'):
        # 检查字幕状态
        if 'WARN 该视频无字幕' in block or '字幕获取失败' in block:
            result['has_subtitle'] = False
        else:
            result['has_subtitle'] = True

    # 提取综合评分 (格式: **综合评分**: 6.62分 C 一般 或 6.62分 C)
    total_score = 0.0
    grade = ''
    score_match = re.search(r'\*\*综合评分\*\*:\s*([\d.]+)\s*分\s*([A-E])', block)
    if score_match:
        total_score = float(score_match.group(1))
        grade = score_match.group(2)
    else:
        # 简化格式
        score_match2 = re.search(r'\*\*综合评分\*\*:\s*([\d.]+)\s*分', block)
        if score_match2:
            total_score = float(score_match2.group(1))

    result['total_score'] = total_score
    result['grade'] = grade if grade else _infer_grade_from_score(total_score)

    # 提取 D1 D2 D3 D4 分数
    if result.get('success'):
        # D1 时长
        d1_match = re.search(r'\|\s*D1\s*(?:时长)?\s*\|\s*([\d.]+)', block)
        if d1_match:
            result['d1_score'] = float(d1_match.group(1))

        # D2 时效
        d2_match = re.search(r'\|\s*D2\s*(?:时效)?\s*\|\s*([\d.]+)', block)
        if d2_match:
            result['d2_score'] = float(d2_match.group(1))

        # D3 互动
        d3_match = re.search(r'\|\s*D3\s*(?:互动)?\s*\|\s*([\d.]+)', block)
        if d3_match:
            result['d3_score'] = float(d3_match.group(1))

    # 提取 AI吸引力评分 (D4)
    d4_match = re.search(r'\*\*AI吸引力评分\*\*:\s*(\d+)\s*分', block)
    if d4_match:
        result['d4_score'] = float(d4_match.group(1))

    # 提取审核结果
    if '[OK 全文安全]' in block or '未发现核心违规' in block:
        result['censor_result'] = 'safe'
    elif '违规' in block or 'unsafe' in block:
        result['censor_result'] = 'unsafe'
    elif 'WARN 该视频无字幕' in block or '无法进行内容审核' in block:
        result['censor_result'] = 'unsafe'
    else:
        result['censor_result'] = 'pending'

    return result


def _infer_grade_from_score(score: float) -> str:
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


def build_updates(result: Dict) -> Optional[Dict[str, Any]]:
    """根据解析结果构建飞书更新数据"""
    platform = result.get('platform', '').lower()

    if platform == 'bilibili':
        updates = {}

        if result.get('d1_score', 0) >= 0:
            updates[BILI_F_D1] = round(result.get('d1_score', 0), 2)
        if result.get('d2_score', 0) >= 0:
            updates[BILI_F_D2] = round(result.get('d2_score', 0), 2)
        if result.get('d3_score', 0) >= 0:
            updates[BILI_F_D3] = round(result.get('d3_score', 0), 2)
        if result.get('d4_score', 0) >= 0:
            updates[BILI_F_D4] = round(result.get('d4_score', 0), 2)

        if result.get('total_score', 0) > 0:
            updates[BILI_F_TOTAL_SCORE] = round(result['total_score'], 2)

        if result.get('grade'):
            updates[BILI_F_RATING] = result['grade']

        if result.get('censor_result'):
            updates[BILI_F_AI_REVIEW] = map_censor_result_to_bili(result['censor_result'])
            # 写入完整AI审核报告（文本类型）
            updates[BILI_F_AI_REVIEW_REPORT] = result['censor_result']

        updates[BILI_F_SUBTITLE_STATUS] = BILI_SUBTITLE_SUCCESS if result.get('has_subtitle') else BILI_SUBTITLE_FAILED

        return updates if updates else None

    elif platform == 'youtube':
        updates = {}

        if result.get('d1_score', 0) > 0:
            updates[YT_F_D1] = round(result['d1_score'], 2)
        if result.get('d2_score', 0) > 0:
            updates[YT_F_D2] = round(result['d2_score'], 2)
        if result.get('d3_score', 0) > 0:
            updates[YT_F_D3] = round(result['d3_score'], 2)
        if result.get('d4_score', 0) > 0:
            updates[YT_F_D4] = round(result['d4_score'], 2)

        if result.get('total_score', 0) > 0:
            updates[YT_F_TOTAL_SCORE] = round(result['total_score'], 2)

        if result.get('grade'):
            updates[YT_F_RATING] = result['grade']

        if result.get('censor_result'):
            updates[YT_F_AI_REVIEW] = map_censor_result_to_yt(result['censor_result'])
            # 写入完整AI审核报告（文本类型）
            updates[YT_F_AI_REVIEW_REPORT] = result['censor_result']

        subtitle_status = YT_SUBTITLE_SUCCESS if result.get('has_subtitle') else (YT_SUBTITLE_FAILED if result.get('success') else YT_SUBTITLE_PENDING)
        updates[YT_F_SUBTITLE_STATUS] = subtitle_status

        return updates if updates else None

    return None


def main():
    import argparse

    parser = argparse.ArgumentParser(description='从报告同步评分结果到飞书')
    parser.add_argument('--report', type=str,
                        default=os.path.join(SCRIPT_DIR, 'core', 'Total_Review_Report.md'),
                        help='报告文件路径')
    parser.add_argument('--dry-run', action='store_true', help='仅预览不执行')
    parser.add_argument('--delay', type=float, default=0.5, help='请求间隔(秒)')

    args = parser.parse_args()

    report_path = args.report
    if not os.path.exists(report_path):
        print(f'[ERROR] 报告文件不存在: {report_path}')
        sys.exit(1)

    print('=' * 70)
    print('  从 Total_Review_Report.md 同步结果到飞书')
    print('=' * 70)

    # 1. 解析报告
    print(f'\n[*] 解析报告: {report_path}')
    records = parse_report(report_path)
    print(f'  总记录: {len(records)}')

    success_records = [r for r in records if r.get('success')]
    failed_records = [r for r in records if not r.get('success')]
    bili_records = [r for r in success_records if r.get('platform') == 'bilibili']
    yt_records = [r for r in success_records if r.get('platform') == 'youtube']

    print(f'  成功: {len(success_records)} (B站: {len(bili_records)}, YouTube: {len(yt_records)})')
    print(f'  失败: {len(failed_records)}')

    if not success_records:
        print('\n  [WARN] 没有成功的记录需要同步')
        return

    # 2. 预览
    print(f'\n[*] 同步预览:')
    for r in success_records:
        updates = build_updates(r)
        if updates:
            print(f'  [{r["platform"].upper():8s}] {r["video_id"]}')
            for k, v in updates.items():
                print(f'      {k}: {v}')

    if args.dry_run:
        print('\n[DRY RUN] 预览模式，不实际写入')
        return

    # 3. 执行同步
    print(f'\n[*] 开始同步 ({len(success_records)} 条)...')
    feishu = FeishuSync()
    feishu.set_bitable_token(BILI_BITABLE_TOKEN)

    stats = {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0}

    for i, r in enumerate(success_records, 1):
        video_id = r.get('video_id', '')
        platform = r.get('platform', '')

        updates = build_updates(r)
        if not updates:
            print(f'  [{i}/{len(success_records)}] [SKIP] {video_id} (无更新数据)')
            stats['skipped'] += 1
            continue

        ok = feishu.update_video_record(video_id, platform, updates)
        stats['total'] += 1
        if ok:
            stats['success'] += 1
            print(f'  [{i}/{len(success_records)}] [OK] [{platform.upper():8s}] {video_id}')
        else:
            stats['failed'] += 1
            print(f'  [{i}/{len(success_records)}] [FAIL] [{platform.upper():8s}] {video_id}')

        # 请求间隔
        if i < len(success_records):
            time.sleep(args.delay)

    # 4. 统计
    print(f'\n{"=" * 70}')
    print(f'  同步完成统计')
    print(f'{"=" * 70}')
    print(f'  总计: {stats["total"]} 条')
    print(f'  成功: {stats["success"]} 条')
    print(f'  失败: {stats["failed"]} 条')
    print(f'  跳过: {stats["skipped"]} 条')
    print(f'{"=" * 70}\n')


if __name__ == '__main__':
    main()
