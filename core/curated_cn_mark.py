#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
curated_cn_mark.py - 中文精选表来源标记 (v5.4.0)

功能：
1. 读取中文精选表 (base=FEISHU_BITABLE_TOKEN, table=tbl_CURATED, 约135条)
   字段: 标题 / 链接 / B站分享链接 / UP / 入选(暂未|入选未配|已配|本轮不选|审核不过)
2. 从 链接 与 B站分享链接 提取 BV号 (BV[0-9A-Za-z]{10});
   b23.tv 短链用 requests allow_redirects 解析最终URL取BV号
   (bilibili 直连不走代理, 超时10s; 解析失败则按标题去B站表匹配)
3. 与 B站表 (BILI_TABLE_ID) BVID 匹配 (v5.4.0 来源注册表架构):
   - 命中: 仅追加 备注 来源标注 + 写入本地来源注册表 (source_registry.json)
     ⚠️ 不再前置写 人工初筛=高优先级 —— 精选视频必须以普通身份
     走完整流程 (机器初筛 -> ScriptDedup -> AI审核+评分), AI 完成后由
     workflow_orchestrator 统一翻转为 高优先级
4. 输出统计: 命中 / 未命中 / 短链解析失败

使用方法：
    python core/curated_cn_mark.py             # 正式运行 (写飞书)
    python core/curated_cn_mark.py --dry-run   # 只打印匹配情况, 不写飞书
"""

import os
import re
import sys
import argparse
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

try:
    import requests
except ImportError:
    print("[ERROR] requests 库未安装")
    sys.exit(1)

from feishu_fields import (
    BILI_F_BVID, BILI_F_NOTES, BILI_F_TITLE,
)
from source_registry import register_source, SOURCE_CURATED

# ================= 配置常量 =================

# 精选表 (与 B站/YouTube 表同一 base)
CURATED_TABLE_ID = "tbl_CURATED"

# 精选表字段名
CURATED_F_TITLE = "标题"
CURATED_F_LINK = "链接"
CURATED_F_SHARE_LINK = "B站分享链接"
CURATED_F_UP = "UP"
CURATED_F_SELECTED = "入选"

BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
B23_HOST = "b23.tv"
SHORT_LINK_TIMEOUT = 10  # 短链解析超时 (秒)
SOURCE_NOTE = "来源: 中文精选表"

_REQUEST_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _extract_text(val) -> str:
    """飞书字段值 -> 纯文本 (兼容 str / 富文本 list / 超链接 dict)"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        parts: List[str] = []
        for item in val:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "") or item.get("link", "") or ""))
            else:
                parts.append(str(item))
        return "".join(parts).strip()
    if isinstance(val, dict):
        return str(val.get("text", "") or val.get("link", "") or "").strip()
    return str(val).strip()


def extract_bv(text: str) -> str:
    """从文本提取 BV 号 (BV + 10位字母数字)"""
    match = BV_PATTERN.search(text or "")
    return match.group(0) if match else ""


def resolve_b23_short_link(url: str) -> str:
    """解析 b23.tv 短链 -> 最终URL (bilibili 直连, 不走代理); 失败返回空串"""
    target = (url or "").strip()
    if not target.startswith("http"):
        target = f"https://{target}"
    try:
        # proxies 传 None 值强制绕过环境代理, 保证 bilibili 直连
        res = requests.get(
            target,
            allow_redirects=True,
            timeout=SHORT_LINK_TIMEOUT,
            headers=_REQUEST_UA,
            proxies={"http": None, "https": None},
        )
        return str(res.url or "")
    except requests.exceptions.RequestException as e:
        print(f"  [WARN] 短链解析失败: {target[:50]} ({type(e).__name__}: {str(e)[:80]})")
        return ""


def get_curated_bv(title: str, link: str, share_link: str) -> Tuple[str, str]:
    """从精选表记录提取 BV 号

    返回: (bv, 解析方式) 解析方式: direct=直链提取 / short=短链解析 / fallback=标题兜底 / none
    """
    for text in (link, share_link):
        bv = extract_bv(text)
        if bv:
            return bv, "direct"

    # 无直链 BV: 尝试解析 b23.tv 短链
    for text in (link, share_link):
        if B23_HOST in (text or ""):
            final_url = resolve_b23_short_link(text)
            bv = extract_bv(final_url)
            if bv:
                return bv, "short"

    # 短链解析失败/无链接: 按标题去 B站表匹配 (调用方处理)
    if title:
        return title, "fallback"
    return "", "none"


# ================= 主流程 =================

def main() -> int:
    parser = argparse.ArgumentParser(description="中文精选表来源标记 (来源注册表架构)")
    parser.add_argument("--dry-run", action="store_true", help="只打印匹配情况, 不写飞书")
    args = parser.parse_args()

    print("=" * 60)
    print("中文精选表来源标记 (v5.4.0 来源注册表架构)")
    if args.dry_run:
        print("模式: DRY-RUN (不写飞书)")
    print("=" * 60)

    from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID
    from config import ENV_CONFIG

    bitable_token = os.getenv("FEISHU_BITABLE_TOKEN", "").strip() or ENV_CONFIG.get("FEISHU_BITABLE_TOKEN", "")
    if not bitable_token:
        print("[ERROR] 缺少 FEISHU_BITABLE_TOKEN 环境变量/.env 配置")
        return 1

    sync = FeishuSync()
    sync.set_bitable_token(bitable_token)

    # ---- 读取精选表 ----
    print(f"[*] 读取精选表 {CURATED_TABLE_ID}...")
    curated_records = sync.get_records(CURATED_TABLE_ID)
    if not curated_records:
        print("[ERROR] 精选表读取为空, 请检查表ID/权限")
        return 1
    print(f"[OK] 精选表共 {len(curated_records)} 条")

    # ---- 读取 B站表, 建立 BV号/标题 双索引 (含 备注当前值) ----
    print("[*] 读取 B站表, 建立 BV号/标题 索引...")
    bili_records = sync.get_records(BILI_TABLE_ID)
    bvid_index: Dict[str, Dict] = {}
    title_index: Dict[str, Dict] = {}
    for rec in bili_records:
        rec_fields = rec.get("fields", {})
        bvid = str(rec_fields.get(BILI_F_BVID, "") or "").strip()
        bili_title = str(rec_fields.get(BILI_F_TITLE, "") or "").strip()
        record_id = str(rec.get("record_id", "") or "")
        info = {
            "record_id": record_id,
            "bvid": bvid,
            "notes": str(rec_fields.get(BILI_F_NOTES, "") or "").strip(),
        }
        if bvid and record_id:
            bvid_index[bvid] = info
        if bili_title and record_id:
            title_index[bili_title] = info
    print(f"[OK] B站表共 {len(bili_records)} 条 (BV索引 {len(bvid_index)} | 标题索引 {len(title_index)})")

    # 规则: 写入前重新拉取表结构, 确认字段存在与类型
    print("[*] 拉取 B站表字段结构...")
    bili_fields = sync.get_fields(BILI_TABLE_ID)
    required_fields = (BILI_F_BVID, BILI_F_NOTES)
    missing = [f for f in required_fields if f not in bili_fields]
    if missing:
        print(f"[ERROR] B站表缺少字段 {missing}, 中止 (写入需确认字段类型)")
        return 1

    # ---- 逐条提取 BV 并匹配 ----
    stats = {"total": 0, "direct": 0, "short": 0, "short_failed": 0, "no_bv": 0,
             "registered": 0, "already": 0, "write_fail": 0, "miss": 0, "title_fallback_hit": 0}
    pending_writes: List[Tuple[str, str, str, str]] = []  # (record_id, bv, 标题, 合并后备注)

    for rec in curated_records:
        rec_fields = rec.get("fields", {})
        title = _extract_text(rec_fields.get(CURATED_F_TITLE))
        link = _extract_text(rec_fields.get(CURATED_F_LINK))
        share_link = _extract_text(rec_fields.get(CURATED_F_SHARE_LINK))
        up_name = _extract_text(rec_fields.get(CURATED_F_UP))
        selected_state = _extract_text(rec_fields.get(CURATED_F_SELECTED))

        stats["total"] += 1
        bv, how = get_curated_bv(title, link, share_link)

        if how == "direct":
            stats["direct"] += 1
        elif how == "short":
            stats["short"] += 1
        elif how == "fallback":
            # 短链解析失败/无链接 -> 按标题兜底匹配
            stats["short_failed"] += 1
            info = title_index.get(title)
            if info:
                stats["title_fallback_hit"] += 1
                bv = info["bvid"]  # v5.4.0: 标题兜底也用真实 BVID 注册来源
                pending_writes.append((info["record_id"], bv or f"(标题匹配){title[:20]}",
                                       title, info["notes"]))
                print(f"  [FALLBACK] 标题匹配命中: {title[:36]} (UP: {up_name}, 入选: {selected_state})")
            else:
                stats["miss"] += 1
                print(f"  [MISS] 无BV且标题未命中: {title[:36]} (链接: {link[:40] or share_link[:40]})")
            continue
        else:
            stats["no_bv"] += 1
            print(f"  [MISS] 无法提取BV号: {title[:36]} (链接: {link[:40] or share_link[:40]})")
            continue

        info = bvid_index.get(bv)
        if info is None:
            stats["miss"] += 1
            print(f"  [MISS] {bv} 不在B站表: {title[:36]} (UP: {up_name}, 入选: {selected_state})")
            continue

        # 命中: 注册来源 + 追加备注 (人工初筛 不动, 由全流程决定)
        pending_writes.append((info["record_id"], bv, title, info["notes"]))
        print(f"  [HIT] {bv} 待注册来源: {title[:36]} (UP: {up_name}, 入选: {selected_state})")

    # ---- 写入 (dry-run 跳过): 注册表 + Notes 来源标注, 不写 人工初筛 ----
    registered = 0
    if pending_writes:
        if args.dry_run:
            print(f"\n[DRY-RUN] 将为 {len(pending_writes)} 条注册来源 (备注追加标注, 不写入)")
        else:
            print(f"\n[*] 为 {len(pending_writes)} 条注册来源...")
            for record_id, bv, title, existing_notes in pending_writes:
                newly = register_source("bilibili", bv, SOURCE_CURATED)
                has_note = SOURCE_NOTE in (existing_notes or "")
                if has_note and not newly:
                    stats["already"] += 1
                    print(f"  [SKIP] {bv} 已注册且已有来源标注")
                    continue
                merged_notes = existing_notes if has_note else (
                    f"{existing_notes}\n{SOURCE_NOTE}" if existing_notes else SOURCE_NOTE
                )
                if sync.update_record(BILI_TABLE_ID, record_id, {
                    BILI_F_NOTES: merged_notes,
                }):
                    registered += 1
                    stats["registered"] += 1
                    print(f"  [OK] {bv} 来源已注册 (中文精选表)")
                else:
                    stats["write_fail"] += 1
                    print(f"  [FAIL] {bv} 备注写入失败")

    # ---- 统计 ----
    print("\n" + "=" * 60)
    print("[SUMMARY] 统计:")
    print(f"  精选表总数:       {stats['total']}")
    print(f"  直链提取BV:       {stats['direct']}")
    print(f"  短链解析成功:     {stats['short']}")
    print(f"  短链解析失败:     {stats['short_failed']} (标题兜底命中 {stats['title_fallback_hit']})")
    print(f"  无法提取BV/为空:  {stats['no_bv']}")
    print(f"  命中B站表:        {len(pending_writes)}")
    print(f"  待注册/已注册:    {len(pending_writes) if args.dry_run else stats['registered']} / {registered}")
    print(f"  已注册跳过:       {stats['already']}")
    print(f"  写入失败:         {stats['write_fail']}")
    print(f"  未命中:           {stats['miss']}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
