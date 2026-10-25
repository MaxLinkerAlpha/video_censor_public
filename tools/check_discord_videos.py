#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_discord_videos.py - 诊断: Discord 自荐视频在飞书中的实际状态

读取 YT 表, 筛选今天创建的记录, 检查 12 条 Discord 标记视频的
Human Prescreen / Subtitle Status / AI Review / Notes 字段实际值。
只读不写。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "core")
sys.path.insert(0, CORE_DIR)

from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID  # noqa: E402
from feishu_fields import (  # noqa: E402
    YT_F_VIDEO_ID, YT_F_HUMAN_PRESCREEN, YT_F_NOTES,
    YT_F_SUBTITLE_STATUS, YT_F_AI_REVIEW, YT_F_CREATED_AT, YT_F_TITLE,
)

# 目标视频 ID 由命令行传入，避免把具体数据写死在代码里
# 用法: python check_discord_videos.py <video_id> [<video_id> ...]
DISCORD_VIDS = sys.argv[1:]


def main() -> int:
    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)

    print("[*] 读取 YT 表全部记录...")
    records = sync.get_records(YT_TABLE_ID)
    print(f"[OK] 共 {len(records)} 条记录")

    tz_cn = timezone(timedelta(hours=8))
    now_cn = datetime.now(tz_cn)
    today_start_ms = int(datetime(now_cn.year, now_cn.month, now_cn.day, tzinfo=tz_cn).timestamp() * 1000)

    # 统计今天创建记录的 Human Prescreen 分布
    today_stats = {}
    discord_found = {}

    for rec in records:
        f = rec.get("fields", {})
        vid = str(f.get(YT_F_VIDEO_ID, "") or "").strip()
        created_raw = f.get(YT_F_CREATED_AT, 0)
        try:
            created_ms = int(created_raw) if created_raw else 0
        except (TypeError, ValueError):
            created_ms = 0

        human = str(f.get(YT_F_HUMAN_PRESCREEN, "") or "").strip() or "(空)"

        if created_ms >= today_start_ms:
            today_stats[human] = today_stats.get(human, 0) + 1

        if vid in DISCORD_VIDS:
            discord_found[vid] = {
                "human": human,
                "subtitle": str(f.get(YT_F_SUBTITLE_STATUS, "") or "").strip() or "(空)",
                "ai": str(f.get(YT_F_AI_REVIEW, "") or "").strip() or "(空)",
                "notes": (str(f.get(YT_F_NOTES, "") or "").strip()[:80]) or "(空)",
                "created_today": created_ms >= today_start_ms,
            }

    print("\n[*] 今天创建记录的 Human Prescreen 分布:")
    for k, v in sorted(today_stats.items(), key=lambda x: -x[1]):
        print(f"    {k}: {v} 条")

    print(f"\n[*] Discord 12 条标记视频状态 (找到 {len(discord_found)}/12):")
    for vid in DISCORD_VIDS:
        s = discord_found.get(vid)
        if s is None:
            print(f"  [MISS] {vid} 不在 YT 表中!")
        else:
            print(f"  [{vid}] human={s['human']} | subtitle={s['subtitle']} | "
                  f"ai={s['ai']} | 今日创建={s['created_today']}")
            print(f"         notes={s['notes']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
