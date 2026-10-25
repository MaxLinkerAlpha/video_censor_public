#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_creator_dup.py - 诊断: 本周 Selected 记录中是否存在同创作者(UP/Channel)重复

检查今天创建、Human Prescreen=Selected 的 56 条记录:
- 按 Channel 分组, 找出同一 Channel 出现多条的
- 标注哪些是 Discord 自荐 (Notes 含来源标注)
只读不写。
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "core")
sys.path.insert(0, CORE_DIR)

from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID  # noqa: E402
from feishu_fields import (  # noqa: E402
    YT_F_VIDEO_ID, YT_F_HUMAN_PRESCREEN, YT_F_NOTES, YT_F_CREATED_AT,
    YT_F_TITLE, YT_F_CHANNEL, YT_F_TOTAL_SCORE,
)

DISCORD_NOTE = "Discord videos-n-streams"


def main() -> int:
    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)

    print("[*] 读取 YT 表全部记录...")
    records = sync.get_records(YT_TABLE_ID)

    tz_cn = timezone(timedelta(hours=8))
    now_cn = datetime.now(tz_cn)
    today_start_ms = int(datetime(now_cn.year, now_cn.month, now_cn.day, tzinfo=tz_cn).timestamp() * 1000)

    selected_today = []
    for rec in records:
        f = rec.get("fields", {})
        vid = str(f.get(YT_F_VIDEO_ID, "") or "").strip()
        human = str(f.get(YT_F_HUMAN_PRESCREEN, "") or "").strip()
        created_raw = f.get(YT_F_CREATED_AT, 0)
        try:
            created_ms = int(created_raw) if created_raw else 0
        except (TypeError, ValueError):
            created_ms = 0

        if created_ms >= today_start_ms and human == "Selected":
            notes = str(f.get(YT_F_NOTES, "") or "")
            selected_today.append({
                "vid": vid,
                "title": str(f.get(YT_F_TITLE, "") or "")[:40],
                "channel": str(f.get(YT_F_CHANNEL, "") or "").strip() or "(无Channel)",
                "discord": DISCORD_NOTE in notes,
                "score": f.get(YT_F_TOTAL_SCORE, ""),
            })

    print(f"[OK] 今天 Selected 共 {len(selected_today)} 条 "
          f"(其中 Discord 自荐 {sum(1 for s in selected_today if s['discord'])} 条)")

    by_channel = defaultdict(list)
    for s in selected_today:
        by_channel[s["channel"]].append(s)

    dups = {ch: items for ch, items in by_channel.items() if len(items) > 1}
    print(f"\n[*] 同一 Channel 多条入选的: {len(dups)} 个 Channel")
    for ch, items in sorted(dups.items(), key=lambda x: -len(x[1])):
        print(f"\n  [Channel] {ch} ({len(items)} 条):")
        for s in items:
            tag = "[Discord自荐]" if s["discord"] else "[脚本优选]"
            print(f"    {tag} {s['vid']} score={s['score']} | {s['title']}")

    if not dups:
        print("    (无同 Channel 重复)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
