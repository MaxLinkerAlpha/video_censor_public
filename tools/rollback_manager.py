#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rollback_manager.py - 增强版回滚管理器 (v2.0)

功能：
1. 自动备份：每次数据获取前自动备份 Excel
2. 快速恢复：一键将备份替换为当前文件
3. 备份列表：查看所有可用备份
4. 清理旧备份：只保留最近 N 个（默认3个）

使用方法：
    # 自动备份（在 fetcher 中调用）
    from rollback_manager import auto_backup
    auto_backup()  # 备份 Bilibili + YouTube

    # 查看备份列表
    python rollback_manager.py --list

    # 恢复到指定备份
    python rollback_manager.py --restore <backup_file>

    # 恢复到最新备份
    python rollback_manager.py --restore-latest

    # 清理旧备份（保留最近3个）
    python rollback_manager.py --cleanup
"""

import os
import sys
import shutil
import json
from datetime import datetime
from pathlib import Path

# ==================== 路径配置 ====================
# tools 目录的上一级是 refactor 目录，core 在 refactor 下
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REFACTOR_DIR = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, os.path.join(REFACTOR_DIR, 'core'))

from config import BILIBILI_EXCEL_PATH, YOUTUBE_EXCEL_PATH, WORK_DIR

# 备份目录
BACKUP_DIR = os.path.join(WORK_DIR, ".backups")
# 保留备份数量
MAX_BACKUPS = 3


def ensure_backup_dir():
    """确保备份目录存在"""
    os.makedirs(BACKUP_DIR, exist_ok=True)


def get_backup_prefix(filename: str) -> str:
    """获取备份文件前缀"""
    name = os.path.splitext(os.path.basename(filename))[0]
    return name.replace(" ", "_")


def create_backup(file_path: str) -> str:
    """
    创建单个文件的备份
    
    参数:
        file_path: 要备份的文件路径
    
    返回:
        备份文件路径，失败返回空字符串
    """
    if not os.path.exists(file_path):
        print(f"  [WARN] 文件不存在，跳过备份: {file_path}")
        return ""
    
    ensure_backup_dir()
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = get_backup_prefix(file_path)
    backup_name = f"{prefix}_v{timestamp}_backup.xlsx"
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    
    try:
        shutil.copy2(file_path, backup_path)
        size_kb = os.path.getsize(backup_path) / 1024
        print(f"  [OK] 已备份: {os.path.basename(file_path)} -> {backup_name} ({size_kb:.1f} KB)")
        return backup_path
    except Exception as e:
        print(f"  [ERR] 备份失败: {e}")
        return ""


def auto_backup() -> dict:
    """
    自动备份所有 Excel 数据库（在 fetcher 运行前调用）
    
    返回:
        {"bilibili": "backup_path", "youtube": "backup_path"}
    """
    print("\n[📦 自动备份中...]")
    
    backups = {
        "bilibili": create_backup(BILIBILI_EXCEL_PATH),
        "youtube": create_backup(YOUTUBE_EXCEL_PATH),
    }
    
    # 清理旧备份
    cleanup_old_backups()
    
    # 保存备份元数据
    save_backup_metadata(backups)
    
    return backups


def list_backups() -> list:
    """
    列出所有可用的备份
    
    返回:
        备份信息列表 [{"name", "path", "size", "time"}, ...]
    """
    ensure_backup_dir()
    
    backups = []
    for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if f.endswith("_backup.xlsx"):
            path = os.path.join(BACKUP_DIR, f)
            stat = os.stat(path)
            size_mb = stat.st_size / (1024 * 1024)
            time_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            
            backups.append({
                "name": f,
                "path": path,
                "size": f"{size_mb:.2f} MB",
                "time": time_str
            })
    
    return backups


def restore_backup(backup_path: str, target_path: str) -> bool:
    """
    从备份恢复文件
    
    参数:
        backup_path: 备份文件路径
        target_path: 目标文件路径
    
    返回:
        是否成功
    """
    if not os.path.exists(backup_path):
        print(f"  [ERR] 备份文件不存在: {backup_path}")
        return False
    
    # 先备份当前版本（以防万一）
    current_backup = create_backup(target_path)
    
    try:
        shutil.copy2(backup_path, target_path)
        print(f"\n  [OK] 已恢复: {os.path.basename(target_path)}")
        print(f"       来源: {os.path.basename(backup_path)}")
        if current_backup:
            print(f"       旧版本已备份为: {os.path.basename(current_backup)}")
        return True
    except Exception as e:
        print(f"  [ERR] 恢复失败: {e}")
        return False


def restore_latest(bilibili: bool = True, youtube: bool = True) -> bool:
    """恢复到最新的备份"""
    backups = list_backups()
    
    if not backups:
        print("[ERR] 没有找到任何备份")
        return False
    
    success = True
    
    # 找到 Bilibili 和 YouTube 的最新备份
    bili_backups = [b for b in backups if "Bilibili" in b["name"] or "bili" in b["name"].lower()]
    yt_backups = [b for b in backups if "YouTube" in b["name"] or "yt" in b["name"].lower()]
    
    print(f"\n[*] 恢复到最新备份...")
    
    if bilibili and bili_backups:
        latest = bili_backups[0]
        print(f"\n  Bilibili 最新备份: {latest['name']} ({latest['time']})")
        if not restore_backup(latest["path"], BILIBILI_EXCEL_PATH):
            success = False
    elif bilibili:
        print("  [WARN] 未找到 Bilibili 备份")
    
    if youtube and yt_backups:
        latest = yt_backups[0]
        print(f"\n  YouTube 最新备份: {latest['name']} ({latest['time']})")
        if not restore_backup(latest["path"], YOUTUBE_EXCEL_PATH):
            success = False
    elif youtube:
        print("  [WARN] 未找到 YouTube 备份")
    
    return success


def cleanup_old_backups(keep_count: int = MAX_BACKUPS):
    """清理旧备份，只保留最近的 keep_count 个"""
    ensure_backup_dir()
    
    # 按 Bilibili/YouTube 分组
    groups = {"bilibili": [], "youtube": []}
    
    for f in os.listdir(BACKUP_DIR):
        if f.endswith("_backup.xlsx"):
            path = os.path.join(BACKUP_DIR, f)
            fl = f.lower()
            if "bili" in fl:
                groups["bilibili"].append(path)
            elif "youtube" in fl or "yt_" in fl:
                groups["youtube"].append(path)
    
    # 对每组按时间排序，删除旧的
    deleted_count = 0
    for group_name, files in groups.items():
        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        
        for old_file in files[keep_count:]:
            try:
                os.remove(old_file)
                deleted_count += 1
            except Exception as e:
                print(f"  [WARN] 删除旧备份失败: {e}")
    
    if deleted_count > 0:
        print(f"  [INFO] 已清理 {deleted_count} 个旧备份（每组保留最近 {keep_count} 个）")


def save_backup_metadata(backups: dict):
    """保存备份元数据"""
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "backups": backups
    }
    
    meta_path = os.path.join(BACKUP_DIR, ".metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)


# ==================== CLI 入口 ====================
def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="增强版回滚管理器 v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python rollback_manager.py --list              # 查看所有备份
  python rollback_manager.py --backup            # 手动创建备份
  python rollback_manager.py --restore-latest    # 恢复到最新备份
  python rollback_manager.py --restore <file>   # 恢复到指定备份
  python rollback_manager.py --cleanup           # 清理旧备份
        """
    )
    
    parser.add_argument("--list", action="store_true", help="列出所有备份")
    parser.add_argument("--backup", action="store_true", help="手动创建备份")
    parser.add_argument("--restore-latest", action="store_true", help="恢复到最新备份")
    parser.add_argument("--restore", metavar="FILE", help="恢复到指定备份文件")
    parser.add_argument("--cleanup", action="store_true", help="清理旧备份")
    parser.add_argument("--keep", type=int, default=MAX_BACKUPS, help="保留备份数量（默认3）")
    
    args = parser.parse_args()
    
    print("=" * 50)
    print("🔄 回滚管理器 v2.0")
    print("=" * 50)
    
    if args.list:
        backups = list_backups()
        
        if not backups:
            print("\n[INFO] 没有找到任何备份")
            return
        
        print(f"\n[*] 共找到 {len(backups)} 个备份:\n")
        print(f"{'序号':<4} {'文件名':<45} {'大小':<12} {'时间'}")
        print("-" * 80)
        
        for i, b in enumerate(backups[:20], 1):  # 只显示最近20个
            print(f"{i:<4} {b['name']:<45} {b['size']:<12} {b['time']}")
        
        if len(backups) > 20:
            print(f"\n  ... 还有 {len(backups) - 20} 个更早的备份")
    
    elif args.backup:
        auto_backup()
        print("\n[OK] 手动备份完成")
    
    elif args.restore_latest:
        if restore_latest():
            print("\n[OK] 恢复完成！请重新运行工作流。")
        else:
            print("\n[ERR] 恢复失败！")
            sys.exit(1)
    
    elif args.restore:
        backup_path = args.restore
        
        # 如果是相对路径，在备份目录中查找
        if not os.path.isabs(backup_path):
            backup_path = os.path.join(BACKUP_DIR, backup_path)
        
        # 根据文件名判断目标
        name_lower = os.path.basename(backup_path).lower()
        if "bili" in name_lower:
            restore_backup(backup_path, BILIBILI_EXCEL_PATH)
        elif "youtube" in name_lower or "yt_" in name_lower:
            restore_backup(backup_path, YOUTUBE_EXCEL_PATH)
        else:
            print("[ERR] 无法确定备份类型（文件名应包含 Bilibili 或 YouTube）")
            sys.exit(1)
    
    elif args.cleanup:
        cleanup_old_backups(args.keep)
        print("\n[OK] 清理完成")
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
