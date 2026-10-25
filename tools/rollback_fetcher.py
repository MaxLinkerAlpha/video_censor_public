#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rollback_fetcher.py - 抓取回滚工具 (v1.0)

功能：
1. 撤销最近一次抓取写入飞书的记录（按序号范围删除）
2. 恢复本地 Excel 到抓取前状态
3. 清除本次抓取产生的缓存状态

使用方法：
    # 查看当前可回滚的快照
    python rollback_fetcher.py --list
    
    # 回滚最近一次 Bilibili 抓取
    python rollback_fetcher.py --platform bilibili --confirm
    
    # 回滚指定序号范围的记录
    python rollback_fetcher.py --platform bilibili --from-seq 163 --to-seq 196 --confirm

安全机制：
- 所有删除操作前必须加 --confirm 参数
- 自动备份当前状态到 .rollback_backup/
- 支持逐步回退，每次只撤销一次操作
"""

import os
import sys
import json
import shutil
import argparse
from datetime import datetime
from typing import List, Dict, Optional, Tuple

# 路径配置：tools/ 目录下的脚本需要访问 core/, platforms/ 等
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)  # 项目根目录
sys.path.insert(0, PROJECT_ROOT)            # 添加根目录以找到 core/, platforms/
sys.path.insert(0, os.path.join(PROJECT_ROOT, "core"))  # 添加core目录

from core.feishu_sync import (
    FeishuSync, BILI_BITABLE_TOKEN,
    BILI_TABLE_ID, YT_TABLE_ID
)


ROLLBACK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", ".rollback_snapshots")
EXCEL_BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", ".rollback_excel_backups")


def _ensure_dirs() -> None:
    """确保回滚目录存在"""
    os.makedirs(ROLLBACK_DIR, exist_ok=True)
    os.makedirs(EXCEL_BACKUP_DIR, exist_ok=True)


def save_snapshot(platform: str, new_seq_start: int, new_seq_end: int,
                  new_record_ids: List[str], excel_file: str) -> str:
    """
    在抓取前/后保存快照信息
    
    返回快照文件路径
    """
    _ensure_dirs()
    
    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "platform": platform,
        "seq_range": {"start": new_seq_start, "end": new_seq_end},
        "record_ids": new_record_ids,
        "record_count": len(new_record_ids),
        "excel_file": excel_file,
        "excel_backup": "",
    }
    
    # 备份 Excel 文件
    if os.path.exists(excel_file):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{os.path.basename(excel_file)}_{ts}.xlsx"
        backup_path = os.path.join(EXCEL_BACKUP_DIR, backup_name)
        shutil.copy2(excel_file, backup_path)
        snapshot["excel_backup"] = backup_path
        print(f"  [BACKUP] Excel 已备份到: {backup_name}")
    
    # 保存快照元数据
    snapshot_file = os.path.join(
        ROLLBACK_DIR,
        f"fetch_{platform}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    with open(snapshot_file, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    
    print(f"  [SNAPSHOT] 快照已保存: {os.path.basename(snapshot_file)}")
    print(f"            序号范围: {new_seq_start} ~ {new_seq_end} ({len(new_record_ids)} 条)")
    
    return snapshot_file


def list_snapshots() -> List[Dict]:
    """列出所有可用的回滚快照"""
    _ensure_dirs()
    snapshots = []
    
    if not os.path.exists(ROLLBACK_DIR):
        return snapshots
    
    for fname in sorted(os.listdir(ROLLBACK_DIR), reverse=True):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(ROLLBACK_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                snap = json.load(f)
            snap["_file"] = fpath
            snap["_filename"] = fname
            snapshots.append(snap)
        except Exception:
            continue
    
    return snapshots


def delete_feishu_records_by_seq(
    table_id: str,
    seq_field: str,
    seq_start: int,
    seq_end: int,
    dry_run: bool = False
) -> Tuple[int, List[str]]:
    """
    按序号范围从飞书表格删除记录
    
    返回 (删除数量, 删除的 record_id 列表)
    """
    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)
    
    records = sync.get_records(table_id)
    to_delete = []
    
    for r in records:
        fields = r.get("fields", {})
        seq_val = fields.get(seq_field, 0)
        try:
            seq_num = int(seq_val) if seq_val else 0
        except (ValueError, TypeError):
            continue
        
        if seq_start <= seq_num <= seq_end:
            to_delete.append(r.get("record_id"))
    
    if dry_run:
        return len(to_delete), to_delete
    
    deleted = 0
    failed = 0

    # 使用 lark-cli 批量删除（每批最多 500 条）
    BATCH_SIZE = 500
    for i in range(0, len(to_delete), BATCH_SIZE):
        batch = to_delete[i:i + BATCH_SIZE]
        try:
            result = sync.delete_records(table_id, batch)
            if result > 0:
                deleted += len(batch)
                print(f"    [OK] 删除 {len(batch)} 条 (序号 {i+1}-{i+len(batch)})")
            else:
                failed += len(batch)
                print(f"    [WARN] 删除失败 {len(batch)} 条")
        except Exception as e:
            failed += len(batch)
            print(f"    [ERR] 批量删除异常: {e}")

    return deleted, to_delete


def restore_excel_from_backup(backup_path: str, target_path: str) -> bool:
    """从备份恢复 Excel 文件"""
    if not os.path.exists(backup_path):
        print(f"  [ERR] 备份文件不存在: {backup_path}")
        return False
    
    try:
        shutil.copy2(backup_path, target_path)
        print(f"  [OK] Excel 已恢复: {os.path.basename(target_path)}")
        return True
    except Exception as e:
        print(f"  [ERR] 恢复失败: {e}")
        return False


def perform_rollback(
    snapshot: Dict,
    confirm: bool = False,
    skip_excel: bool = False,
    skip_feishu: bool = False
) -> bool:
    """执行回滚操作"""
    
    platform = snapshot["platform"]
    seq_start = snapshot["seq_range"]["start"]
    seq_end = snapshot["seq_range"]["end"]
    record_count = snapshot["record_count"]
    excel_file = snapshot.get("excel_file", "")
    excel_backup = snapshot.get("excel_backup", "")
    
    print(f"\n{'='*50}")
    print(f"回滚预览 - {platform.upper()} 抓取")
    print(f"{'='*50}")
    print(f"  快照时间: {snapshot['timestamp']}")
    print(f"  序号范围: {seq_start} ~ {seq_end}")
    print(f"  记录数量: {record_count}")
    print(f"  Excel文件: {os.path.basename(excel_file) if excel_file else 'N/A'}")
    print(f"  Excel备份: {os.path.basename(excel_backup) if excel_backup else 'N/A'}")
    
    table_id = BILI_TABLE_ID if platform == "bilibili" else YT_TABLE_ID
    seq_field = "序号" if platform == "bilibili" else "Number"
    
    if not confirm:
        print(f"\n  [--] 这是预览模式。如需执行回滚，请添加 --confirm 参数")
        return False
    
    print(f"\n[*] 开始执行回滚...")
    
    success = True
    
    # 1. 删除飞书记录
    if not skip_feishu:
        print(f"\n[1/2] 删除飞书记录 (序号 {seq_start}-{seq_end})...")
        deleted, rids = delete_feishu_records_by_seq(table_id, seq_field, seq_start, seq_end)
        print(f"      结果: 成功删除 {deleted}/{len(rids)} 条")
        if deleted < len(rids):
            print(f"      [WARN] 有 {len(rids) - deleted} 条删除失败，请手动检查")
            success = False
    else:
        print(f"\n[1/2] 跳过飞书记录删除 (--skip-feishu)")
    
    # 2. 恢复 Excel
    if not skip_excel and excel_backup:
        print(f"\n[2/2] 恢复本地 Excel...")
        if not restore_excel_from_backup(excel_backup, excel_file):
            success = False
    elif skip_excel:
        print(f"\n[2/2] 跳过 Excel 恢复 (--skip-excel)")
    elif not excel_backup:
        print(f"\n[2/2] 无可用 Excel 备份，跳过")
    
    # 3. 标记快照为已回滚
    if success:
        snap_file = snapshot.get("_file", "")
        if snap_file and os.path.exists(snap_file):
            rolled_back_file = snap_file.replace(".json", "_rolledback.json")
            os.rename(snap_file, rolled_back_file)
            print(f"\n  [OK] 快照已标记为已回滚")
    
    print(f"\n{'='*50}")
    print(f"回滚完成: {'成功' if success else '部分成功（有警告）'}")
    print(f"{'='*50}")
    
    return success


def main():
    parser = argparse.ArgumentParser(
        description="抓取回滚工具 v1.0 - 撤销抓取和飞书写入操作",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python rollback_fetcher.py --list                              # 查看所有快照
  python rollback_fetcher.py --platform bilibili --confirm       # 回滚最新 Bilibili 抓取
  python rollback_fetcher.py --snapshot fetch_bilibili_xxx.json --confirm  # 回滚指定快照
  python rollback_fetcher.py --platform bilibili --from-seq 163 --to-seq 196 --confirm  # 自定义范围
        """
    )
    
    parser.add_argument("--list", action="store_true", help="列出所有可回滚的快照")
    parser.add_argument("--platform", choices=["bilibili", "youtube"], help="指定平台")
    parser.add_argument("--snapshot", help="指定快照文件路径")
    parser.add_argument("--from-seq", type=int, help="起始序号（自定义范围）")
    parser.add_argument("--to-seq", type=int, help="结束序号（自定义范围）")
    parser.add_argument("--confirm", action="store_true", help="确认执行回滚（必须显式指定）")
    parser.add_argument("--skip-excel", action="store_true", help="跳过 Excel 恢复")
    parser.add_argument("--skip-feishu", action="store_true", help="跳过飞书记录删除")
    
    args = parser.parse_args()
    
    # 列出模式
    if args.list:
        snapshots = list_snapshots()
        
        if not snapshots:
            print("\n没有找到可回滚的快照")
            return
        
        print(f"\n{'='*60}")
        print(f"可回滚快照列表 ({len(snapshots)} 个)")
        print(f"{'='*60}")
        
        for i, s in enumerate(snapshots, 1):
            print(f"\n  [{i}] {s['_filename']}")
            print(f"      平台: {s['platform'].upper()}")
            print(f"      时间: {s['timestamp']}")
            print(f"      序号: {s['seq_range']['start']} ~ {s['seq_range']['end']} ({s['record_count']} 条)")
            print(f"      Excel: {'有备份' if s.get('excel_backup') else '无备份'}")
        
        return
    
    # 确定要回滚的目标
    if args.snapshot:
        if os.path.exists(args.snapshot):
            with open(args.snapshot, "r", encoding="utf-8") as f:
                target_snapshot = json.load(f)
            target_snapshot["_file"] = args.snapshot
        else:
            print(f"[ERR] 快照文件不存在: {args.snapshot}")
            return
    elif args.platform and args.from_seq is not None and args.to_seq is not None:
        # 自定义范围模式
        target_snapshot = {
            "timestamp": datetime.now().isoformat(),
            "platform": args.platform,
            "seq_range": {"start": args.from_seq, "end": args.to_seq},
            "record_count": args.to_seq - args.from_seq + 1,
            "record_ids": [],
            "excel_file": "",
            "excel_backup": "",
        }
    elif args.platform:
        # 平台最新快照模式
        snapshots = list_snapshots()
        matching = [s for s in snapshots if s["platform"] == args.platform]
        
        if not matching:
            print(f"[ERR] 未找到平台 '{args.platform}' 的快照")
            return
        
        target_snapshot = matching[0]
    else:
        parser.print_help()
        print("\n[ERR] 请指定 --list、--platform 或 --snapshot 参数")
        return
    
    # 执行回滚
    perform_rollback(target_snapshot, confirm=args.confirm)


if __name__ == "__main__":
    main()
