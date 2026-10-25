#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回溯脚本：利用已有 BV 号，将所有 B站视频的"链接"字段填补齐全
格式: https://www.bilibili.com/video/BV{x}

使用方法:
    python tools/backfill_bilibili_links.py

安全机制:
    - 只更新"链接"字段为空的记录（不覆盖已有数据）
    - 只更新拥有有效BV号的记录
    - 每 50 条输出一次进度
"""
import os
import sys

# 将项目根目录加入 sys.path，确保能导入 core 模块
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.join(_project_root, "core"))

from core.feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID
from core.feishu_fields import BILI_F_BVID, BILI_F_LINK


def main():
    print("=" * 60)
    print("   B站视频 【链接】 字段回溯填充工具")
    print("=" * 60)

    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)

    # Step 1: 获取所有记录
    print("\n[1] 正在读取 B站飞书表格所有记录...")
    records = sync.get_records(BILI_TABLE_ID)
    print(f"  [OK] 共读取 {len(records)} 条记录")

    if not records:
        print("  [WARN] 没有找到任何记录，脚本退出。")
        return

    # Step 2: 筛选需要填充的记录
    to_update = []
    skip_empty_bvid = 0
    skip_already_filled = 0

    for rec in records:
        fields = rec.get("fields", {})
        bvid = fields.get(BILI_F_BVID, "").strip()
        existing_link = fields.get(BILI_F_LINK, "")

        # 没有 BV 号 → 跳过
        if not bvid:
            skip_empty_bvid += 1
            continue

        # 已有链接且格式正确 → 跳过
        expected_link = f"https://www.bilibili.com/video/{bvid}"
        if existing_link and str(existing_link).strip() == expected_link:
            skip_already_filled += 1
            continue

        to_update.append({
            "record_id": rec["record_id"],
            "fields": {BILI_F_LINK: expected_link},
        })

    print(f"\n[2] 分析结果:")
    print(f"    总记录数:         {len(records)}")
    print(f"    缺少BV号（跳过）: {skip_empty_bvid}")
    print(f"    链接已正确（跳过）: {skip_already_filled}")
    print(f"    需要填充:         {len(to_update)}")

    if not to_update:
        print("\n  [OK] 没有需要填充的记录，所有链接已齐全！")
        return

    # Step 3: 确认后执行
    print(f"\n[3] 即将填充 {len(to_update)} 条记录的 【链接】 字段...")
    
    success = sync.batch_update_records(BILI_TABLE_ID, to_update)
    failed = len(to_update) - success

    print(f"\n[4] 填充完成:")
    print(f"    成功: {success}")
    print(f"    失败: {failed}")

    if failed > 0:
        print(f"  [WARN] 有 {failed} 条记录填充失败，可重新运行本脚本重试。")

    print("\n" + "=" * 60)
    print("   回溯填充任务结束。")
    print("=" * 60)


if __name__ == "__main__":
    main()
