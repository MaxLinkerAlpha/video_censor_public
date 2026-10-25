#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - 视频内容审查系统主入口 (v4.3.0)

重构目标：
- 模块化拆分，职责清晰
- 主脚本只负责流程编排
- 复用 core/ 目录下的现有模块

使用方法：
    python main.py              # 使用默认配置运行
    python main.py --days 3     # 只处理最近3天的视频
"""

import os
import sys
import time
import threading
from typing import List, Dict, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))
from console_utils import fix_windows_console
from version import __version__
fix_windows_console()

from config import CONFIG, BILI_COOKIE, prompt_for_bilibili_cookie
from scheduler import VideoScheduler, VideoStatus
from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID, YT_TABLE_ID
from feishu_fields import (
    BILI_F_SUBTITLE_STATUS, BILI_F_AI_REVIEW, BILI_F_AI_REVIEW_REPORT, BILI_F_TOTAL_SCORE,
    BILI_F_D1, BILI_F_D2, BILI_F_D3, BILI_F_D4, BILI_F_RATING, BILI_F_BVID,
    YT_F_SUBTITLE_STATUS, YT_F_AI_REVIEW, YT_F_AI_REVIEW_REPORT, YT_F_TOTAL_SCORE,
    YT_F_D1, YT_F_D2, YT_F_D3, YT_F_D4, YT_F_RATING, YT_F_VIDEO_ID,
    YT_F_LANGUAGE, YT_F_CHANNEL_ID,
    LANG_FIELD, LANG_CN, LANG_OPTIONS, FIELD_TYPE_SELECT,
    map_subtitle_status_to_bili, map_subtitle_status_to_yt,
    map_censor_result_to_bili, map_censor_result_to_yt,
)
from language_classifier import classify_by_ai, classify_language_code, arbitrate_language

from platforms.video_parser import parse_video_url
from platforms.bilibili_client import BilibiliSubtitleFetcher
from platforms.youtube_client import fetch_youtube_subtitle
from ai_client import call_api
from video_processor import (
    calculate_video_score,
    submit_ai_tasks,
    process_ai_results,
    apply_filter_and_rank,
)
from reporter import (
    print_summary,
    write_report,
    print_video_result,
)


def load_videos_from_feishu(days_filter: int = None) -> Tuple[List[Dict], List[Dict]]:
    """从飞书多维表格获取需要处理的视频"""
    if days_filter is None:
        days_filter = CONFIG.get("human_prescreen_days_filter", 7)

    print(f"[*] 从飞书获取视频 (时间过滤: {days_filter}天)...")

    sync = FeishuSync()

    bili_videos = []
    youtube_videos = []

    for platform, table_id, id_field in [
        ("bilibili", BILI_TABLE_ID, "BV号"),
        ("youtube", YT_TABLE_ID, "Video ID"),
    ]:
        video_data = sync.get_videos_for_human_prescreen(platform, days_filter=days_filter)

        priority = video_data.get("priority", [])
        forced = video_data.get("forced_include", [])

        print(f"    [{platform}] 优先处理: {len(priority)} 个")
        print(f"    [{platform}] 强制入选: {len(forced)} 个")

        for v in priority + forced:
            link = v.get("link", "")
            if isinstance(link, list) and len(link) > 0:
                url = link[0].get("link") or link[0].get("text", "")
            else:
                url = str(link)

            if not url and v.get("video_id"):
                if platform == "bilibili":
                    url = f"https://www.bilibili.com/video/{v['video_id']}"
                else:
                    url = f"https://www.youtube.com/watch?v={v['video_id']}"

            video_info = {
                "url": url,
                "platform": platform,
                "video_id": v.get("video_id", ""),
                "title": v.get("title", ""),
                "human_prescreen": v.get("human_prescreen", ""),
                "d1_score": v.get("d1_score", 0),
                "d2_score": v.get("d2_score", 0),
                "record_id": v.get("record_id", ""),
            }

            if platform == "bilibili":
                bili_videos.append(video_info)
            else:
                youtube_videos.append(video_info)

    return bili_videos, youtube_videos


def _ensure_language_fields(feishu: FeishuSync) -> None:
    """v5.3.5: 确保两表存在 语言_Language 字段 (select, 预置10选项)

    规则: 写入前重新拉取表结构, 确认字段存在与类型, 缺失才创建
    (init_fields 的补充保障 —— human-prescreen 主流程不经过 stage feishu)
    """
    for table_id in (BILI_TABLE_ID, YT_TABLE_ID):
        try:
            fields = feishu.get_fields(table_id)
            if fields and LANG_FIELD not in fields:
                print(f"[*] 表 {table_id} 缺少 {LANG_FIELD} 字段, 自动创建 (select, 10选项)...")
                feishu.create_field(table_id, LANG_FIELD, FIELD_TYPE_SELECT, {"options": LANG_OPTIONS})
        except Exception as e:
            print(f"[WARN] 确保 {LANG_FIELD} 字段失败 ({table_id}): {e}")


def sync_results_to_feishu(results: List[Dict]) -> int:
    """将处理结果同步到飞书多维表格"""
    print(f"\n[*] 开始同步结果到飞书多维表格...")

    try:
        feishu = FeishuSync()
        feishu.set_bitable_token(BILI_BITABLE_TOKEN)
    except Exception as e:
        print(f"  [ERROR] 飞书连接失败: {e}")
        return 0

    # v5.3.5: 写入前确保 语言_Language 字段存在 (重新拉取表结构确认)
    _ensure_language_fields(feishu)

    print(f"[*] 预加载飞书记录索引...")
    bili_records = feishu.get_records(BILI_TABLE_ID)
    yt_records = feishu.get_records(YT_TABLE_ID)

    bili_index: Dict[str, str] = {}
    for rec in bili_records:
        fields = rec.get("fields", {})
        bvid = str(fields.get("BV号", ""))
        if bvid:
            bili_index[bvid] = rec.get("record_id", "")

    yt_index: Dict[str, str] = {}
    for rec in yt_records:
        fields = rec.get("fields", {})
        vid = str(fields.get("Video ID", ""))
        if vid:
            yt_index[vid] = rec.get("record_id", "")

    print(f"    B站记录: {len(bili_index)} 条 | YouTube记录: {len(yt_index)} 条")

    synced_count = 0
    failed_count = 0
    sync_lock = threading.Lock()

    def build_updates(r: Dict) -> tuple:
        video_id = r.get('video_id', '')
        platform = r.get('platform', '')
        if not video_id or not platform:
            return video_id, platform, None, None

        if platform.lower() == "bilibili":
            updates = {
                BILI_F_SUBTITLE_STATUS: map_subtitle_status_to_bili(r.get('has_subtitle')),
            }
            scores = r.get('scores', {})
            if scores:
                updates[BILI_F_TOTAL_SCORE] = round(scores.get('total', 0), 2)
                d1_data = scores.get('d1', {})
                d2_data = scores.get('d2', {})
                d3_data = scores.get('d3', {})
                d4_data = scores.get('d4', {})
                updates[BILI_F_D1] = round(d1_data.get('score', 0), 2)
                updates[BILI_F_D2] = round(d2_data.get('score', 0), 2)
                updates[BILI_F_D3] = round(d3_data.get('score', 0), 2)
                updates[BILI_F_D4] = round(d4_data.get('score', 0), 2)
                grade = scores.get('grade', 'D')
                if grade:
                    for g in ['A', 'B', 'C', 'D']:
                        if g in str(grade):
                            updates[BILI_F_RATING] = g
                            break
            censor_result = r.get('censor_result', '') or ""
            updates[BILI_F_AI_REVIEW] = map_censor_result_to_bili(censor_result)
            # 写入完整AI审核报告（文本类型，不限safe/unsafe）
            if censor_result:
                updates[BILI_F_AI_REVIEW_REPORT] = censor_result
            # v5.3.5: B站视频语言固定为中文
            # 业务决策: B站源均为中文内容, 无字幕视频也直接标记中文
            updates[LANG_FIELD] = LANG_CN
        else:
            updates = {
                YT_F_SUBTITLE_STATUS: map_subtitle_status_to_yt(r.get('has_subtitle')),
            }
            scores = r.get('scores', {})
            if scores:
                updates[YT_F_TOTAL_SCORE] = round(scores.get('total', 0), 2)
                d1_data = scores.get('d1', {})
                d2_data = scores.get('d2', {})
                d3_data = scores.get('d3', {})
                d4_data = scores.get('d4', {})
                updates[YT_F_D1] = round(d1_data.get('score', 0), 2)
                updates[YT_F_D2] = round(d2_data.get('score', 0), 2)
                updates[YT_F_D3] = round(d3_data.get('score', 0), 2)
                updates[YT_F_D4] = round(d4_data.get('score', 0), 2)
                grade = scores.get('grade', 'D')
                if grade:
                    for g in ['A', 'B', 'C', 'D']:
                        if g in str(grade):
                            updates[YT_F_RATING] = g
                            break
            censor_result = r.get('censor_result', '') or ""
            updates[YT_F_AI_REVIEW] = map_censor_result_to_yt(censor_result)
            # 写入完整AI审核报告（文本类型）
            if censor_result:
                updates[YT_F_AI_REVIEW_REPORT] = censor_result
            # v5.3.5: 语言分类写入 (fetch 阶段已 AI 分类; 缺失时兜底补分类)
            lang_val = str(r.get('lang_class', '') or '').strip()
            if not lang_val and r.get('has_subtitle'):
                video_info = r.get('_video_info') or {}
                lang_val = classify_by_ai(
                    (str(video_info.get('subtitle_text', '') or ''))[:1500],
                    str(r.get('title', '') or ''),
                    str(video_info.get('subtitle_language', '') or r.get('subtitle_language', '') or ''),
                )
                r['lang_class'] = lang_val
            if lang_val:
                updates[LANG_FIELD] = lang_val

        return video_id, platform, updates, r.get('title', '')[:30]

    def sync_one(r: Dict) -> bool:
        nonlocal synced_count, failed_count
        video_id, platform, updates, title = build_updates(r)
        if not updates:
            return False

        if platform.lower() == "bilibili":
            record_id = bili_index.get(video_id, "")
            table_id = BILI_TABLE_ID
        else:
            record_id = yt_index.get(video_id, "")
            table_id = YT_TABLE_ID

        if not record_id:
            with sync_lock:
                failed_count += 1
                print(f"  [WARN] 未找到记录: {video_id} ({platform})")
            return False

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            success = feishu.update_record(table_id, record_id, updates)
            if success:
                with sync_lock:
                    synced_count += 1
                    print(f"  [OK] 同步: {title}...")
                return True
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  [RETRY] 同步重试 ({attempt}/{max_retries}): {video_id}，等待{wait}秒...")
                time.sleep(wait)
        with sync_lock:
            failed_count += 1
            print(f"  [WARN] 同步失败(已重试{max_retries}次): {video_id}")
        return False

    success_results = [r for r in results if r.get('success') and r.get('video_id')]
    print(f"[*] 开始同步 {len(success_results)} 条记录 (并发数: 5)...")

    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="sync_worker") as executor:
        futures = [executor.submit(sync_one, r) for r in success_results]
        for future in as_completed(futures):
            future.result()

    print(f"\n[*] 飞书同步完成: {synced_count}/{len(success_results)} 条记录")
    if failed_count > 0:
        print(f"[*] 同步失败: {failed_count} 条")
    return synced_count


# ============================================================
# 模块级函数：供 workflow_orchestrator 按平台分别调用 (v4.5.0)
# ============================================================

# ------------------------------------------------------------
# v5.3.7: 语言多重兜底 —— 频道历史语言索引 (线程安全, 进程内缓存一次)
#   video_lang:   Video ID -> 视频声明音频语言代码 (YT表 Language 字段)
#   channel_hist: Channel ID -> 频道主导语言分类 (历史>=3条且单一语言占比>=80%才有效)
# ------------------------------------------------------------
_channel_ctx_lock = threading.Lock()
_channel_ctx_cache: Optional[Dict[str, Dict[str, str]]] = None


def _norm_feishu_cell(v) -> str:
    """飞书 select/text 字段可能是 str 或 [{'text': ...}], 统一转 str"""
    if isinstance(v, list):
        return str(v[0].get("text", "")).strip() if v else ""
    return str(v or "").strip()


def _get_channel_language_context() -> Dict[str, Dict[str, str]]:
    """惰性构建频道语言上下文索引 (双检锁; 构建失败返回空映射, 不阻塞主流程)"""
    global _channel_ctx_cache
    with _channel_ctx_lock:
        if _channel_ctx_cache is not None:
            return _channel_ctx_cache

        ctx: Dict[str, Dict[str, str]] = {"video_lang": {}, "channel_hist": {}}
        try:
            from collections import Counter
            sync = FeishuSync()
            sync.set_bitable_token(BILI_BITABLE_TOKEN)
            records = sync.get_records(YT_TABLE_ID)
            channel_counter: Dict[str, Counter] = {}
            for rec in records:
                f = rec.get("fields", {})
                vid = _norm_feishu_cell(f.get(YT_F_VIDEO_ID))
                lang_code = _norm_feishu_cell(f.get(YT_F_LANGUAGE))
                if vid and lang_code and lang_code != "N/A":
                    ctx["video_lang"][vid] = lang_code
                # 频道历史: 优先取 AI 分类结果(人工可校正), 缺失时用声明语言代码推断
                ch = _norm_feishu_cell(f.get(YT_F_CHANNEL_ID))
                if not ch:
                    continue
                lang_cls = _norm_feishu_cell(f.get(LANG_FIELD))
                if not lang_cls:
                    lang_cls = classify_language_code(lang_code) or ""
                if lang_cls:
                    channel_counter.setdefault(ch, Counter())[lang_cls] += 1

            # 主导语言判定: 历史>=3条 且 单一语言占比>=80%
            for ch, counter in channel_counter.items():
                total = sum(counter.values())
                if total < 3:
                    continue
                top_cls, top_n = counter.most_common(1)[0]
                if top_n / total >= 0.8:
                    ctx["channel_hist"][ch] = top_cls

            print(f"[LANG] 语言上下文索引: {len(ctx['video_lang'])} 个视频声明语言 | "
                  f"{len(ctx['channel_hist'])} 个频道有稳定历史语言", flush=True)
        except Exception as e:
            # 索引失败只降级(无视频声明语言/无频道历史兜底), 不影响字幕获取主流程
            print(f"[WARN] 频道语言索引构建失败, 语言仲裁降级: {type(e).__name__}: {str(e)[:80]}", flush=True)

        _channel_ctx_cache = ctx
        return ctx


def get_preferred_lang_for(video_id: str) -> str:
    """供外部 (orchestrator 重试探测路径) 获取视频声明语言代码; 查询失败返回 ''"""
    try:
        return _get_channel_language_context()["video_lang"].get(str(video_id or "").strip(), "")
    except Exception:
        return ""


def fetch_and_process_single(raw_input: str, probe_data: Optional[Dict] = None, preferred_lang: Optional[str] = None) -> Dict:
    """获取单个视频字幕并预处理评分 (模块级, v4.5.0)

    v5.3.4: 新增 probe_data 参数 —— 智能重试时复用"探测"结果，
    避免"先探测字幕又 fetch 一次"的双重请求（探测已拿到字幕时不再二次请求）。

    v5.3.7: 新增 preferred_lang 参数 —— 视频声明音频语言代码 (如 "de"):
      - None (默认): 自动从飞书 Language 字段查询 (频道语言上下文索引)
      - "": 强制不使用 (跳过字幕轨语言匹配)
      - 显式代码: 直接使用
    """
    video_id, platform, _ = parse_video_url(raw_input)

    # v5.3.7: YouTube 多重兜底 —— 提前解析声明语言, 供字幕轨优先匹配
    if platform == 'youtube' and preferred_lang is None:
        preferred_lang = get_preferred_lang_for(video_id)

    if platform == 'bilibili':
        fetcher = BilibiliSubtitleFetcher(BILI_COOKIE)
        subtitle_data, sub_err = fetcher.fetch(video_id)
    elif probe_data is not None:
        # v5.3.4: 复用上层探测结果（仅 YouTube 智能重试路径）
        subtitle_data, sub_err = probe_data, None
    else:
        subtitle_data, sub_err = fetch_youtube_subtitle(video_id, preferred_lang or "")

    if sub_err or not subtitle_data:
        # v5.4.3 修复: 补 video_id / error_category —— 缺 video_id 时
        #   workflow_orchestrator._sync_youtube_failed_status 会静默跳过,
        #   导致失败视频在飞书里既没标 failed 也没标 pending (表现为"什么都没写")
        return {
            'input': raw_input,
            'success': False,
            'error': sub_err or "获取字幕失败",
            'platform': platform,
            'video_id': video_id or '',
            'error_category': str((subtitle_data or {}).get('error_category', '') or ''),
            'has_subtitle': False,
        }

    video_info = subtitle_data.copy()
    video_info['platform'] = platform.upper() if platform else 'UNKNOWN'
    video_info['video_id'] = video_id or ''
    video_info['author_id'] = str(subtitle_data.get('mid', '')) if platform == 'bilibili' else str(subtitle_data.get('channel_id', ''))

    has_subtitle = video_info.get('has_subtitle', False)
    duration = video_info.get('duration', 0)
    title_display = video_info.get('title', '未知标题')[:40]

    score_result, score_details = calculate_video_score(video_info, platform)

    print(f"    [VIDEO] {title_display}")
    print(f"    [INFO] 时长: {score_details['d1']['desc']} | 字幕: {'有' if has_subtitle else '无'}")
    print(f"    [INFO] D1:{score_details['d1']['score']:.1f} D2:{score_details['d2']['score']:.1f} D3:{score_details['d3']['score']:.1f}")

    if not has_subtitle:
        return {
            'input': raw_input,
            'success': True,
            'title': video_info.get('title', ''),
            'platform': platform.upper() if platform else 'UNKNOWN',
            'video_id': video_id or '',
            'author_id': video_info.get('author_id', ''),
            'error': '',
            'censor_result': "WARN 该视频无字幕，无法进行内容审核",
            'appeal_result': '',
            'duration': duration,
            'has_subtitle': False,
            '_score_result': score_result,
            'scores': score_details
        }
    else:
        result_info = submit_ai_tasks(video_info, platform)
        result_info['input'] = raw_input
        result_info['duration'] = duration
        result_info['has_subtitle'] = True
        # v5.3.7: 处理单视频时同步完成语言分类 (避免同步阶段积压 AI 请求)
        #   - B站: 固定中文, 不调 AI
        #   - YouTube: 多源仲裁(视频声明语言 hint + AI 文本分类 + 频道历史), 不再单纯"AI 结果为准"
        result_info['subtitle_language'] = str(video_info.get('subtitle_language', '') or '')
        if platform == 'bilibili':
            result_info['lang_class'] = LANG_CN
        else:
            # v5.3.7: 多源仲裁 (多重兜底第 2/3 层)
            #   hint  = 视频声明音频语言(preferred_lang)推断, 缺失时退字幕轨语言推断
            #   ai    = 字幕文本 AI 分类 (classify_by_ai 永不返回空)
            #   hist  = 频道历史主导语言 (>=3条且>=80%占比才有效)
            ai_cls = classify_by_ai(
                (str(video_info.get('subtitle_text', '') or ''))[:1500],
                str(video_info.get('title', '') or ''),
                result_info['subtitle_language'],
            )
            hint_cls = classify_language_code(preferred_lang) \
                or classify_language_code(result_info['subtitle_language']) \
                or ""
            hist_cls = ""
            try:
                ctx = _get_channel_language_context()
                hist_cls = ctx["channel_hist"].get(str(video_info.get('channel_id', '') or ''), "")
            except Exception:
                pass
            result_info['lang_class'] = arbitrate_language(hint_cls, ai_cls, hist_cls) or ai_cls
            if hist_cls and hist_cls != ai_cls:
                print(f"    [LANG] AI={ai_cls} | hint={hint_cls or '无'} | "
                      f"频道历史={hist_cls} -> 仲裁: {result_info['lang_class']}", flush=True)
        return result_info


def run_fetch_phase_videos(
    url_list: List[str],
    phase_name: str,
    interval: float = 0.5,
    scheduler: Optional[VideoScheduler] = None,
    dashboard: Any = None,
) -> List[Dict]:
    """执行一轮字幕获取 (模块级, v4.5.0)

    参数:
        url_list: 待处理的视频 URL 列表
        phase_name: 阶段名称（用于日志）
        interval: 请求间隔（秒）
        scheduler: 可选的 VideoScheduler（用于断点续传追踪）
        dashboard: 可选的仪表盘对象

    返回:
        处理结果列表
    """
    print(f"\n[*] {phase_name} (共 {len(url_list)} 个视频, 间隔 {interval:.1f}秒)...")
    phase_results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=CONFIG.get('bili_max_workers', 2), thread_name_prefix="subtitle_worker") as executor:
        futures = {}
        for raw_input in url_list:
            future = executor.submit(fetch_and_process_single, raw_input)
            futures[future] = raw_input
            if scheduler:
                scheduler.update_status(raw_input, VideoStatus.FETCHING_SUBTITLE)
            time.sleep(interval)

        for future in as_completed(futures):
            raw_input = futures[future]
            completed += 1
            try:
                result = future.result(timeout=300)
                phase_results.append(result)

                if result.get('success'):
                    print(f"\n[OK] [{completed}/{len(url_list)}] {raw_input[:50]}...", flush=True)
                elif '_censor_future' in result:
                    print(f"\n[PENDING] [{completed}/{len(url_list)}] {raw_input[:50]}... (AI任务已提交)", flush=True)
                else:
                    print(f"\n[FAIL] [{completed}/{len(url_list)}] {raw_input[:50]}... ({result.get('error', '')[:30]})", flush=True)

            except Exception as e:
                print(f"\n[ERROR] [{completed}/{len(url_list)}] {raw_input} - {str(e)}", flush=True)
                phase_results.append({
                    'input': raw_input,
                    'success': False,
                    'error': str(e),
                    'platform': 'unknown',
                    'has_subtitle': False,
                })

            if completed % 10 == 0 and dashboard:
                dashboard.print()

    return phase_results


def resolve_ai_futures(results: List[Dict]) -> None:
    """等待并解析所有已提交的 AI 任务 (模块级, v4.5.0)"""
    process_ai_results(results)


# ============================================================
# 原有 main() 函数 — 保留向后兼容 (v4.5.0)
# ============================================================

def main(days_filter: int = None) -> None:
    """主函数 - 视频内容审查流程 (v4.5.0 兼容模式)"""
    print("=" * 60)
    print("视频字幕获取 & AI 内容审查系统 (v" + __version__ + " 重构版)")
    print("=" * 60)

    # 启动前提示用户检查/更新 Bilibili Cookie (v3.4.3)
    prompt_for_bilibili_cookie()

    # 启动前自动验证飞书字段名
    print(f"\n[*] 验证飞书表格字段...")
    try:
        _feishu = FeishuSync()
        _feishu.set_bitable_token(BILI_BITABLE_TOKEN)
        fields_ok = _feishu.validate_required_fields(platform="both")
        if not fields_ok:
            print("\n[!!!] 飞书字段验证未通过，继续运行可能导致写入失败！")
            print("  建议：先修复字段名问题再运行")
            try:
                confirm = input("  是否继续运行？(y/N): ").strip().lower()
                if confirm != 'y':
                    print("[INFO] 用户取消运行")
                    return
            except EOFError:
                print("[INFO] 非交互模式，自动继续")
    except Exception as e:
        print(f"[WARN] 字段验证跳过（飞书连接失败）: {e}")

    print(f"\n[*] 从飞书获取视频列表...")
    bili_videos, youtube_videos = load_videos_from_feishu(days_filter=days_filter)

    total_videos = len(bili_videos) + len(youtube_videos)
    if total_videos == 0:
        print("[ERROR] 没有找到需要处理的视频，程序退出")
        return

    print(f"\n[*] 第一阶段: 并发获取字幕...")
    print(f"    B站视频: {len(bili_videos)} 个")
    print(f"    YouTube视频: {len(youtube_videos)} 个")

    scheduler = VideoScheduler()
    dashboard = scheduler.dashboard

    for v in bili_videos + youtube_videos:
        scheduler.add_video(v['url'])

    resume_tasks = scheduler.get_resume_tasks()
    if resume_tasks:
        print(f"\n[*] 检测到 {len(resume_tasks)} 个待恢复任务（断点续传）")

    dashboard.print()

    total_results = []

    if bili_videos:
        bili_urls = [v['url'] for v in bili_videos]
        bili_results = run_fetch_phase_videos(bili_urls, "B站字幕获取", interval=0.5, scheduler=scheduler, dashboard=dashboard)
        total_results.extend(bili_results)

    if youtube_videos:
        print(f"\n[*] YouTube字幕获取 - 多轮重试机制")
        print(f"    第一轮: 快速获取 (间隔 0.3秒)")

        youtube_urls = [v['url'] for v in youtube_videos]
        round1_results = run_fetch_phase_videos(youtube_urls, "YouTube第一轮", interval=0.3, scheduler=scheduler, dashboard=dashboard)
        total_results.extend(round1_results)

        failed_round1 = [r for r in round1_results if not r.get('has_subtitle', False)]
        if failed_round1:
            print(f"\n[*] 第二轮: 重试失败视频 ({len(failed_round1)} 个, 间隔 1.0秒)")
            failed_urls = [r['input'] for r in failed_round1]

            for r in failed_round1:
                print(f"    - {r['input'][:60]}... ({r.get('error', 'no subtitle')[:30]})")

            round2_results = run_fetch_phase_videos(failed_urls, "YouTube第二轮", interval=1.0, scheduler=scheduler, dashboard=dashboard)

            for r in round2_results:
                if r.get('has_subtitle'):
                    for orig in total_results:
                        if orig['input'] == r['input']:
                            total_results.remove(orig)
                            total_results.append(r)
                            break

            still_failed = [r for r in round2_results if not r.get('has_subtitle', False)]
            if still_failed:
                print(f"\n[*] 最终: {len(still_failed)} 个YouTube视频无法获取字幕，将继续处理（无字幕）")

    # 标记完成状态
    for r in total_results:
        if r.get('success'):
            scheduler.update_status(r['input'], VideoStatus.COMPLETED)

    print(f"\n[*] 第二阶段: 并发处理AI审查和评分任务...")
    resolve_ai_futures(total_results)

    print(f"\n[*] 第三阶段: 筛选和排名...")
    selected_videos = apply_filter_and_rank(total_results)

    if selected_videos:
        print(f"\n[*] 筛选结果: 从 {len(total_results)} 个视频中选出 {len(selected_videos)} 个推荐视频:")
        for i, v in enumerate(selected_videos, 1):
            print(f"  {i}. {v['title'][:40]}... ({v['platform']}) - {v['scores'].get('total', 0):.2f}分")

    print(f"\n[*] 生成最终报告...")

    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Total_Review_Report.md")
    write_report(total_results, report_path)
    print(f"\n[SAVED] 报告已保存: {report_path}")

    dashboard.print()

    sync_results_to_feishu(total_results)
    print_summary(total_results)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="视频内容审查系统")
    parser.add_argument("--days", type=int, default=None, help="只处理最近N天的视频")
    args = parser.parse_args()

    main(days_filter=args.days)
