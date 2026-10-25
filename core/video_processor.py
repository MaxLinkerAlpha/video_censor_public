#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video_processor.py - 视频处理核心模块 (v4.3.0)

功能：
- 视频评分计算（D1+D2+D3+M1系统）
- AI审查任务提交与管理
- AI结果处理与解析
- 视频筛选与排名
- 优先级映射管理

使用方法：
    from video_processor import calculate_video_score, submit_ai_tasks, process_ai_results

    score_result, score_details = calculate_video_score(video_info, platform)
    result_info = submit_ai_tasks(video_info, platform)
"""

import os
import json
from typing import Dict, List, Tuple, Optional, Any
from concurrent.futures import as_completed

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CONFIG, SYSTEM_PROMPT_CENSOR, SYSTEM_PROMPT_APPEAL
from scoring import (
    ScoreResult,
    SelectionHistory,
    calculate_full_score,
    filter_and_rank_videos,
)
from feishu_fields import (
    BILI_HUMAN_HIGH, YT_HUMAN_HIGH, PRIORITY_WEIGHT,
)

from ai_client import (
    call_api,
    parse_appeal_score,
    get_ai_executor,
    select_model,
)
from reporter import print_video_result
from platforms.video_parser import parse_video_url
from platforms.bilibili_client import BilibiliSubtitleFetcher, BILI_COOKIE
from platforms.youtube_client import fetch_youtube_subtitle, fetch_youtube_stats


_PRIORITY_MAP_CACHE: Dict[str, Dict] = {}


def _load_priority_map() -> Dict[str, Dict]:
    """加载优先级映射文件 (v5.4.0 修复路径: orchestrator 写到项目根, 此前误读 core/core/ 永远为空)"""
    global _PRIORITY_MAP_CACHE
    if _PRIORITY_MAP_CACHE:
        return _PRIORITY_MAP_CACHE

    priority_map_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "video_priority_map.json"
    )
    if os.path.exists(priority_map_file):
        try:
            with open(priority_map_file, "r", encoding="utf-8") as f:
                _PRIORITY_MAP_CACHE = json.load(f)
            print(f"  [INFO] 已加载优先级映射: {len(_PRIORITY_MAP_CACHE)} 个视频")
        except Exception as e:
            print(f"  [WARN] 加载优先级映射失败: {e}")
    return _PRIORITY_MAP_CACHE


def _get_video_priority(video_url: str) -> str:
    """获取视频的优先级"""
    priority_map = _load_priority_map()
    return priority_map.get(video_url, {}).get("human_priority", "normal")


_selection_history: Optional[SelectionHistory] = None


def get_selection_history() -> SelectionHistory:
    """获取入选历史管理器单例"""
    global _selection_history
    if _selection_history is None:
        history_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "data",
            CONFIG.get('selection_history_file', 'selection_history.json')
        )
        _selection_history = SelectionHistory(history_file)
    return _selection_history


def calculate_video_score(video_info: dict, platform: str, ai_appeal_score: int = 0) -> Tuple[ScoreResult, dict]:
    """计算视频评分（使用 D1+D2+D3+D4+M1 评分系统 v3.4.0）"""
    duration = video_info.get('duration', 0)
    publish_time = video_info.get('publish_time', 0)
    views = video_info.get('views', 0)
    likes = video_info.get('likes', 0)
    comments = video_info.get('comments', 0)
    danmaku = video_info.get('danmaku', 0)

    # v5.4.6: YouTube 统计数据补全
    #   字幕路径 (youtube_transcript_api) 不含 publish_time/views/likes/comments,
    #   导致 D2(时效) 与 D3(互动) 恒为 0 —— 评分退化成 (D1+D4)/2。
    #   v3.9.0 曾设计"从飞书回填", 但实现已在重构中丢失 (见 core/CHANGELOG.md v5.4.6)。
    #   现改为直接调官方 Data API 实时获取; 失败**不阻断**评分, 但必须显式告警, 禁止静默降级。
    if platform.lower() == 'youtube':
        vid = str(video_info.get('video_id', '') or '')
        if vid and not publish_time:
            stats = fetch_youtube_stats(vid)
            if stats:
                publish_time = publish_time or int(stats.get('publish_time', 0) or 0)
                views = views or int(stats.get('views', 0) or 0)
                likes = likes or int(stats.get('likes', 0) or 0)
                comments = comments or int(stats.get('comments', 0) or 0)
                if not video_info.get('channel_id'):
                    video_info['channel_id'] = stats.get('channel_id', '') or ''
                # 写回 video_info: 同一视频在本流程内会被评分 3 次
                #   (fetch 阶段 / submit_ai_tasks / process_ai_results),
                #   写回后后续调用直接复用, 不必再查
                video_info['publish_time'] = publish_time
                video_info['views'] = views
                video_info['likes'] = likes
                video_info['comments'] = comments
                print(f"    [INFO] {vid} 统计数据补全: views={views} likes={likes} "
                      f"comments={comments} pub={publish_time}", flush=True)
            elif not video_info.get('_stats_warned'):
                # 同一视频只告警一次, 避免 3 次评分各打一遍
                video_info['_stats_warned'] = True
                print(f"    [WARN] {vid} 统计数据补全失败 -> D2/D3 按 0 计 (总分会被低估)", flush=True)

    if platform.lower() == 'bilibili':
        author_id = str(video_info.get('mid', ''))
        bvid = video_info.get('bvid', '')
    else:
        author_id = str(video_info.get('channel_id', ''))
        bvid = ''

    human_priority = "normal"

    if bvid:
        try:
            from feishu_sync import FeishuSync
            sync = FeishuSync()
            if sync.is_in_creator_table(bvid):
                human_priority = BILI_HUMAN_HIGH
                print(f"  [INFO] 视频 {bvid} 在创作者表格中，设为高优先级")
        except Exception as e:
            print(f"  [WARN] 检查创作者表格失败: {e}")

    video_url = video_info.get('url', '')
    if human_priority == "normal":
        # v5.4.0: 来源注册表加权 —— Discord 自荐/中文精选视频不再前置写 High Priority
        # (Human Prescreen 字段留给 ScriptDedup 流程), 评分时按来源注册表直接给 High 权重 (+10),
        # AI 审核完成后由 workflow_orchestrator 统一翻转 Human Prescreen 字段
        try:
            from source_registry import get_source
            src_vid = bvid if platform.lower() == 'bilibili' else str(video_info.get('video_id', '') or '')
            if src_vid and get_source(platform.lower(), src_vid):
                human_priority = BILI_HUMAN_HIGH if platform.lower() == 'bilibili' else YT_HUMAN_HIGH
                print(f"  [INFO] {src_vid} 为来源推荐视频 (Discord自荐/中文精选), 评分加权: {human_priority} (+10)")
        except Exception as e:
            print(f"  [WARN] 读取来源注册表失败: {e}")
    if human_priority == "normal":
        human_priority = _get_video_priority(video_url)

    history = get_selection_history()

    score_result = calculate_full_score(
        duration_seconds=duration,
        publish_timestamp=publish_time,
        views=views,
        likes=likes,
        comments=comments,
        danmaku=danmaku,
        platform=platform.lower(),
        author_id=author_id,
        selection_history=history,
        human_priority=human_priority,
        ai_appeal_score=ai_appeal_score
    )

    priority_bonus = PRIORITY_WEIGHT.get(score_result.human_priority, 0.0)

    score_details = {
        'd1': {'score': score_result.d1_score, 'desc': score_result.d1_desc},
        'd2': {'score': score_result.d2_score, 'desc': score_result.d2_desc},
        'd3': {'score': score_result.d3_score, 'desc': score_result.d3_desc},
        'd4': {'score': score_result.d4_score, 'desc': score_result.d4_desc},
        'm1': {'multiplier': score_result.m1_multiplier, 'desc': score_result.m1_desc},
        'content_score': score_result.content_score,
        'total': round(score_result.final_score + priority_bonus, 2),
        'grade': score_result.grade,
        'recommendation': score_result.recommendation
    }

    return score_result, score_details


def split_subtitle_chunks(subtitle_text: str, max_chars: int) -> List[str]:
    """将字幕文本分割成块"""
    if len(subtitle_text) <= max_chars:
        return [subtitle_text]

    chunks = []
    start = 0
    text_len = len(subtitle_text)

    while start < text_len:
        chunk_size = max_chars

        if start + chunk_size >= text_len:
            raw_chunk = subtitle_text[start:]
        else:
            end = start + chunk_size
            boundary = max(
                subtitle_text.rfind('。', start, end),
                subtitle_text.rfind('！', start, end),
                subtitle_text.rfind('？', start, end),
                subtitle_text.rfind('.', start, end),
                subtitle_text.rfind('!', start, end),
                subtitle_text.rfind('?', start, end),
                subtitle_text.rfind('\n', start, end)
            )
            if boundary > start + max_chars * 0.5:
                end = boundary + 1

            raw_chunk = subtitle_text[start:end]

        chunk = raw_chunk.strip()
        if chunk:
            chunks.append(chunk)
        start += len(raw_chunk) if raw_chunk else 1

    return chunks


def format_for_ai(title: str, subtitle_text: str) -> str:
    """格式化输入给 AI 的内容"""
    return f"标题: {title}\n\n正文: {subtitle_text}"


def submit_ai_tasks(video_info: dict, platform: str) -> dict:
    """提交 AI 审查和评分任务"""
    result_info = {
        'input': video_info.get('url', ''),
        'success': False,
        'title': video_info.get('title', ''),
        'platform': platform.upper() if platform else 'UNKNOWN',
        'video_id': video_info.get('video_id', ''),
        'author_id': video_info.get('mid', '') if platform.lower() == 'bilibili' else video_info.get('channel_id', ''),
        'error': '',
        'censor_result': '',
        'appeal_result': '',
        'duration': video_info.get('duration', 0),
        'has_subtitle': video_info.get('has_subtitle', False),
        'scores': {}
    }

    title = video_info.get('title', '未知标题')
    subtitle_text = video_info.get('subtitle_text', '')
    duration = video_info.get('duration', 0)
    has_subtitle = video_info.get('has_subtitle', False)

    score_result, score_details = calculate_video_score(video_info, platform)
    result_info['scores'] = score_details
    result_info['_score_result'] = score_result

    if not has_subtitle:
        result_info['censor_result'] = "WARN 该视频无字幕，无法进行内容审核"
        result_info['success'] = True
        return result_info

    max_chars = CONFIG.get('max_subtitle_chars', 15000)
    original_len = len(subtitle_text)
    chunks = split_subtitle_chunks(subtitle_text, max_chars)
    is_split = len(chunks) > 1

    if is_split:
        print(f"    [WARN] 字幕过长({original_len}字符)，已拆分为{len(chunks)}段", flush=True)

    censor_content = format_for_ai(title, subtitle_text)
    appeal_content = format_for_ai(title, chunks[0])
    censor_len = len(censor_content)
    appeal_len = len(appeal_content)

    censor_model, censor_model_desc = select_model(censor_len)
    appeal_model, appeal_model_desc = select_model(appeal_len)

    print(f"    [*] 审查内容: {censor_len}字符({len(chunks)}段) | 评分内容: {appeal_len}字符", flush=True)
    print(f"    [*] 提交AI审查和评分任务...", flush=True)

    ai_executor = get_ai_executor()
    censor_future = ai_executor.submit(call_api, censor_content, SYSTEM_PROMPT_CENSOR, censor_model)
    appeal_future = ai_executor.submit(call_api, appeal_content, SYSTEM_PROMPT_APPEAL, appeal_model)

    result_info['_censor_future'] = censor_future
    result_info['_appeal_future'] = appeal_future
    result_info['_is_split'] = is_split
    result_info['_chunks'] = chunks
    result_info['_video_info'] = video_info

    return result_info


def process_ai_results(results: List[Dict]) -> None:
    """处理 AI 任务结果"""
    pending = []
    future_to_result = {}
    for r in results:
        if '_censor_future' in r and '_appeal_future' in r:
            pending.append(r)
            future_to_result[r['_censor_future']] = (r, 'censor')
            future_to_result[r['_appeal_future']] = (r, 'appeal')

    if not pending:
        return

    print(f"\n[*] AI任务进行中 ({len(pending)} 个视频)...", flush=True)

    futures_to_wait = list(future_to_result.keys())

    for future_obj in as_completed(futures_to_wait):
        result_dict, ftype = future_to_result[future_obj]

        try:
            result_dict[ftype + '_result'] = future_obj.result(timeout=600)
        except Exception as e:
            result_dict[ftype + '_result'] = f"[ERR] {ftype} 任务异常: {str(e)}"

        censor_done = bool(result_dict.get('censor_result') and result_dict['censor_result'] != '')
        appeal_done = bool(result_dict.get('appeal_result') and result_dict['appeal_result'] != '')

        if censor_done and appeal_done:
            censor_val = result_dict.get('censor_result', '')
            appeal_val = result_dict.get('appeal_result', '')

            if censor_val.startswith('[ERR]') or appeal_val.startswith('[ERR]'):
                err_parts = []
                if censor_val.startswith('[ERR]'):
                    err_parts.append(f"审查失败: {censor_val[:80]}")
                if appeal_val.startswith('[ERR]'):
                    err_parts.append(f"评分失败: {appeal_val[:80]}")
                result_dict['success'] = False
                result_dict['error'] = '; '.join(err_parts)
                print(f"\n[ERROR] AI任务失败: {result_dict.get('title', '未知')} - {result_dict['error']}", flush=True)
                continue

            try:
                appeal_score, appeal_desc, appeal_details = parse_appeal_score(appeal_val)

                if appeal_desc == "解析失败":
                    result_dict['success'] = False
                    result_dict['error'] = f"AI评分解析失败"
                else:
                    result_dict['success'] = True
                    result_dict['error'] = ''

                result_dict['scores']['ai_appeal'] = {
                    'score': appeal_score,
                    'desc': appeal_desc,
                    'details': appeal_details
                }

                video_info = result_dict.get('_video_info', {})
                platform = result_dict.get('platform', '').lower()
                
                if video_info and platform:
                    score_result, score_details = calculate_video_score(video_info, platform, appeal_score)
                    result_dict['scores'] = score_details
                    result_dict['scores']['ai_appeal'] = {
                        'score': appeal_score,
                        'desc': appeal_desc,
                        'details': appeal_details
                    }
                    result_dict['_score_result'] = score_result

                print(f"\n[OK] AI任务完成: {result_dict['title'][:30]}...", flush=True)
                print_video_result(result_dict)

            except Exception as e:
                print(f"\n[ERROR] AI任务失败: {result_dict.get('title', '未知')} - {str(e)}", flush=True)
                result_dict['error'] = f"AI处理异常: {str(e)}"
                result_dict['success'] = False


def fetch_subtitle_only(raw_input: str) -> Tuple[str, Optional[dict], str]:
    """仅获取字幕（不进行 AI 评分）"""
    video_id, platform, parse_err = parse_video_url(raw_input)
    if parse_err:
        return raw_input, None, parse_err

    if platform == 'bilibili':
        fetcher = BilibiliSubtitleFetcher(BILI_COOKIE)
        subtitle_data, sub_err = fetcher.fetch(video_id)
        return raw_input, subtitle_data, sub_err or ""
    else:
        subtitle_data, sub_err = fetch_youtube_subtitle(video_id)
        return raw_input, subtitle_data, sub_err or ""


def apply_filter_and_rank(results: List[Dict]) -> List[Dict]:
    """应用筛选和排名"""
    video_list = []
    for r in results:
        if not r.get('success'):
            continue

        video_info = {
            'url': r.get('input', ''),
            'title': r.get('title', ''),
            'platform': r.get('platform', ''),
            'video_id': r.get('video_id', ''),
            'author_id': r.get('author_id', ''),
            'duration': r.get('duration', 0),
            'has_subtitle': r.get('has_subtitle', False),
            'scores': r.get('scores', {}),
            'score_result': r.get('_score_result')
        }
        video_list.append(video_info)

    if not video_list:
        return []

    history = get_selection_history()
    max_videos = CONFIG.get('max_videos_per_week', 8)
    max_per_author = CONFIG.get('max_videos_per_author', 1)

    selected = filter_and_rank_videos(
        video_list=video_list,
        selection_history=history,
        max_videos=max_videos,
        max_per_author=max_per_author
    )

    for video in selected:
        platform = video.get('platform', '').lower()
        author_id = video.get('author_id', '')
        if platform and author_id:
            history.increment_selection_count(platform, author_id)

    return selected
