#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
source_registry.py - 视频来源注册表 (SSOT, v5.4.0)

背景 (v5.4.0):
  Discord 自荐 / 中文精选视频不再前置写入 Human Prescreen=高优先级,
  而是以普通视频身份走完整流程 (机器初筛 -> ScriptDedup -> AI审核+评分),
  AI 审核完成后才翻转标记为高优先级。
  来源信息此前依赖 Notes 字段, 但 Notes 会被 ScriptDedup 的去重备注覆盖,
  因此引入独立的本地注册表作为来源唯一可信源 (Single Source of Truth)。

消费方:
  1. video_processor.calculate_video_score: 评分时给来源视频加权 (+10)
  2. workflow_orchestrator._flip_source_priority: AI 完成后翻转 Human Prescreen
  3. workflow_orchestrator._resurrect_source_videos: 最终入选 <8 时复活补跑 AI

存储: data/source_registry.json
  {
    "youtube": {"<video_id>": {"source": "discord", "marked_at": 1789...}},
    "bilibili": {"<bvid>":    {"source": "curated", "marked_at": 1789...}}
  }

线程安全: 模块级 threading.Lock 保护 读-改-写 (规则6: 并发写入必须加锁)
"""

import json
import os
import threading
import time
from typing import Dict, Optional, Set

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

REGISTRY_FILE = os.path.join(PROJECT_ROOT, "data", "source_registry.json")

# 来源类型常量
SOURCE_DISCORD = "discord"
SOURCE_CURATED = "curated"

_LOCK = threading.Lock()


def load_registry() -> Dict[str, Dict[str, Dict]]:
    """加载注册表; 文件缺失/损坏返回空结构 (不抛异常, 保证调用方可用)"""
    if not os.path.exists(REGISTRY_FILE):
        return {}
    try:
        with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] 来源注册表读取异常: {e}")
        return {}
    result: Dict[str, Dict[str, Dict]] = {}
    if isinstance(data, dict):
        for platform, ids in data.items():
            if isinstance(ids, dict):
                # 兼容旧格式: 值直接是字符串时视为 {"source": 字符串}
                result[str(platform).lower()] = {
                    str(vid): (v if isinstance(v, dict) else {"source": str(v), "marked_at": 0})
                    for vid, v in ids.items()
                }
    return result


def save_registry(registry: Dict[str, Dict[str, Dict]]) -> None:
    """保存注册表 (UTF-8; 目录不存在则创建)"""
    try:
        os.makedirs(os.path.dirname(REGISTRY_FILE), exist_ok=True)
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[ERROR] 来源注册表保存失败: {e}")


def register_source(platform: str, video_id: str, source: str) -> bool:
    """注册视频来源 (幂等: 已注册则保留首次记录)

    返回: True=本次新注册 / False=已存在或参数不完整
    """
    platform = (platform or "").strip().lower()
    video_id = (video_id or "").strip()
    source = (source or "").strip()
    if not platform or not video_id or not source:
        print(f"[WARN] register_source 参数不完整: "
              f"platform={platform!r}, video_id={video_id!r}, source={source!r}")
        return False

    with _LOCK:
        registry = load_registry()
        platform_map = registry.setdefault(platform, {})
        if video_id in platform_map:
            return False
        platform_map[video_id] = {"source": source, "marked_at": int(time.time())}
        save_registry(registry)
    return True


def get_source(platform: str, video_id: str) -> str:
    """查询视频来源类型; 未注册返回空串"""
    platform = (platform or "").strip().lower()
    video_id = (video_id or "").strip()
    if not platform or not video_id:
        return ""
    with _LOCK:
        registry = load_registry()
    info = registry.get(platform, {}).get(video_id)
    if isinstance(info, dict):
        return str(info.get("source", "") or "")
    return ""


def is_source_video(platform: str, video_id: str) -> bool:
    """该视频是否为来源推荐视频 (Discord自荐/中文精选等)"""
    return bool(get_source(platform, video_id))


def get_source_ids(platform: str, source: Optional[str] = None) -> Set[str]:
    """列出某平台 (可按来源类型过滤) 的全部已注册视频ID"""
    platform = (platform or "").strip().lower()
    if not platform:
        return set()
    with _LOCK:
        registry = load_registry()
    ids: Set[str] = set()
    for vid, info in registry.get(platform, {}).items():
        if source is None or (isinstance(info, dict) and info.get("source") == source):
            ids.add(vid)
    return ids
