#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retry_failed_youtube_two_phase.py - YouTube 失败视频两阶段重试工具 (v4.4.0)

v4.4.0 新增：替代"立刻重试"的智能两阶段重试流程。

流程:
  阶段 1: 第一轮处理（跳过失败，不立即重试）
       ↓
  生成第一轮报告 → 写入飞书
       ↓
  阶段 2: 仅重试失败任务（应用 SmartRetryEngine 决策）
       ↓
  追加合并报告 → 写入飞书

特性:
- 错误精确分类（修复 IP被阻止 误报）
- IP 健康度滑动窗口监控
- 时间预算控制（防止一次运行耗 2 小时）
- 视频级问题（无字幕/区域限制）智能跳过，不再触发 IP 阻止级联退避
- 飞书状态分两次写入（避免长重试时数据不一致）

使用方法:
    # 重试全部 YouTube 失败视频（从飞书读取）
    python tools/retry_failed_youtube_two_phase.py

    # 重试指定 URL 列表
    python tools/retry_failed_youtube_two_phase.py --urls url1,url2,url3

    # 调整时间预算（秒）
    python tools/retry_failed_youtube_two_phase.py --budget 1800
"""

import os
import sys
import time
import json
import argparse
from datetime import datetime
from typing import Dict, List, Tuple, Optional

# ==================== 路径配置 ====================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)  # refactor 目录
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "core"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "platforms"))

from console_utils import fix_windows_console
fix_windows_console()

from config import CONFIG, BILIBILI_EXCEL_PATH
from scheduler import VideoScheduler, VideoStatus
from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID, YT_TABLE_ID
from feishu_fields import (
    YT_F_VIDEO_ID, YT_F_SUBTITLE_STATUS, YT_F_TOTAL_SCORE, YT_F_RATING,
    YT_F_AI_REVIEW, YT_SUBTITLE_SUCCESS, YT_SUBTITLE_FAILED,
    YT_AI_RESULT_SAFE, YT_AI_RESULT_UNSAFE, YT_SUBTITLE_PENDING,
    YT_F_SUBTITLE_TEXT,  # 用于写入重试报告
)
from platforms.video_parser import parse_video_url
from platforms.youtube_client import (
    fetch_youtube_subtitle,
    _fetch_youtube_subtitle_via_transcript_api,
)
from video_processor import calculate_video_score, submit_ai_tasks, process_ai_results
from smart_retry import (
    ErrorCategory,
    RetryDecision,
    SmartRetryEngine,
    TwoPhaseRetryCoordinator,
    RetryTask,
    classify_youtube_transcript_error,
)


# ==================== 报告输出 ====================
REPORT_DIR = os.path.join(PROJECT_ROOT, "data", "retry_reports")
os.makedirs(REPORT_DIR, exist_ok=True)


def save_report_to_local(report_text: str, phase: int) -> str:
    """将报告保存到本地文件（便于人工审阅）"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"yt_retry_phase{phase}_{timestamp}.md"
    filepath = os.path.join(REPORT_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report_text)
    return filepath


# ==================== 单视频处理 ====================
def process_single_video_smart(
    url: str,
    engine: SmartRetryEngine,
    coordinator: TwoPhaseRetryCoordinator,
    attempt: int = 1,
) -> Optional[dict]:
    """
    智能处理单个视频（应用 SmartRetryEngine 决策）

    参数:
        url: 视频 URL
        engine: 重试决策引擎
        coordinator: 两阶段协调器
        attempt: 当前是第几轮（1=第一轮，2=第二轮重试）

    返回:
        成功时返回 result_info 字典，失败时返回 None
    """
    video_id, platform, parse_err = parse_video_url(url)

    if parse_err or not video_id or platform != "youtube":
        err_msg = parse_err or "URL 解析失败或非 YouTube 视频"
        coordinator.record(video_id or "unknown", "youtube", success=False,
                          error_category=ErrorCategory.VIDEO_UNAVAILABLE, error_message=err_msg, url=url)
        print(f"  [WARN] 跳过: {err_msg}")
        return None

    # 检查时间预算
    if engine.is_budget_exceeded():
        print(f"\n[STOP] 时间预算已超出 ({engine.get_elapsed_seconds():.0f}s > {engine.budget_seconds:.0f}s)")
        print(f"  建议：本批剩余任务将延后到下次运行处理")
        coordinator.record(video_id, "youtube", success=False,
                          error_category=ErrorCategory.UNKNOWN,
                          error_message="时间预算超出，跳过", url=url)
        return None

    # 检查 IP 健康度
    if engine.should_pause_batch() and attempt == 1:
        print(f"\n[PAUSE] IP 健康度过低 ({engine.ip_health.success_rate:.1%})")
        print(f"  建议: 手动切换代理节点，或等待 10-30 分钟")
        coordinator.record(video_id, "youtube", success=False,
                          error_category=ErrorCategory.IP_BLOCKED,
                          error_message="IP 健康度过低，主动暂停", url=url)
        return None

    print(f"\n[*] [{attempt}/?] 处理: {url}")

    # v4.4.0: 使用 _fetch_youtube_subtitle_via_transcript_api 直接获取 error_category
    data, err, error_category = _fetch_youtube_subtitle_via_transcript_api(video_id)

    if data and data.get('has_subtitle') and data.get('subtitle_text', '').strip():
        # 成功路径
        video_info = {
            'platform': 'YOUTUBE',
            'video_id': video_id,
            'url': url,
            'title': data.get('title', f'YouTube Video {video_id}'),
            'subtitle_text': data['subtitle_text'],
            'has_subtitle': True,
            'duration': data.get('duration', 0),
        }
        coordinator.record(video_id, "youtube", success=True, error_category="", url=url)
        print(f"  [OK] 字幕获取成功! 长度: {len(data['subtitle_text'])} 字符")
        return video_info

    # 失败路径: 应用 SmartRetryEngine 决策
    print(f"  [WARN] 字幕获取失败: category={error_category}, err={err[:60] if err else '无'}")

    # 让决策器判断下一步动作
    decision = engine.decide(error_category, context={"attempt_count": attempt})

    if decision.next_action == "manual_intervention":
        # IP 极差，提示用户
        print(f"\n{'='*60}")
        print(f"  [CRITICAL] IP 状况极差，建议立即人工介入！")
        print(f"  {decision.suggested_user_action}")
        print(f"{'='*60}\n")
        coordinator.record(video_id, "youtube", success=False,
                          error_category=error_category, error_message=err or decision.reason, url=url)
        return None

    if not decision.should_retry:
        # 视频级问题或已超最大重试
        print(f"  [SKIP] {decision.reason}")
        coordinator.record(video_id, "youtube", success=False,
                          error_category=error_category, error_message=err or decision.reason, url=url)
        return None

    # 需要重试（同一次函数调用内重试一次后即返回）
    print(f"  [RETRY] {decision.reason} (等待 {decision.wait_seconds:.0f} 秒)...")
    print(f"  [HINT] {decision.suggested_user_action}")
    time.sleep(decision.wait_seconds)

    # 记录失败
    coordinator.record(video_id, "youtube", success=False,
                      error_category=error_category, error_message=err or decision.reason, url=url)
    return None


# ==================== 飞书同步 ====================
def sync_to_feishu(
    results: List[dict],
    failed_tasks: List[RetryTask],
    feishu: FeishuSync,
    yt_index: Dict[str, str],
    phase: int = 1,
) -> Tuple[int, int]:
    """
    将结果同步到飞书（按阶段分别调用）

    参数:
        results: 本阶段成功的 result_info 列表
        failed_tasks: 本阶段失败的 RetryTask 列表（用于标记状态）
        feishu: FeishuSync 实例
        yt_index: video_id -> record_id 索引
        phase: 1=第一轮, 2=第二轮

    返回:
        (success_count, failed_count)
    """
    success = 0
    failed = 0

    # 阶段 1: 处理成功结果
    for r in results:
        if not r or not r.get('video_id'):
            continue
        video_id = r['video_id']
        record_id = yt_index.get(video_id, "")
        if not record_id:
            print(f"  [WARN] 飞书中未找到 Video ID={video_id} 的记录")
            failed += 1
            continue

        updates = {
            YT_F_SUBTITLE_STATUS: YT_SUBTITLE_SUCCESS,
        }
        # 计算评分
        try:
            score_result, score_details = calculate_video_score(r, "youtube")
            updates[YT_F_TOTAL_SCORE] = round(score_details.get('total', 0), 2)
            grade = score_details.get('grade', 'D')
            if grade:
                for g in ['A', 'B', 'C', 'D']:
                    if g in str(grade):
                        updates[YT_F_RATING] = g
                        break
        except Exception as e:
            print(f"  [WARN] 评分失败: {e}")

        if feishu.update_record(YT_TABLE_ID, record_id, updates):
            success += 1
            print(f"  [OK] 阶段{phase}同步成功: {video_id}")
        else:
            failed += 1
            print(f"  [WARN] 阶段{phase}同步失败: {video_id}")

    # 阶段 2: 处理失败结果（标记为 pending 或 failed）
    for t in failed_tasks:
        if not t.video_id or t.video_id in {r.get('video_id') for r in results if r}:
            continue  # 已在成功列表
        record_id = yt_index.get(t.video_id, "")
        if not record_id:
            continue
        # 失败任务标记为 pending（重试） 或 failed（视频级问题）
        from feishu_fields import YT_SUBTITLE_PENDING
        if t.error_category in ErrorCategory.VIDEO_LEVEL_NO_RETRY:
            status = YT_SUBTITLE_FAILED
        else:
            status = YT_SUBTITLE_PENDING
        if feishu.update_record(YT_TABLE_ID, record_id, {YT_F_SUBTITLE_STATUS: status}):
            print(f"  [INFO] 阶段{phase}标记失败任务: {t.video_id} ({t.error_category}) -> {status}")

    return success, failed


# ==================== 主流程 ====================
def load_failed_youtube_urls() -> List[str]:
    """从飞书读取所有 Subtitle Status != success 的 YouTube 视频 URL"""
    print("[1/3] 从飞书读取 YouTube 失败视频...")
    feishu = FeishuSync()
    feishu.set_bitable_token(BILI_BITABLE_TOKEN)

    records = feishu.get_records(YT_TABLE_ID)
    failed_urls = []
    yt_index = {}

    for rec in records:
        fields = rec.get("fields", {})
        vid = str(fields.get(YT_F_VIDEO_ID, "")).strip()
        if not vid:
            continue
        yt_index[vid] = rec.get("record_id", "")

        subtitle_status = str(fields.get(YT_F_SUBTITLE_STATUS, ""))
        # pending 或 failed 都视为需要重试
        if subtitle_status in (YT_SUBTITLE_PENDING, YT_SUBTITLE_FAILED, ""):
            # 构造 URL
            url = f"https://www.youtube.com/watch?v={vid}"
            failed_urls.append(url)

    print(f"  发现 {len(failed_urls)} 个失败视频 (pending/failed/空)")
    return failed_urls, yt_index


def main():
    parser = argparse.ArgumentParser(
        description="YouTube 失败视频两阶段重试工具 v4.4.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--urls", type=str, default="",
                       help="手动指定 URL 列表（逗号分隔），留空则从飞书读取")
    parser.add_argument("--budget", type=int, default=1800,
                       help="时间预算（秒），默认 1800（30分钟）")
    parser.add_argument("--skip-phase1-writeback", action="store_true",
                       help="跳过阶段 1 后的飞书写入（仅生成本地报告）")
    parser.add_argument("--skip-phase2", action="store_true",
                       help="只跑阶段 1，不重试")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  YouTube 两阶段智能重试 v4.4.0")
    print(f"  时间预算: {args.budget} 秒")
    print("=" * 60)

    # 加载视频列表
    if args.urls:
        urls = [u.strip() for u in args.urls.split(",") if u.strip()]
        _, yt_index = load_failed_youtube_urls()  # 仍读取索引
    else:
        urls, yt_index = load_failed_youtube_urls()

    if not urls:
        print("[INFO] 没有需要重试的 YouTube 视频")
        return

    print(f"\n总共 {len(urls)} 个待处理视频")
    print(f"飞书索引: {len(yt_index)} 条记录\n")

    # 初始化决策引擎
    engine = SmartRetryEngine(budget_seconds=float(args.budget))
    coordinator = TwoPhaseRetryCoordinator(engine=engine)

    feishu = FeishuSync()
    feishu.set_bitable_token(BILI_BITABLE_TOKEN)

    # ============================================================
    # 阶段 1: 第一轮（失败即跳过，不立即重试）
    # ============================================================
    print(f"\n{'='*60}")
    print(f"  阶段 1: 第一轮处理 ({len(urls)} 个视频)")
    print(f"{'='*60}\n")

    phase1_results: List[dict] = []

    for i, url in enumerate(urls, 1):
        print(f"\n--- [{i}/{len(urls)}] ---")
        result = process_single_video_smart(url, engine, coordinator, attempt=1)
        if result:
            phase1_results.append(result)

    # 生成阶段 1 报告
    print(f"\n{'='*60}")
    print(f"  阶段 1 完成: 成功 {len(phase1_results)} / 总数 {len(urls)}")
    print(f"{'='*60}")

    phase1_report = coordinator.generate_phase_report(phase=1)
    print(phase1_report)

    phase1_report_path = save_report_to_local(phase1_report, phase=1)
    print(f"\n[INFO] 阶段 1 报告已保存: {phase1_report_path}")

    # 阶段 1 飞书写入
    failed_phase1 = coordinator.get_failed_tasks()
    if not args.skip_phase1_writeback:
        print(f"\n[*] 阶段 1 飞书同步...")
        sync_to_feishu(phase1_results, failed_phase1, feishu, yt_index, phase=1)

    if args.skip_phase2:
        print(f"\n[DONE] 仅执行阶段 1，跳过阶段 2")
        return

    # ============================================================
    # 阶段 2: 重试失败任务
    # ============================================================
    retry_candidates = coordinator.get_failed_tasks()

    # 过滤出真正可重试的（视频级问题无需重试）
    retry_candidates = [
        t for t in retry_candidates
        if t.error_category not in ErrorCategory.VIDEO_LEVEL_NO_RETRY
    ]

    if not retry_candidates:
        print(f"\n[INFO] 阶段 2 无可重试任务（所有失败都是视频级问题）")
        return

    print(f"\n{'='*60}")
    print(f"  阶段 2: 智能重试 ({len(retry_candidates)} 个失败任务)")
    print(f"{'='*60}\n")

    phase2_results: List[dict] = []

    for i, task in enumerate(retry_candidates, 1):
        print(f"\n--- 重试 [{i}/{len(retry_candidates)}]: {task.video_id} (上轮: {task.error_category}) ---")

        # 应用决策
        decision = engine.decide(task.error_category, context={"attempt_count": 2})
        if not decision.should_retry:
            print(f"  [SKIP] {decision.reason}")
            continue

        print(f"  [WAIT] {decision.reason}")
        if decision.wait_seconds > 0:
            time.sleep(decision.wait_seconds)

        # 重试
        result = process_single_video_smart(
            task.url or f"https://www.youtube.com/watch?v={task.video_id}",
            engine,
            coordinator,
            attempt=2,
        )
        if result:
            phase2_results.append(result)

    # 生成合并报告
    print(f"\n{'='*60}")
    print(f"  阶段 2 完成: 成功 {len(phase2_results)} / 重试 {len(retry_candidates)}")
    print(f"{'='*60}")

    combined_report = coordinator.generate_combined_report()
    print(combined_report)

    combined_report_path = save_report_to_local(combined_report, phase=2)
    print(f"\n[INFO] 合并报告已保存: {combined_report_path}")

    # 阶段 2 飞书写入
    still_failed = coordinator.get_still_failed()
    print(f"\n[*] 阶段 2 飞书同步（追加状态）...")
    sync_to_feishu(phase2_results, still_failed, feishu, yt_index, phase=2)

    # 打印最终状态
    print(f"\n{engine.get_status_report()}")

    # 最终统计
    success_after_retry = coordinator.get_success_after_retry()
    print(f"\n{'='*60}")
    print(f"  [FINAL] 最终结果:")
    print(f"    第一轮直接成功: {len(phase1_results)}")
    print(f"    第二轮重试成功: {len(success_after_retry)}")
    print(f"    最终仍失败:   {len(still_failed)}")
    print(f"    整体成功率:   {(len(phase1_results) + len(success_after_retry)) / len(urls) * 100:.1f}%")
    print(f"  报告文件:")
    print(f"    阶段 1: {phase1_report_path}")
    print(f"    阶段 2: {combined_report_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
