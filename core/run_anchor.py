#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_anchor.py - 工作流运行锚点 (v5.4.2)

问题背景:
    ScriptDedup 需要区分两件事:
      1. 候选池: 本次运行新增的、机器初筛通过且尚未人工标记的视频
      2. 已标记代表: 本次运行之前已被标记为正向的视频 (其 UP 在本批不再重复入选)
    旧实现用固定 "7 天滚动窗口" (cutoff = today - (days_filter-1)) 同时约束两者,
    导致: 若上次工作流的运行日恰好 == today-6, 上次的产出 (Human=Selected)
    就会落在窗口内, 被当作"本周已标记 UP", 使其 UP 的本批候选视频被整批排除。
    (2026-09-20 事故: YouTube 25 个频道共 102/145 条被误排除)

方案:
    用"上次运行的起止时刻"作为锚点, 彻底摆脱固定天数:
      - 候选池下界 = 上次运行 **开始** 时刻 (上次中断未处理的记录也能被本次接住)
      - 已标记下界 = 上次运行 **结束** 时刻 (从而排除上次运行自身的产出)

    为什么这样最稳: 用户可能提前或延后启动工作流, 间隔不固定;
    "上次运行"是唯一可靠的时间边界, 与运行节奏完全解耦。

存储: data/run_anchor.json
    {"last_run_start_ms": ..., "last_run_end_ms": ..., "..._iso": "..."}

并发安全: 模块级 threading.Lock + 原子替换写 (规则6: 并发写入必须加锁)
"""

import json
import os
import threading
import time
from typing import Dict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

ANCHOR_FILE = os.path.join(PROJECT_ROOT, "data", "run_anchor.json")

_LOCK = threading.Lock()


def _empty() -> Dict[str, int]:
    """无锚点时的空结构 (0 表示"无下界")"""
    return {"start": 0, "end": 0}


def get_last_run() -> Dict[str, int]:
    """读取上次运行的起止时刻 (毫秒时间戳)

    返回 {"start": int, "end": int}; 文件缺失/损坏时返回 {"start": 0, "end": 0},
    调用方据此回退到固定天数窗口。
    """
    if not os.path.exists(ANCHOR_FILE):
        return _empty()
    with _LOCK:
        try:
            with open(ANCHOR_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[WARN] 运行锚点读取失败, 将回退到固定天数窗口: {e}")
            return _empty()
    if not isinstance(data, dict):
        return _empty()

    def _as_int(key: str) -> int:
        val = data.get(key, 0)
        return int(val) if isinstance(val, (int, float)) and val > 0 else 0

    return {"start": _as_int("last_run_start_ms"), "end": _as_int("last_run_end_ms")}


def set_last_run(start_ms: int, end_ms: int) -> None:
    """写入本次运行的起止时刻 (原子替换, 避免半写状态)"""
    if start_ms <= 0 or end_ms <= 0:
        print(f"[WARN] 运行锚点参数非法, 已跳过写入: start={start_ms}, end={end_ms}")
        return
    payload = {
        "last_run_start_ms": int(start_ms),
        "last_run_end_ms": int(end_ms),
        "last_run_start_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_ms / 1000)),
        "last_run_end_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_ms / 1000)),
    }
    tmp_path = ANCHOR_FILE + ".tmp"
    with _LOCK:
        try:
            os.makedirs(os.path.dirname(ANCHOR_FILE), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, ANCHOR_FILE)  # 原子替换
        except OSError as e:
            print(f"[ERROR] 运行锚点写入失败: {e}")


def describe() -> str:
    """人类可读的锚点描述 (用于日志)"""
    anchor = get_last_run()
    if not anchor["start"] and not anchor["end"]:
        return "无锚点 (将回退到固定天数窗口)"
    fmt = lambda ms: time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else "-"
    return f"上次运行 {fmt(anchor['start'])} ~ {fmt(anchor['end'])}"