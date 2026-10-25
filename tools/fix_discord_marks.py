#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_discord_marks.py - 数据修复: Discord 自荐视频标记值 Selected → High Priority (v5.3.7)

背景:
  v5.3.7 修复前 discord_video_mark.py 写入的是 Selected (零加权"强行入选"),
  与"自荐优先"意图不符。本脚本把本周已标记的 Discord 自荐视频修正为
  High Priority (+10 加权), 并同步重算 Total Score 与 Rating。

安全规范 (遵循 feishu-write-safety):
  1. 写入前先重新获取字段结构, 校验类型: Human Prescreen=select(3),
     Total Score=number(2), Rating=select(3)
  2. select 字段传 option NAME (不传 option ID)
  3. number 字段传数字
  4. 目标记录按 Notes 来源标注 + 当前值=Selected + 今日创建 三重条件定位,
     并与已知 12 个 Video ID 交叉核对, 防止误改

用法:
    python tools/fix_discord_marks.py           # dry-run: 只打印计划, 不写入
    python tools/fix_discord_marks.py --pilot  # 试点: 只修 1 条并回读验证
    python tools/fix_discard_marks.py --full    # 全量: 修复全部 (自动含试点验证)
"""
import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "core")
sys.path.insert(0, CORE_DIR)

from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID  # noqa: E402
from feishu_fields import (  # noqa: E402
    YT_F_VIDEO_ID, YT_F_HUMAN_PRESCREEN, YT_F_NOTES, YT_F_CREATED_AT,
    YT_F_TOTAL_SCORE, YT_F_RATING,
    YT_HUMAN_SELECTED, YT_HUMAN_HIGH,
)

# 来源标注 (与 discord_video_mark.py 的 SOURCE_NOTE 保持一致)
DISCORD_NOTE = "Discord videos-n-streams"

# 交叉核对用的视频 ID, 由 --vids 传入 (留空则跳过核对)
KNOWN_VIDS: set = set()

PRIORITY_BONUS = 10.0  # High Priority 加权 (与 feishu_fields.PRIORITY_WEIGHT 一致)


def _parse_score(val) -> float:
    """解析当前 Total Score (可能为数字或字符串), 无效返回 0"""
    try:
        num = float(val)
        return num if num == num else 0.0  # NaN 检查
    except (TypeError, ValueError):
        return 0.0


def main() -> int:
    global KNOWN_VIDS
    parser = argparse.ArgumentParser(description="Discord 标记数据修复 v5.3.7")
    parser.add_argument("--pilot", action="store_true", help="试点模式: 只修 1 条并回读验证")
    parser.add_argument("--full", action="store_true", help="全量模式: 修复全部目标记录")
    parser.add_argument("--vids", default="", help="交叉核对的视频 ID, 逗号分隔 (留空则跳过核对)")
    args = parser.parse_args()
    KNOWN_VIDS = {v.strip() for v in args.vids.split(",") if v.strip()}

    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)

    # ---- 步骤 1: 写入前重新获取字段结构并校验类型 (安全规范) ----
    print("[*] 步骤 1: 重新获取 YT 表字段结构...")
    fields_type = sync.get_fields(YT_TABLE_ID)
    if not fields_type:
        print("[FAIL] 无法获取字段结构, 中止")
        return 1

    def _type_of(name: str) -> str:
        return str(fields_type.get(name, "?"))

    checks = [
        (YT_F_HUMAN_PRESCREEN, "3", "select"),
        (YT_F_TOTAL_SCORE, "2", "number"),
        (YT_F_RATING, "3", "select"),
    ]
    for fname, expect, label in checks:
        actual = _type_of(fname)
        status = "OK" if actual == expect else "FAIL"
        print(f"    [{status}] {fname}: type={actual} (期望 {expect}/{label})")
        if actual != expect:
            print(f"[FAIL] 字段类型不符, 中止 (避免类型误写)")
            return 1

    # ---- 步骤 2: 定位目标记录 (三重条件 + 交叉核对) ----
    print("[*] 步骤 2: 定位目标记录 (Notes含来源 + Human=Selected + 今日创建)...")
    records = sync.get_records(YT_TABLE_ID)
    print(f"    共读取 {len(records)} 条记录")

    tz_cn = timezone(timedelta(hours=8))
    now_cn = datetime.now(tz_cn)
    today_start_ms = int(datetime(now_cn.year, now_cn.month, now_cn.day, tzinfo=tz_cn).timestamp() * 1000)

    targets = []
    already_fixed = set()  # 试点/前次运行已修复的记录
    for rec in records:
        f = rec.get("fields", {})
        vid = str(f.get(YT_F_VIDEO_ID, "") or "").strip()
        if not vid:
            continue
        notes = str(f.get(YT_F_NOTES, "") or "")
        human = str(f.get(YT_F_HUMAN_PRESCREEN, "") or "").strip()
        if DISCORD_NOTE not in notes or human not in (YT_HUMAN_SELECTED, YT_HUMAN_HIGH):
            continue
        try:
            created_ms = int(f.get(YT_F_CREATED_AT, 0) or 0)
        except (TypeError, ValueError):
            created_ms = 0
        if created_ms < today_start_ms:
            continue
        if human == YT_HUMAN_HIGH:
            already_fixed.add(vid)
            continue
        targets.append({
            "record_id": rec.get("record_id", ""),
            "vid": vid,
            "old_score": _parse_score(f.get(YT_F_TOTAL_SCORE, 0)),
            "old_rating": str(f.get(YT_F_RATING, "") or "").strip(),
        })

    print(f"    定位到 {len(targets)} 条目标记录 (另有 {len(already_fixed)} 条已修复)")
    if not targets:
        print("[OK] 无需修复 (可能已修复过), 退出")
        return 0

    # 交叉核对: 提供了 --vids 时, 待修复 + 已修复 必须恰好覆盖该集合
    got_vids = {t["vid"] for t in targets} | already_fixed
    if KNOWN_VIDS and got_vids != KNOWN_VIDS:
        print(f"    [WARN] 定位结果与 --vids 给定期望集合不完全一致:")
        print(f"      多出: {sorted(got_vids - KNOWN_VIDS)}")
        print(f"      缺少: {sorted(KNOWN_VIDS - got_vids)}")
        print("    中止, 请人工检查")
        return 1
    print(f"    [OK] 与 --vids 给定 {len(KNOWN_VIDS)} 条 Video ID 完全一致")

    # ---- 步骤 3: 计算修复计划 ----
    print("\n[*] 步骤 3: 修复计划 (Human Prescreen → High Priority, 分数 +10):")
    for t in targets:
        if t["old_score"] > 0:
            t["new_score"] = round(t["old_score"] + PRIORITY_BONUS, 2)
            t["new_rating"] = "A"  # >= 10 必为 A
            print(f"    {t['vid']}: {t['old_score']} -> {t['new_score']}, "
                  f"Rating {t['old_rating'] or '(空)'} -> A")
        else:
            t["new_score"] = None
            t["new_rating"] = None
            print(f"    {t['vid']}: 无有效分数 (审核失败), 仅改标记")

    if not (args.pilot or args.full):
        print("\n[DRY-RUN] 未指定 --pilot/--full, 不写入。确认无误后加参数执行。")
        return 0

    # ---- 步骤 4: 执行写入 ----
    def _apply(t: dict) -> bool:
        payload = {YT_F_HUMAN_PRESCREEN: YT_HUMAN_HIGH}  # select 传 option NAME
        if t["new_score"] is not None:
            payload[YT_F_TOTAL_SCORE] = t["new_score"]   # number 传数字
            payload[YT_F_RATING] = t["new_rating"]       # select 传 option NAME
        return sync.update_record(YT_TABLE_ID, t["record_id"], payload)

    def _verify(t: dict) -> bool:
        """回读该记录, 核对标记与分数"""
        recs = sync.get_records(YT_TABLE_ID)
        for rec in recs:
            f = rec.get("fields", {})
            if str(f.get(YT_F_VIDEO_ID, "") or "").strip() == t["vid"]:
                human = str(f.get(YT_F_HUMAN_PRESCREEN, "") or "").strip()
                score_ok = True
                rating_ok = True
                if t["new_score"] is not None:
                    score_ok = abs(_parse_score(f.get(YT_F_TOTAL_SCORE, 0)) - t["new_score"]) < 0.01
                    rating_ok = str(f.get(YT_F_RATING, "") or "").strip() == t["new_rating"]
                return human == YT_HUMAN_HIGH and score_ok and rating_ok
        return False

    if args.pilot:
        t = targets[0]
        print(f"\n[*] PILOT: 先修复 {t['vid']} ...")
        if not _apply(t):
            print("[FAIL] 试点写入失败, 中止")
            return 1
        print("[*] 回读验证...")
        if _verify(t):
            print(f"[OK] 试点成功: {t['vid']} Human=High Priority, "
                  f"score={t['new_score']}, rating={t['new_rating']}")
            print("    确认无误后运行 --full 修复其余记录")
            return 0
        print("[FAIL] 试点回读验证失败, 中止")
        return 1

    # full: 第一条也按试点流程走 (写 + 验证), 通过后再批量
    print("\n[*] 步骤 4: 全量修复 (首条带回读验证)...")
    ok, fail = 0, 0
    for i, t in enumerate(targets):
        if not _apply(t):
            print(f"  [FAIL] {t['vid']} 写入失败")
            fail += 1
            continue
        if i == 0:
            if not _verify(t):
                print(f"  [FAIL] 首条 {t['vid']} 回读验证失败, 中止后续")
                return 1
            print(f"  [OK] 首条 {t['vid']} 写入并验证通过")
        else:
            print(f"  [OK] {t['vid']} 已写入")
        ok += 1

    # ---- 步骤 5: 终验 ----
    print("\n[*] 步骤 5: 全量回读终验...")
    final = sync.get_records(YT_TABLE_ID)
    fixed = 0
    for rec in final:
        f = rec.get("fields", {})
        vid = str(f.get(YT_F_VIDEO_ID, "") or "").strip()
        if not KNOWN_VIDS or vid in KNOWN_VIDS:
            human = str(f.get(YT_F_HUMAN_PRESCREEN, "") or "").strip()
            if human == YT_HUMAN_HIGH:
                fixed += 1
            else:
                print(f"  [WARN] {vid} Human 仍为 {human!r}")
    target_total = len(KNOWN_VIDS) or len(got_vids)
    print(f"\n[RESULT] 写入 成功 {ok} / 失败 {fail}; 终验 High Priority 共 {fixed}/{target_total}")
    return 0 if fail == 0 and fixed == target_total else 1


if __name__ == "__main__":
    sys.exit(main())
