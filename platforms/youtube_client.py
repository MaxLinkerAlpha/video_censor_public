#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
youtube_client.py - YouTube字幕获取模块 (v4.4.0)

功能：
- 通过 youtube_transcript_api 获取字幕
- 智能限速与重试机制
- IP风控检测与自动降级
- 自动检测代理端口

v4.4.0 更新:
- 修复 IP被阻止 误报：原代码用 'blocked' in err_str.lower() 子串匹配，
  会把 RequestBlocked（视频级限制/无字幕/区域限制）误判为 IP 阻止。
- 新增 _classify_youtube_error() 精确异常分类，单条 transcript 失败不再触发 IP 误判。
- 集成 SmartRetryEngine（core/smart_retry.py）替代"立即重试"。

使用方法：
    from platforms.youtube_client import fetch_youtube_subtitle

    video_info, error = fetch_youtube_subtitle("dQw4w9WgXcQ")
"""

import re
import time
import random
import threading
from datetime import datetime
from typing import Optional, Tuple

import requests

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"))
from config import CONFIG
from console_utils import detect_proxy_port, get_proxy_url, setup_proxy

# v4.4.0: 引入智能重试引擎
try:
    from smart_retry import (
        ErrorCategory,
        RetryDecision,
        SmartRetryEngine,
        classify_youtube_transcript_error,
    )
    _SMART_RETRY_AVAILABLE = True
except ImportError:
    _SMART_RETRY_AVAILABLE = False
    ErrorCategory = None
    RetryDecision = None
    SmartRetryEngine = None
    classify_youtube_transcript_error = None

# v4.4.0: 异常类精确导入（仅做引用探测，运行时可能版本不同）
try:
    from youtube_transcript_api import (
        IpBlocked,
        RequestBlocked,
        TranscriptsDisabled,
        VideoUnavailable,
        NoTranscriptFound,
        TooManyRequests,
        CouldNotRetrieveTranscript,
    )
    _YT_EXC_TYPES_IMPORTED = True
except ImportError:
    # 旧版本可能没有这些细分异常类
    IpBlocked = None
    RequestBlocked = None
    TranscriptsDisabled = None
    VideoUnavailable = None
    NoTranscriptFound = None
    TooManyRequests = None
    CouldNotRetrieveTranscript = None
    _YT_EXC_TYPES_IMPORTED = False

# 设置代理 (youtube_transcript_api 会读取环境变量)
_proxy_url = get_proxy_url()
if _proxy_url:
    os.environ['HTTP_PROXY'] = _proxy_url
    os.environ['HTTPS_PROXY'] = _proxy_url
    os.environ['http_proxy'] = _proxy_url
    os.environ['https_proxy'] = _proxy_url


_last_youtube_api_call = 0
_YOUTUBE_API_MIN_INTERVAL = 3.0
_yt_consecutive_failures = 0
_yt_current_interval = 3.0
_yt_max_interval = 120.0
_yt_success_cooldown = 0
_yt_lock = threading.Lock()


def _rate_limited_youtube_call():
    """YouTube API 请求限速"""
    global _last_youtube_api_call, _yt_current_interval, _yt_success_cooldown
    with _yt_lock:
        current = time.time()
        effective_interval = _yt_current_interval + _yt_success_cooldown
        elapsed = current - _last_youtube_api_call
        if elapsed < effective_interval:
            time.sleep(effective_interval - elapsed)
        _last_youtube_api_call = time.time()


def _on_youtube_success():
    """YouTube API 调用成功回调"""
    global _yt_consecutive_failures, _yt_current_interval, _yt_success_cooldown
    with _yt_lock:
        _yt_consecutive_failures = 0
        if _yt_current_interval > _YOUTUBE_API_MIN_INTERVAL:
            _yt_current_interval = max(_YOUTUBE_API_MIN_INTERVAL, _yt_current_interval * 0.8)
        _yt_success_cooldown = 0


def _on_youtube_failure():
    """YouTube API 调用失败回调"""
    global _yt_consecutive_failures, _yt_current_interval, _yt_success_cooldown
    with _yt_lock:
        _yt_consecutive_failures += 1
        if _yt_consecutive_failures == 1:
            _yt_current_interval = 10.0
            _yt_success_cooldown = 0
        elif _yt_consecutive_failures == 2:
            _yt_current_interval = 25.0
            _yt_success_cooldown = 5 + random.uniform(0, 3)
        elif _yt_consecutive_failures == 3:
            _yt_current_interval = 60.0
            _yt_success_cooldown = 15 + random.uniform(0, 3)
        elif _yt_consecutive_failures >= 4:
            _yt_current_interval = min(_yt_max_interval, _yt_current_interval * 1.5)
            _yt_success_cooldown = 30
    print(f"    [WARN] YouTube风控检测，当前请求间隔: {_yt_current_interval:.1f}秒 | 连续失败: {_yt_consecutive_failures}次", flush=True)


# ================= YouTube 统计数据获取 (v5.4.6) =================
# 背景: 字幕获取路径 (youtube_transcript_api) 不提供 publish_time / views / likes / comments,
#   导致 calculate_video_score 的 D2(时效) 与 D3(互动) 恒为 0, YouTube 评分退化成 (D1+D4)/2。
#   v3.9.0 曾设计"从飞书回填"(见 core/CHANGELOG.md), 但其实现已在重构中丢失,
#   故在此直接调官方 Data API 实时补全。
_YT_STATS_CACHE: dict = {}
_YT_STATS_LOCK = threading.Lock()

YT_STATS_API_URL = "https://www.googleapis.com/youtube/v3/videos"


def _iso_to_epoch(published_at: str) -> int:
    """ISO8601 (如 2026-09-20T10:00:00Z) -> 秒级时间戳; 解析失败返回 0

    注意: calc_d2_freshness_score 期望的是**秒**级时间戳 (与 time.time() 同口径)
    """
    if not published_at:
        return 0
    try:
        return int(datetime.fromisoformat(published_at.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, OSError):
        return 0


def _to_int(val) -> int:
    """API 可能不返回某字段 (如创作者隐藏点赞数), 视为 0"""
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def fetch_youtube_stats(video_id: str, timeout: int = 20) -> dict:
    """获取 YouTube 视频统计数据 (v5.4.6)

    返回 (失败返回空 dict, 由调用方显式告警并降级, 禁止静默):
        publish_time  int  发布时间 (秒级时间戳)
        views / likes / comments  int
        channel_id    str  频道 ID (供 M1 新人系数按作者统计)
        channel_title str
    """
    if not video_id:
        return {}

    # 进程内缓存: 同一视频在 fetch 阶段 / AI 提交阶段 / AI 结果回填阶段会被算 3 次
    with _YT_STATS_LOCK:
        cached = _YT_STATS_CACHE.get(video_id)
    if cached is not None:
        return cached

    api_key = CONFIG.get("yt_api_key", "")
    if not api_key:
        print("    [WARN] YT_API_KEY 未配置 -> 无法补全 YouTube 统计数据 (D2/D3 按 0 计)", flush=True)
        with _YT_STATS_LOCK:
            _YT_STATS_CACHE[video_id] = {}
        return {}

    proxies = {"http": _proxy_url, "https": _proxy_url} if _proxy_url else None
    params = {"part": "snippet,statistics", "id": video_id, "key": api_key}

    for attempt in (1, 2):
        try:
            resp = requests.get(YT_STATS_API_URL, params=params, timeout=timeout, proxies=proxies)
            data = resp.json()
            items = data.get("items") or []
            if not items:
                # 视频不存在/已删除/私享 -> 确定性结果, 缓存以避免同批反复请求
                reason = str((data.get("error") or {}).get("message", "") or "items 为空")
                print(f"    [WARN] {video_id} 取不到统计数据 ({reason[:60]})", flush=True)
                with _YT_STATS_LOCK:
                    _YT_STATS_CACHE[video_id] = {}
                return {}

            snippet = items[0].get("snippet", {})
            stats = items[0].get("statistics", {})
            out = {
                "publish_time": _iso_to_epoch(snippet.get("publishedAt", "")),
                "views": _to_int(stats.get("viewCount")),
                "likes": _to_int(stats.get("likeCount")),
                "comments": _to_int(stats.get("commentCount")),
                "channel_id": str(snippet.get("channelId", "") or ""),
                "channel_title": str(snippet.get("channelTitle", "") or ""),
            }
            with _YT_STATS_LOCK:
                _YT_STATS_CACHE[video_id] = out
            return out
        except (requests.RequestException, ValueError, KeyError) as e:
            # 网络/JSON 异常多为临时问题 -> 重试一次 (不缓存, 保留后续重试机会)
            if attempt == 1:
                time.sleep(1.5)
                continue
            print(f"    [WARN] {video_id} 统计数据请求失败: {type(e).__name__}: {e}", flush=True)

    return {}


def _youtube_base_result(video_id: str, has_subtitle: bool, subtitle_text: str = "", duration: int = 0, title: str = "", subtitle_language: str = "") -> dict:
    """构建YouTube基础结果字典

    v5.3.5: 新增 subtitle_language —— 字幕语言代码 (取自 transcript.language_code)
    """
    return {
        'title': title or f"YouTube Video {video_id}",
        'author': 'Unknown',
        'channel_id': '',
        'publish_time': 0,
        'duration': duration,
        'views': 0,
        'likes': 0,
        'comments': 0,
        'danmaku': 0,
        'subtitle_text': subtitle_text,
        'url': f"https://www.youtube.com/watch?v={video_id}",
        'subtitle_language': subtitle_language,
        'has_subtitle': has_subtitle
    }


def _process_transcript_entries(transcript_entries, video_id: str, language_code: str = "") -> Tuple[dict, str]:
    """处理字幕条目，提取文本和时长信息

    v5.3.5: 新增 language_code 参数, 随结果存入 subtitle_language
    """
    transcript_data = list(transcript_entries)
    lines = []
    for entry in transcript_data:
        text = getattr(entry, 'text', None)
        if text is None and isinstance(entry, dict):
            text = entry.get('text', '')
        text = str(text or '').replace('\r', '').replace('\n', ' ').strip()
        if text:
            lines.append(text)
    subtitle_text = re.sub(r'\s+', ' ', " ".join(lines)).strip()
    if not subtitle_text:
        return _youtube_base_result(video_id, False, subtitle_language=language_code), ""

    duration = 0
    if transcript_data:
        last_entry = transcript_data[-1]
        start = getattr(last_entry, 'start', None)
        if start is None and isinstance(last_entry, dict):
            start = last_entry.get('start')
        length = getattr(last_entry, 'duration', None)
        if length is None and isinstance(last_entry, dict):
            length = last_entry.get('duration')
        if isinstance(start, (int, float)) and isinstance(length, (int, float)):
            duration = int(start + length)
    return _youtube_base_result(video_id, True, subtitle_text=subtitle_text, duration=duration, subtitle_language=language_code), ""


def _classify_youtube_error(err: Exception) -> str:
    """
    v4.4.0: 精确分类 youtube-transcript-api 抛出的异常

    返回值（与 smart_retry.ErrorCategory 对齐）:
      - "ip_blocked"        - 真实 IP 被阻止（IpBlocked 异常或 429 + IP 提示）
      - "rate_limit"        - 频率限制（429 TooManyRequests）
      - "no_subtitle"       - 视频无字幕（NoTranscriptFound / TranscriptsDisabled）
      - "region_locked"     - 区域限制（RequestBlocked 但不是 IP）
      - "video_unavailable" - 视频不可用（私有/已删除）
      - "transient_network" - 临时网络错误（超时/连接重置）
      - "unknown"           - 未知错误
    """
    if _SMART_RETRY_AVAILABLE:
        return classify_youtube_transcript_error(err)
    # 退化实现：仅依赖字符串精确匹配
    err_str = str(err)
    err_type_name = type(err).__name__
    if err_type_name == "IpBlocked" or err_str.startswith("IpBlocked"):
        return "ip_blocked"
    if err_type_name == "TooManyRequests" or "429" in err_str or "Too Many Requests" in err_str:
        return "rate_limit"
    if err_type_name in ("NoTranscriptFound", "TranscriptsDisabled"):
        return "no_subtitle"
    if err_type_name == "VideoUnavailable":
        return "video_unavailable"
    if err_type_name == "RequestBlocked":
        # 注意：RequestBlocked 绝不是 IP 阻止！可能是区域/视频级限制
        return "region_locked"
    if err_type_name == "CouldNotRetrieveTranscript":
        return "no_subtitle"
    return "unknown"


def _ordered_transcripts(transcripts: list, preferred_lang: str = "") -> list:
    """v5.3.7: 按优先级排序字幕轨候选 (纯函数, 便于自检)

    优先级 (多重兜底第 1 层 —— 字幕轨源头匹配):
      1. language_code 与 preferred_lang (视频声明音频语言) 匹配的轨, 手动优先于自动
      2. 其余手动轨 (原逻辑: 按列表顺序)
      3. 其余自动生成轨
    """
    from language_utils import normalize_language

    preferred_norm = normalize_language(preferred_lang) if preferred_lang else ""
    has_valid_preferred = preferred_norm and preferred_norm != "N/A"

    manual = [t for t in transcripts if not getattr(t, "is_generated", False)]
    generated = [t for t in transcripts if getattr(t, "is_generated", False)]

    ordered: list = []
    if has_valid_preferred:
        # 归一化后匹配 (de-DE / de / DE 都归一为 de), 手动轨优先
        for t in manual:
            if normalize_language(str(getattr(t, "language_code", "") or "")) == preferred_norm:
                ordered.append(t)
        for t in generated:
            if normalize_language(str(getattr(t, "language_code", "") or "")) == preferred_norm:
                ordered.append(t)

    # 其余候选: 手动 -> 自动 (排除已入选的, 保持原列表顺序)
    ordered += [t for t in manual if t not in ordered]
    ordered += [t for t in generated if t not in ordered]
    return ordered


def _fetch_youtube_subtitle_via_transcript_api(video_id: str, preferred_lang: str = "") -> Tuple[Optional[dict], str, str]:
    """
    通过 transcript API 获取 YouTube 字幕

    优先获取与 preferred_lang 匹配的字幕轨 (v5.3.7), 没有则获取原声字幕,
    再退自动生成字幕, 不翻译

    v5.2.3 重要修复:
    - 合并 3 次 list() 调用为 1 次，避免重复 HTTP 请求导致的网络错误
    - 对 list() 失败添加 3 次 retry (2s/4s/8s)，区分 transient vs permanent
    - 新版 youtube_transcript_api 兼容: FetchedTranscriptSnippet 是对象非 dict

    v5.3.7: 新增 preferred_lang —— 视频声明音频语言代码 (如 "de"),
    字幕轨选择时优先匹配该语言, 修复"德语视频取到英文字幕轨导致 AI 误判语言"问题

    返回：(视频信息字典, 错误信息, 错误分类)
      错误分类: "ip_blocked" / "rate_limit" / "no_subtitle" /
                "region_locked" / "video_unavailable" / "transient_network" / "unknown" / ""
    """
    error_category = ""
    try:
        _rate_limited_youtube_call()
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt_api = YouTubeTranscriptApi()

        # === 阶段 1: 获取字幕列表（仅调用一次，带 retry）===
        # v5.2.3: 合并原来的 3 次 list() 为 1 次
        transcript_list_obj = None
        list_call_failed = False

        for attempt in range(1, 4):
            try:
                transcript_list_obj = ytt_api.list(video_id)
                break  # 成功，跳出重试循环
            except Exception as e:
                cat = _classify_youtube_error(e)
                if cat == "ip_blocked":
                    print(f"    [ERROR] 列出字幕列表失败 - IP被封禁，或视频就没有字幕: {str(e)[:100]}")
                    return _youtube_base_result(video_id, False), "", "ip_blocked"
                elif cat in ("no_subtitle", "video_unavailable", "region_locked"):
                    # 永久性错误，不重试
                    print(f"    [WARN] 列出字幕列表失败 - {cat}: {str(e)[:100]}")
                    return _youtube_base_result(video_id, False), "", cat
                elif cat in ("rate_limit", "transient_network", "unknown"):
                    if attempt < 3:
                        wait = 2 ** attempt
                        print(f"    [WARN] list() 失败 ({cat}, retry {attempt}/3), {wait}s后重试: {str(e)[:80]}")
                        time.sleep(wait)
                        continue
                    else:
                        print(f"    [WARN] list() 重试3次后仍失败 ({cat}): {str(e)[:100]}")
                        list_call_failed = True
                        error_category = cat

        if list_call_failed:
            pass  # 回退到 fetch()
        elif transcript_list_obj is None:
            error_category = "unknown"
        else:
            # v5.3.7: 统一候选轨优先级 (preferred匹配手动 > preferred匹配自动 > 其余手动 > 其余自动),
            # 逐个尝试 fetch, 任一成功即返回
            for transcript in _ordered_transcripts(list(transcript_list_obj), preferred_lang):
                try:
                    transcript_entries = transcript.fetch()
                    if transcript_entries:
                        track_kind = "自动生成" if getattr(transcript, "is_generated", False) else "原声"
                        print(f"    [INFO] 获取到{track_kind}字幕 ({transcript.language_code})")
                        # v5.3.5: 将字幕语言代码随结果带出
                        return _process_transcript_entries(
                            transcript_entries, video_id,
                            str(getattr(transcript, 'language_code', '') or ''),
                        ) + ("",)
                except Exception as e:
                    cat = _classify_youtube_error(e)
                    if cat == "ip_blocked":
                        track_kind = "自动生成" if getattr(transcript, "is_generated", False) else "原声"
                        print(f"    [ERROR] {track_kind}字幕 - IP被封禁，或视频就没有字幕 ({transcript.language_code}): {str(e)[:80]}")
                        return _youtube_base_result(video_id, False), "", "ip_blocked"
                    else:
                        print(f"    [WARN] 字幕轨获取失败 ({transcript.language_code}, {cat}): {str(e)[:60]}")
                        continue

        # 阶段 4: fallback - 直接 fetch()
        try:
            transcript_entries = ytt_api.fetch(video_id)
            if transcript_entries:
                print(f"    [INFO] 通过 fetch() 获取到字幕")
                # v5.3.6: fallback 路径同样带出字幕语言代码 (FetchedTranscript 对象带 language_code 属性)
                return _process_transcript_entries(
                    transcript_entries, video_id,
                    str(getattr(transcript_entries, 'language_code', '') or ''),
                ) + ("",)
        except Exception as e:
            cat = _classify_youtube_error(e)
            if cat == "ip_blocked":
                error_category = "ip_blocked"
                print(f"    [ERROR] fetch() - IP被封禁，或视频就没有字幕: {str(e)[:100]}")
            elif cat == "no_subtitle":
                error_category = "no_subtitle"
                print(f"    [WARN] fetch() 失败 - 视频无字幕: {str(e)[:80]}")
            elif cat == "region_locked":
                error_category = "region_locked"
                print(f"    [WARN] fetch() 失败 - 区域限制: {str(e)[:80]}")
            elif cat == "rate_limit":
                error_category = "rate_limit"
                print(f"    [WARN] fetch() 失败 - 频率限制: {str(e)[:80]}")
            else:
                error_category = cat
                print(f"    [WARN] fetch() 失败: {str(e)[:80]}")

        # 所有方法都未取到字幕
        return _youtube_base_result(video_id, False), "", error_category or "no_subtitle"
    except Exception as e:
        return None, f"youtube_transcript_api获取失败: {str(e)}", "unknown"


def _stamp_error_category(data: Optional[dict], video_id: str, category: str) -> dict:
    """v5.4.3: 确保失败结果字典带上 error_category

    为什么需要: fetch_youtube_subtitle 只返回 (data, err), 把分类信息丢掉了;
    上层 (workflow_orchestrator._sync_youtube_failed_status) 需要 classification
    才能区分"视频级终态(no_subtitle)"与"可重试(ip_blocked)", 否则飞书状态标记失效。
    """
    d = data or _youtube_base_result(video_id, False)
    d["error_category"] = category
    return d


def fetch_youtube_subtitle(video_id: str, preferred_lang: str = "") -> Tuple[Optional[dict], str]:
    """
    获取YouTube视频字幕

    参数：
        video_id: YouTube 视频 ID
        preferred_lang: 视频声明音频语言代码 (如 "de", v5.3.7); 字幕轨优先匹配该语言

    返回：
        (视频信息字典, 错误信息)

    v4.4.0 行为变更:
    - IP 被阻止时不再"立刻重试"，而是返回错误分类，由上层调用方（SmartRetryEngine）决策
    - 单条 transcript 失败不会再被错误标记为 IP 阻止
    """
    data, err, error_category = _fetch_youtube_subtitle_via_transcript_api(video_id, preferred_lang)

    if data and data.get('has_subtitle') and data.get('subtitle_text', '').strip():
        _on_youtube_success()
        return data, ""

    # v4.4.0: 仅当 error_category == "ip_blocked" 才真正调用失败回调
    # 其他类型（no_subtitle / region_locked / video_unavailable）是视频级问题
    if error_category == "ip_blocked":
        print(f"    [ERROR] IP被封禁，或视频就没有字幕；请更换代理节点后重跑失败视频", flush=True)
        _on_youtube_failure()
        return (_stamp_error_category(data, video_id, error_category),
                err or "IP被封禁，或视频就没有字幕")

    if error_category == "rate_limit":
        # 429 限流 - 走失败回调，触发速率拉长
        _on_youtube_failure()
        return (_stamp_error_category(data, video_id, error_category),
                err or "频率限制(429)")

    # 其他类型（no_subtitle / region_locked / video_unavailable / unknown / transient_network）
    # 不再触发速率回调（因为不是 IP 问题），仅返回空结果
    if error_category in ("no_subtitle", "region_locked", "video_unavailable"):
        return (_stamp_error_category(data, video_id, error_category),
                err or error_category)

    # 未知错误 → 走失败回调
    _on_youtube_failure()
    return (_stamp_error_category(data, video_id, error_category or "unknown"),
            err or "字幕获取失败")
