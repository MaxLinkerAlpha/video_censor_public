#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reset_today_selected_videos.py - 重置今天被错误标记为"历史视频"的 YouTube 视频

修复目标：
  由于之前两次流程运行，部分被标记为 Selected 但未完成 AI 审核的 YouTube 视频
  被时间过滤跳过（"历史视频"）。本脚本重置它们的 Subtitle Status 为 pending，
  使下次工作流能重新处理这些视频。

安全限制（仅影响今天标记的视频）：
  ✓ Date Created = 今天 (UTC+8 00:00:00 之后)
  ✓ Human Prescreen = Selected (仅 Selected，不含 Priority)
  ✓ AI Review Report 为空 (尚未完成 AI 审核)
  ✓ 只修改 Subtitle Status → pending (不碰其他字段)
"""

import sys
import os
from datetime import datetime, timedelta, timezone

# 添加 core 目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID
from feishu_fields import (
    YT_F_HUMAN_PRESCREEN, YT_F_SUBTITLE_STATUS, YT_F_AI_REVIEW_REPORT,
    YT_F_VIDEO_ID, YT_F_CREATED_AT,
    YT_HUMAN_SELECTED, YT_SUBTITLE_PENDING
)


def main():
    print("=" * 60)
    print("  YouTube 视频状态重置工具 (v5.3.0)")
    print("  仅重置今天 Selected + AI 报告为空的视频")
    print("=" * 60)

    # 计算今天 00:00:00 的毫秒时间戳 (UTC+8)
    tz_cn = timezone(timedelta(hours=8))
    now_cn = datetime.now(tz_cn)
    today_start_ms = int(datetime(now_cn.year, now_cn.month, now_cn.day, tzinfo=tz_cn).timestamp() * 1000)
    today_str = now_cn.strftime("%Y-%m-%d")
    print(f"\n[日期] 今日: {today_str}")
    print(f"  UTC+8 00:00:00 ms: {today_start_ms}")

    # 连接飞书
    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)

    print(f"\n[1/3] 读取 YouTube 表格全部记录...")
    records = sync.get_records(YT_TABLE_ID)
    print(f"  读取到 {len(records)} 条记录")

    # 筛选目标视频
    print(f"\n[2/3] 筛选目标视频 (Human Prescreen=Selected, 今天, AI 报告为空)...")
    target_records = []
    stats = {
        "total": 0,
        "not_selected": 0,
        "not_today": 0,
        "ai_report_exists": 0,
        "to_reset": 0,
    }

    for rec in records:
        fields = rec.get("fields", {})
        vid = str(fields.get(YT_F_VIDEO_ID, "")).strip()
        if not vid:
            continue
        stats["total"] += 1

        # 筛选: Human Prescreen = Selected
        hp = str(fields.get(YT_F_HUMAN_PRESCREEN, "") or "").strip()
        if hp != YT_HUMAN_SELECTED:
            stats["not_selected"] += 1
            continue

        # 筛选: Date Created = 今天
        created_at_raw = fields.get(YT_F_CREATED_AT, 0)
        try:
            created_at_ms = int(created_at_raw) if created_at_raw else 0
        except (TypeError, ValueError):
            created_at_ms = 0
        if created_at_ms < today_start_ms:
            stats["not_today"] += 1
            continue

        # 筛选: AI Review Report 为空 (尚未完成 AI 审核)
        ai_report = str(fields.get(YT_F_AI_REVIEW_REPORT, "") or "").strip()
        if ai_report:
            stats["ai_report_exists"] += 1
            continue

        target_records.append({
            "record_id": rec.get("record_id", ""),
            "video_id": vid,
            "human_prescreen": hp,
            "subtitle_status": str(fields.get(YT_F_SUBTITLE_STATUS, "") or "").strip(),
        })
        stats["to_reset"] += 1

    print(f"\n  筛选统计:")
    print(f"    YouTube 总记录:        {stats['total']}")
    print(f"    非 Selected:           {stats['not_selected']}")
    print(f"    非今天创建:            {stats['not_today']}")
    print(f"    AI 报告已存在:         {stats['ai_report_exists']}")
    print(f"    → 需要重置:            {stats['to_reset']}")

    if not target_records:
        print("\n[结果] 没有需要重置的视频，退出。")
        return

    # 显示预览
    print(f"\n  预览 (最多 10 条):")
    for v in target_records[:10]:
        print(f"    - {v['video_id']} (当前状态: {v['subtitle_status'] or '空'})")
    if len(target_records) > 10:
        print(f"    ... 还有 {len(target_records) - 10} 条")

    # 确认后执行重置
    print(f"\n[3/3] 重置 Subtitle Status → 'pending'...")
    success_count = 0
    for i, v in enumerate(target_records, 1):
        if sync.update_record(YT_TABLE_ID, v["record_id"], {YT_F_SUBTITLE_STATUS: YT_SUBTITLE_PENDING}):
            success_count += 1
            print(f"  ✓ [{i:3d}/{len(target_records)}] {v['video_id']}: pending", end="")
            if v["subtitle_status"]:
                print(f" (原: {v['subtitle_status']})", end="")
            print()
        else:
            print(f"  ✗ [{i:3d}/{len(target_records)}] {v['video_id']}: 更新失败")
        if (i + 1) % 20 == 0:
            print(f"    进度: {i + 1}/{len(target_records)}")

    print(f"\n{'=' * 60}")
    print(f"  重置完成!")
    print(f"  成功: {success_count} / {len(target_records)}")
    print(f"  失败: {len(target_records) - success_count}")
    print(f"{'=' * 60}")
    print(f"\n[下一步] 请填写 .env 中的 DEEPSEEK_API_KEY，然后重新运行工作流:")
    print(f"  python core/run_workflow.py")
    print()


if __name__ == "__main__":
    main()