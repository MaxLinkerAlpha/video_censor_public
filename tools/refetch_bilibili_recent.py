#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refetch_bilibili_recent.py - 重新抓取 B站近期视频并同步到飞书 (v5.3.1)

背景:
  Fetcher 以本地 Excel 为去重基准, 导致 7/27 后的视频虽在本地 Excel 中,
  但从未写入飞书 (上次运行显示"与本地 Excel 完全重复, 无新增").
  本脚本以【飞书】为去重基准, 强制重新抓取 2026-07-27 至今的所有相关视频并推送.

流程:
  1. 重新抓取 B站 (Playwright, 复用 fetcher 的 scrape_bilibili)
  2. 过滤 发布日期 >= 2026-07-27
  3. 读取飞书现有 BV 号集合 (去重基准 = 飞书)
  4. 推送飞书中缺失的视频
"""

import os
import sys
import datetime as dt

# 添加 core 目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

import pandas as pd

from bilibili_youtube_once_for_all_fetcher import (
    scrape_bilibili,
    push_to_bitable,
    BILI_BITABLE_TOKEN,
    BILI_TABLE_ID,
    BILI_KEYWORD,
)
from feishu_sync import FeishuSync

SINCE_DATE = "2026-07-27"  # 起始日期 (含)


def main() -> int:
    print("=" * 60)
    print("  B站近期视频重抓 & 飞书同步工具 (v5.3.1)")
    print(f"  起始日期: {SINCE_DATE} ~ 今天")
    print("=" * 60)

    # ---------- 1. 重新抓取 ----------
    print(f"\n[1/4] 重新抓取 B站 (关键词: {BILI_KEYWORD})...")
    df = scrape_bilibili(BILI_KEYWORD)
    if df.empty:
        print("  [FAIL] 抓取结果为空 (可能风控/页面无内容)，尝试回退本地 Excel 数据")
        excel_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'Bilibili_local_db.xlsx'
        )
        if os.path.exists(excel_path):
            df = pd.read_excel(excel_path)
            print(f"  [回退] 已加载本地 Excel: {len(df)} 条")
        else:
            print("  [FAIL] 本地 Excel 也不存在，退出")
            return 1

    print(f"  抓取到 {len(df)} 条原始记录")

    # ---------- 2. 过滤日期 ----------
    print(f"\n[2/4] 过滤发布日期 >= {SINCE_DATE}...")
    if '发布日期' not in df.columns:
        print("  [FAIL] 数据中无 '发布日期' 列")
        return 1
    df['发布日期_dt'] = pd.to_datetime(df['发布日期'], errors='coerce')
    recent = df[df['发布日期_dt'] >= SINCE_DATE].copy()
    print(f"  过滤后: {len(recent)} 条 (7/27 至今)")

    if recent.empty:
        print("  [INFO] 无符合条件的视频，退出")
        return 0

    # ---------- 3. 以飞书为去重基准 ----------
    print(f"\n[3/4] 读取飞书现有 BV 号 (去重基准 = 飞书)...")
    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)
    records = sync.get_records(BILI_TABLE_ID)
    feishu_bvids = set()
    for r in records:
        bv = str(r.get("fields", {}).get("BV号", "") or "").strip()
        if bv:
            feishu_bvids.add(bv)
    print(f"  飞书现有记录: {len(records)} 条, 有效 BV 号: {len(feishu_bvids)} 个")

    # 去重: 过滤掉飞书已存在的 BV 号
    recent["BV号_clean"] = recent["BV号"].astype(str).str.strip()
    missing = recent[~recent["BV号_clean"].isin(feishu_bvids)].copy()
    print(f"  飞书缺失 (需推送): {len(missing)} 条")
    if len(missing) == 0:
        print("  [INFO] 所有 7/27 后视频都已在飞书，无需推送")
        return 0

    # 预览
    print(f"\n  预览 (最多 15 条):")
    for _, row in missing.head(15).iterrows():
        d = row.get("发布日期", "")
        print(f"    - {row.get('BV号', '')} | {d} | {str(row.get('标题', ''))[:40]}")
    if len(missing) > 15:
        print(f"    ... 还有 {len(missing) - 15} 条")

    # ---------- 4. 推送 ----------
    print(f"\n[4/4] 推送 {len(missing)} 条到飞书...")
    # 删除辅助列, 避免推送到飞书
    for col in ('发布日期_dt', 'BV号_clean'):
        if col in missing.columns:
            missing = missing.drop(columns=[col])
    push_to_bitable(BILI_BITABLE_TOKEN, BILI_TABLE_ID, missing)

    print("\n" + "=" * 60)
    print("  推送完成!")
    print("=" * 60)
    print("\n[下一步] 运行 B站工作流处理新视频:")
    print("  python core/prescreen.py --platform bilibili")
    print("  python core/workflow_orchestrator.py --stage human-prescreen --platform bilibili --auto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
