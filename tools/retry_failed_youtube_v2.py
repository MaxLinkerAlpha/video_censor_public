#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retry_failed_youtube_v2.py - 使用混合策略重试失败的YouTube视频 (v3.4.3 → v4.4.0)

混合策略：
1. 先尝试 youtube_transcript_api（更快）
2. 失败时回退到 yt-dlp（更稳定，支持更多格式）
3. 检测到IP阻止时自动延长冷却时间
4. 智能风控检测和退避

v4.4.0 更新（IP 误报修复 + 智能重试）:
- 错误分类接入 core/smart_retry.py，修复 'IpBlocked'/'RequestBlocked' 子串误判
- 视频级问题（无字幕/区域限制）不再触发 IP 阻止级联退避
- 保留 RateLimitTracker 作为旧版兼容（建议迁移到 retry_failed_youtube_two_phase.py）
"""

import os
import sys
import time
import json
import subprocess
from typing import Dict, List, Tuple, Optional
from datetime import datetime

# 路径配置：tools/ 目录下的脚本需要访问 core/, platforms/ 等
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)  # 项目根目录
sys.path.insert(0, PROJECT_ROOT)            # 添加根目录以找到 core/, platforms/
sys.path.insert(0, os.path.join(PROJECT_ROOT, "core"))  # 添加core目录
from config import CONFIG, BILI_COOKIE
from console_utils import fix_windows_console
fix_windows_console()

from scheduler import VideoScheduler, VideoStatus
from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID, YT_TABLE_ID
from feishu_fields import (
    YT_F_VIDEO_ID, YT_F_SUBTITLE_STATUS, YT_F_TOTAL_SCORE, YT_F_RATING,
    YT_F_AI_REVIEW, YT_F_AI_REVIEW_REPORT,
    YT_SUBTITLE_SUCCESS, YT_SUBTITLE_FAILED,
    YT_AI_RESULT_SAFE, YT_AI_RESULT_UNSAFE, YT_SUBTITLE_PENDING,
)
from platforms.video_parser import parse_video_url
from video_processor import calculate_video_score, submit_ai_tasks, process_ai_results

# v4.4.0: 接入智能分类器
sys.path.insert(0, os.path.join(PROJECT_ROOT, "core"))
try:
    from smart_retry import ErrorCategory, classify_youtube_transcript_error
    _SMART_RETRY_AVAILABLE = True
except ImportError:
    _SMART_RETRY_AVAILABLE = False
    ErrorCategory = None
    def classify_youtube_transcript_error(err):
        # 退化实现: 严格异常类型检测，绝不进行子串匹配
        err_type_name = type(err).__name__
        if err_type_name == "IpBlocked":
            return "ip_blocked"
        if err_type_name == "RequestBlocked":
            return "region_locked"  # 关键: 旧代码会误判为 ip_blocked
        return "unknown"

# 待重试的视频 URL, 运行时由命令行或 --file 提供 (不写死在代码里)
# 用法: python retry_failed_youtube_v2.py <url> [<url> ...]
#       python retry_failed_youtube_v2.py --file failed_urls.txt
FAILED_VIDEOS: List[str] = []


def _collect_urls() -> List[str]:
    """从命令行参数或 --file 指定的文本文件收集待重试 URL"""
    args = sys.argv[1:]
    if args and args[0] == "--file":
        if len(args) < 2:
            return []
        with open(args[1], "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    return [a for a in args if a.startswith("http")]

# 风控状态跟踪
class RateLimitTracker:
    def __init__(self):
        self.consecutive_failures = 0
        self.base_delay = 5  # 基础延迟（秒）
        self.max_delay = 300  # 最大延迟（5分钟）
        self.last_request_time = 0
        self.ip_blocked_count = 0
    
    def get_delay(self) -> float:
        """根据失败次数计算延迟"""
        if self.consecutive_failures == 0:
            return self.base_delay
        # 指数退避：2^(n-1) * base_delay
        delay = min((2 ** (self.consecutive_failures - 1)) * self.base_delay, self.max_delay)
        return delay
    
    def record_success(self):
        """记录成功，重置失败计数"""
        self.consecutive_failures = max(0, self.consecutive_failures - 1)
        self.ip_blocked_count = max(0, self.ip_blocked_count - 1)
    
    def record_failure(self, is_ip_blocked: bool = False):
        """记录失败，增加计数"""
        self.consecutive_failures += 1
        if is_ip_blocked:
            self.ip_blocked_count += 1
            # IP被阻止时，额外增加延迟
            self.consecutive_failures += 2
    
    def wait(self):
        """等待适当的时间"""
        delay = self.get_delay()
        if delay > 0:
            print(f"    [WAIT] 等待 {delay:.1f} 秒 (连续失败: {self.consecutive_failures}次)...")
            time.sleep(delay)
        self.last_request_time = time.time()


def get_subtitle_with_transcript_api(video_id: str, tracker: RateLimitTracker) -> Tuple[bool, str, Dict]:
    """
    使用 youtube_transcript_api 获取字幕
    
    返回: (success, subtitle_text, metadata)
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt_api = YouTubeTranscriptApi()
        
        try:
            transcript_list = ytt_api.list(video_id)
            
            # 优先获取手动字幕
            for transcript in transcript_list:
                if not transcript.is_generated:
                    try:
                        entries = transcript.fetch()
                        lines = [entry.text for entry in entries]
                        tracker.record_success()
                        return True, ' '.join(lines), {
                            "language": transcript.language,
                            "is_auto": False,
                            "source": "transcript_api"
                        }
                    except (ValueError, TypeError, RuntimeError):
                        continue
            
            # 获取自动生成字幕
            for transcript in transcript_list:
                if transcript.is_generated:
                    try:
                        entries = transcript.fetch()
                        lines = [entry.text for entry in entries]
                        tracker.record_success()
                        return True, ' '.join(lines), {
                            "language": transcript.language,
                            "is_auto": True,
                            "source": "transcript_api"
                        }
                    except (ValueError, TypeError, RuntimeError):
                        continue
            
            return False, "字幕获取失败", {}
            
        except Exception as e:
            err_str = str(e)
            # v4.4.0: 使用智能分类器，修复 IP 误报
            # 旧代码: is_ip_blocked = 'IpBlocked' in err_str or 'RequestBlocked' in err_str
            # 问题: 把 RequestBlocked (视频级) 误判为 IP 阻止
            error_category = classify_youtube_transcript_error(e)
            is_ip_blocked = (error_category == ErrorCategory.IP_BLOCKED) if _SMART_RETRY_AVAILABLE else (error_category == "ip_blocked")
            tracker.record_failure(is_ip_blocked)
            return False, f"API错误: {err_str[:100]}", {"is_ip_blocked": is_ip_blocked, "error_category": error_category}
            
    except ImportError:
        return False, "youtube_transcript_api 未安装", {}
    except Exception as e:
        return False, f"异常: {str(e)[:100]}", {}


def get_subtitle_with_ytdlp(video_id: str, tracker: RateLimitTracker) -> Tuple[bool, str, Dict]:
    """
    使用 yt-dlp 获取字幕
    
    返回: (success, subtitle_text, metadata)
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    try:
        # 方法1：尝试获取自动生成字幕
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-sub",
            "--sub-langs", "en,zh,ja,de,ru,pt,es,fr,it,ko",
            "--convert-subs", "srt",
            "--output", f"subtitle_{video_id}",
            "--no-warnings",
            "--proxy", "http://127.0.0.1:17891",
            url
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        # 检查生成的字幕文件
        for lang in ["en", "zh", "ja", "de", "ru", "pt", "es", "fr", "it", "ko"]:
            sub_file = f"subtitle_{video_id}.{lang}.srt"
            if os.path.exists(sub_file):
                with open(sub_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                try:
                    os.remove(sub_file)
                except (OSError, IOError):
                    pass
                
                # 解析 SRT
                lines = []
                for line in content.split('\n'):
                    line = line.strip()
                    if line and not line.isdigit() and '-->' not in line and not line.startswith('<'):
                        lines.append(line)
                
                tracker.record_success()
                return True, ' '.join(lines), {
                    "language": lang,
                    "is_auto": True,
                    "source": "yt-dlp"
                }
        
        # 方法2：尝试获取手动字幕
        cmd2 = [
            "yt-dlp",
            "--skip-download",
            "--write-sub",
            "--sub-langs", "en,zh,ja,de,ru,pt,es,fr,it,ko",
            "--convert-subs", "srt",
            "--output", f"subtitle_{video_id}",
            "--no-warnings",
            "--proxy", "http://127.0.0.1:17891",
            url
        ]
        
        result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=60)
        
        for lang in ["en", "zh", "ja", "de", "ru", "pt", "es", "fr", "it", "ko"]:
            sub_file = f"subtitle_{video_id}.{lang}.srt"
            if os.path.exists(sub_file):
                with open(sub_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                try:
                    os.remove(sub_file)
                except (OSError, IOError):
                    pass
                
                lines = []
                for line in content.split('\n'):
                    line = line.strip()
                    if line and not line.isdigit() and '-->' not in line and not line.startswith('<'):
                        lines.append(line)
                
                tracker.record_success()
                return True, ' '.join(lines), {
                    "language": lang,
                    "is_auto": False,
                    "source": "yt-dlp"
                }
        
        # 检查是否是429错误
        if "429" in result.stderr or "Too Many Requests" in result.stderr:
            tracker.record_failure(is_ip_blocked=True)
            return False, "YouTube速率限制(429)", {"is_ip_blocked": True}
        
        return False, "无可用字幕", {}
        
    except subprocess.TimeoutExpired:
        return False, "请求超时", {}
    except FileNotFoundError:
        return False, "yt-dlp 未安装", {}
    except Exception as e:
        return False, f"异常: {str(e)[:100]}", {}


def fetch_youtube_subtitle_hybrid(video_id: str, tracker: RateLimitTracker) -> Tuple[bool, str, Dict]:
    """
    混合策略获取YouTube字幕
    
    1. 先尝试 youtube_transcript_api（更快）
    2. 失败时回退到 yt-dlp（更稳定）
    3. 都失败时返回错误
    """
    print(f"    [*] 尝试 youtube_transcript_api...")
    success, text, meta = get_subtitle_with_transcript_api(video_id, tracker)
    
    if success:
        print(f"    [OK] transcript_api 成功!")
        return True, text, meta
    
    print(f"    [WARN] transcript_api 失败: {text[:80]}")
    
    # 如果是IP被阻止，先等待再尝试yt-dlp
    if meta.get("is_ip_blocked"):
        tracker.wait()
    
    print(f"    [*] 回退到 yt-dlp...")
    success, text, meta = get_subtitle_with_ytdlp(video_id, tracker)
    
    if success:
        print(f"    [OK] yt-dlp 成功!")
        return True, text, meta
    
    print(f"    [WARN] yt-dlp 失败: {text[:80]}")
    return False, text, meta


def process_single_video(url: str, tracker: RateLimitTracker) -> Dict:
    """处理单个视频"""
    video_id, platform, parse_err = parse_video_url(url)
    
    if parse_err or not video_id:
        return {
            'input': url,
            'success': False,
            'error': parse_err or "解析失败",
            'platform': 'youtube',
        }
    
    print(f"\n[*] 处理: {url}")
    
    # 等待适当时间（根据风控状态）
    tracker.wait()
    
    # 使用混合策略获取字幕
    success, subtitle_text, meta = fetch_youtube_subtitle_hybrid(video_id, tracker)
    
    if not success:
        print(f"  [FAIL] 字幕获取失败: {subtitle_text}")
        return {
            'input': url,
            'success': False,
            'error': subtitle_text,
            'platform': 'youtube',
            'video_id': video_id,
        }
    
    # 构建视频信息
    video_info = {
        'platform': 'YOUTUBE',
        'video_id': video_id,
        'url': url,
        'title': f'YouTube Video {video_id}',
        'subtitle_text': subtitle_text,
        'has_subtitle': True,
        'duration': 0,  # 暂时未知
    }
    
    print(f"  [OK] 字幕获取成功! 长度: {len(subtitle_text)}字符")
    print(f"  [INFO] 语言: {meta.get('language', 'unknown')} | 来源: {meta.get('source', 'unknown')}")
    
    # 提交AI任务
    result_info = submit_ai_tasks(video_info, 'youtube')
    result_info['input'] = url
    result_info['video_id'] = video_id
    
    print(f"  [OK] AI任务已提交")
    return result_info


def sync_results_to_feishu(results: List[Dict]) -> int:
    """将处理结果同步到飞书多维表格"""
    print(f"\n[*] 开始同步结果到飞书多维表格...")
    
    try:
        feishu = FeishuSync()
        feishu.set_bitable_token(BILI_BITABLE_TOKEN)
    except Exception as e:
        print(f"  [ERROR] 飞书连接失败: {e}")
        return 0
    
    # 预加载YouTube记录索引
    yt_records = feishu.get_records(YT_TABLE_ID)
    yt_index: Dict[str, str] = {}
    for rec in yt_records:
        fields = rec.get("fields", {})
        vid = str(fields.get(YT_F_VIDEO_ID, ""))
        if vid:
            yt_index[vid] = rec.get("record_id", "")
    
    print(f"    YouTube记录: {len(yt_index)} 条")
    
    synced_count = 0
    failed_count = 0
    
    for r in results:
        if not r.get('success') or not r.get('video_id'):
            continue
        
        video_id = r['video_id']
        record_id = yt_index.get(video_id, "")
        
        if not record_id:
            print(f"  [WARN] 未找到记录: {video_id}")
            failed_count += 1
            continue
        
        updates = {
            YT_F_SUBTITLE_STATUS: YT_SUBTITLE_SUCCESS if r.get('has_subtitle') else YT_SUBTITLE_FAILED,
        }
        
        scores = r.get('scores', {})
        if scores:
            updates[YT_F_TOTAL_SCORE] = round(scores.get('total', 0), 2)
            grade = scores.get('grade', 'D')
            if grade:
                for g in ['A', 'B', 'C', 'D']:
                    if g in str(grade):
                        updates[YT_F_RATING] = g
                        break
        
        censor_result = r.get('censor_result', '') or ""
        if "[OK" in censor_result or "未发现核心违规" in censor_result:
            updates[YT_F_AI_REVIEW] = YT_AI_RESULT_SAFE
        elif "违规" in censor_result or "RED" in censor_result:
            updates[YT_F_AI_REVIEW] = YT_AI_RESULT_UNSAFE
        else:
            updates[YT_F_AI_REVIEW] = YT_SUBTITLE_PENDING
        # 写入完整AI审核报告（文本类型）
        if censor_result:
            updates[YT_F_AI_REVIEW_REPORT] = censor_result
        
        # 重试3次
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            success = feishu.update_record(YT_TABLE_ID, record_id, updates)
            if success:
                synced_count += 1
                print(f"  [OK] 同步: {video_id}")
                break
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  [RETRY] 同步重试 ({attempt}/{max_retries}): {video_id}，等待{wait}秒...")
                time.sleep(wait)
        else:
            failed_count += 1
            print(f"  [WARN] 同步失败(已重试{max_retries}次): {video_id}")
    
    print(f"\n[*] 飞书同步完成: {synced_count}/{len([r for r in results if r.get('success')])} 条记录")
    if failed_count > 0:
        print(f"[*] 同步失败: {failed_count} 条")
    return synced_count


def main():
    global FAILED_VIDEOS
    FAILED_VIDEOS = _collect_urls()
    if not FAILED_VIDEOS:
        print("[ERROR] 未提供待重试的视频 URL。")
        print("        用法: python retry_failed_youtube_v2.py <url> [<url> ...]")
        print("              python retry_failed_youtube_v2.py --file failed_urls.txt")
        return 1

    print("=" * 60)
    print("使用混合策略重试失败的YouTube视频 (v3.4.3)")
    print("=" * 60)
    print(f"\n策略: transcript_api -> yt-dlp -> 智能冷却")
    print(f"代理: http://127.0.0.1:17891")
    print(f"视频数: {len(FAILED_VIDEOS)}")
    print()
    
    tracker = RateLimitTracker()
    results = []
    
    for i, url in enumerate(FAILED_VIDEOS, 1):
        print(f"\n[{i}/{len(FAILED_VIDEOS)}] 开始处理...")
        result = process_single_video(url, tracker)
        results.append(result)
    
    print(f"\n[*] 第二阶段: 处理AI审查结果...")
    process_ai_results(results)
    
    # 打印结果
    success_count = sum(1 for r in results if r.get('success'))
    print(f"\n[*] 处理完成: {success_count}/{len(results)} 个视频成功")
    
    # 同步到飞书
    if success_count > 0:
        sync_results_to_feishu(results)
    
    print("\n" + "=" * 60)
    print("[OK] 重试完成")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
