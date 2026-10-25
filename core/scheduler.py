#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scheduler.py - 统一调度器模块 (v4.3.0)

功能：
1. 统一调度器 - 协调采集→评分→审核流程
2. 状态追踪 - 记录每个视频的处理状态
3. 断点续传 - 支持中断后继续处理
4. 进度大屏 - 实时显示处理进度

使用方法：
    from scheduler import VideoScheduler, ProgressDashboard

    scheduler = VideoScheduler()
    dashboard = ProgressDashboard()

    # 添加视频到队列
    scheduler.add_video("https://www.youtube.com/watch?v=xxx")

    # 运行调度
    scheduler.run()
"""

import os
import json
import time
import datetime
import threading
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass, asdict
from enum import Enum


class VideoStatus(Enum):
    """视频处理状态枚举"""
    PENDING = "pending"
    FETCHING_SUBTITLE = "fetching_subtitle"
    SCORING = "scoring"
    CENSORING = "censoring"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class VideoTask:
    """视频任务数据类"""
    url: str
    platform: str = ""
    video_id: str = ""
    title: str = ""
    author_id: str = ""
    status: VideoStatus = VideoStatus.PENDING
    error: str = ""
    retry_count: int = 0
    max_retries: int = 3
    created_at: str = ""
    updated_at: str = ""
    subtitle_data: Dict = None
    score_result: Dict = None
    censor_result: str = ""

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if self.updated_at is None:
            self.updated_at = self.created_at
        if self.subtitle_data is None:
            self.subtitle_data = {}
        if self.score_result is None:
            self.score_result = {}

    def to_dict(self) -> Dict:
        """转换为字典"""
        data = asdict(self)
        data['status'] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict) -> 'VideoTask':
        """从字典创建"""
        if 'status' in data and isinstance(data['status'], str):
            data['status'] = VideoStatus(data['status'])
        return cls(**data)


class StateManager:
    """
    状态管理器

    功能：
    - 持久化存储视频处理状态
    - 支持断点续传
    - 线程安全
    """

    def __init__(self, state_file: str = None):
        """
        初始化状态管理器

        参数：
            state_file: 状态文件路径，默认为脚本同目录下的 scheduler_state.json
        """
        if state_file is None:
            state_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "data", "scheduler_state.json"
            )
        self.state_file = state_file
        self._lock = threading.Lock()
        self._tasks: Dict[str, VideoTask] = {}
        self._load_state()

    def _load_state(self) -> None:
        """从文件加载状态"""
        if not os.path.exists(self.state_file):
            return

        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for url, task_data in data.get('tasks', {}).items():
                    self._tasks[url] = VideoTask.from_dict(task_data)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[WARN] 加载状态文件失败: {e}")

    def _save_state(self) -> None:
        """保存状态到文件"""
        data = {
            'last_updated': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'tasks': {url: task.to_dict() for url, task in self._tasks.items()}
        }
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add_task(self, task: VideoTask) -> None:
        """添加任务"""
        with self._lock:
            self._tasks[task.url] = task
            self._save_state()

    def get_task(self, url: str) -> Optional[VideoTask]:
        """获取任务"""
        return self._tasks.get(url)

    def update_task(self, url: str, **kwargs) -> None:
        """更新任务"""
        with self._lock:
            if url in self._tasks:
                task = self._tasks[url]
                for key, value in kwargs.items():
                    if hasattr(task, key):
                        setattr(task, key, value)
                task.updated_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self._save_state()

    def get_pending_tasks(self) -> List[VideoTask]:
        """获取待处理任务"""
        return [t for t in self._tasks.values() if t.status == VideoStatus.PENDING]

    def get_failed_tasks(self, max_retries: int = 3) -> List[VideoTask]:
        """获取可重试的失败任务"""
        return [t for t in self._tasks.values()
                if t.status == VideoStatus.FAILED and t.retry_count < max_retries]

    def get_completed_tasks(self) -> List[VideoTask]:
        """获取已完成任务"""
        return [t for t in self._tasks.values() if t.status == VideoStatus.COMPLETED]

    def get_statistics(self) -> Dict[str, int]:
        """获取统计信息"""
        stats = {status.value: 0 for status in VideoStatus}
        for task in self._tasks.values():
            stats[task.status.value] += 1
        stats['total'] = len(self._tasks)
        return stats

    def clear_completed(self) -> int:
        """清除已完成的任务"""
        with self._lock:
            completed_urls = [url for url, task in self._tasks.items()
                            if task.status == VideoStatus.COMPLETED]
            for url in completed_urls:
                del self._tasks[url]
            self._save_state()
            return len(completed_urls)


class ProgressDashboard:
    """
    进度大屏

    功能：
    - 实时显示处理进度
    - 显示评分分布
    - 显示审核状态
    - 显示最近处理记录
    """

    def __init__(self, state_manager: StateManager = None):
        """
        初始化进度大屏

        参数：
            state_manager: 状态管理器实例
        """
        self.state_manager = state_manager or StateManager()
        self._recent_logs: List[Dict] = []
        self._lock = threading.Lock()
        self._start_time = time.time()

    def add_log(self, message: str, status: str = "info") -> None:
        """添加日志记录"""
        with self._lock:
            self._recent_logs.append({
                'time': datetime.datetime.now().strftime('%H:%M:%S'),
                'message': message[:50],
                'status': status
            })
            if len(self._recent_logs) > 10:
                self._recent_logs.pop(0)

    def render(self) -> str:
        """渲染进度大屏"""
        stats = self.state_manager.get_statistics()
        total = stats.get('total', 0)
        completed = stats.get('completed', 0)
        failed = stats.get('failed', 0)
        pending = stats.get('pending', 0)
        fetching = stats.get('fetching_subtitle', 0)
        scoring = stats.get('scoring', 0)
        censoring = stats.get('censoring', 0)

        elapsed = time.time() - self._start_time

        progress_bar = self._make_progress_bar(completed, total)

        lines = [
            "",
            "=" * 60,
            "         [VIDEO] 视频内容审核进度大屏 (v3.0.0)",
            "=" * 60,
            "",
            f"  总体进度: {progress_bar} {completed}/{total} ({completed*100//max(total,1)}%)",
            f"  运行时间: {int(elapsed//60)}分{int(elapsed%60)}秒",
            "",
            "  +---------------------+    +---------------------+",
            f"  |  待处理: {pending:<9} |    |  已完成: {completed:<9} |",
            f"  |  获取中: {fetching:<9} |    |  成功:   {completed:<9} |",
            f"  |  评分中: {scoring:<9} |    |  失败:   {failed:<9} |",
            f"  |  审核中: {censoring:<9} |    |                     |",
            "  +---------------------+    +---------------------+",
            "",
        ]

        with self._lock:
            if self._recent_logs:
                lines.append("  最近处理:")
                for log in self._recent_logs[-5:]:
                    status_icon = {
                        'success': '[OK]',
                        'error': '[ERROR]',
                        'warning': '[WARN]',
                        'info': '[INFO]'
                    }.get(log['status'], '[INFO]')
                    lines.append(f"    - {log['time']} {status_icon} {log['message']}")

        lines.append("")
        lines.append("=" * 60)

        return "\n".join(lines)

    def _make_progress_bar(self, completed: int, total: int, width: int = 20) -> str:
        """生成进度条"""
        if total == 0:
            return "[" + " " * width + "]"

        filled = int(width * completed / total)
        bar = "[" + "#" * filled + " " * (width - filled) + "]"
        return bar

    def print(self) -> None:
        """打印进度大屏"""
        print(self.render(), flush=True)


class VideoScheduler:
    """
    视频处理调度器

    功能：
    - 协调字幕获取、评分、审核流程
    - 支持断点续传
    - 支持重试机制
    """

    def __init__(
        self,
        state_file: str = None,
        max_retries: int = 3,
        on_progress: Callable[[str, Dict], None] = None
    ):
        """
        初始化调度器

        参数：
            state_file: 状态文件路径
            max_retries: 最大重试次数
            on_progress: 进度回调函数
        """
        self.state_manager = StateManager(state_file)
        self.dashboard = ProgressDashboard(self.state_manager)
        self.max_retries = max_retries
        self.on_progress = on_progress
        self._running = False

    def add_video(self, url: str, platform: str = "") -> None:
        """
        添加视频到队列

        参数：
            url: 视频URL
            platform: 平台标识（youtube/bilibili）
        """
        task = VideoTask(url=url, platform=platform)
        self.state_manager.add_task(task)
        self.dashboard.add_log(f"添加视频: {url[:40]}", "info")

    def add_videos(self, urls: List[str]) -> None:
        """批量添加视频"""
        for url in urls:
            self.add_video(url)

    def get_resume_tasks(self) -> List[VideoTask]:
        """
        获取需要恢复的任务

        返回：
            待处理任务 + 可重试的失败任务
        """
        pending = self.state_manager.get_pending_tasks()
        failed = self.state_manager.get_failed_tasks(self.max_retries)
        return pending + failed

    def update_status(self, url: str, status: VideoStatus, **kwargs) -> None:
        """更新任务状态"""
        self.state_manager.update_task(url, status=status, **kwargs)

        status_messages = {
            VideoStatus.FETCHING_SUBTITLE: "获取字幕中",
            VideoStatus.SCORING: "评分中",
            VideoStatus.CENSORING: "审核中",
            VideoStatus.COMPLETED: "处理完成",
            VideoStatus.FAILED: "处理失败",
        }

        message = status_messages.get(status, status.value)
        log_status = "success" if status == VideoStatus.COMPLETED else \
                    "error" if status == VideoStatus.FAILED else "info"
        self.dashboard.add_log(f"{message}: {url[:40]}", log_status)

        if self.on_progress:
            task = self.state_manager.get_task(url)
            if task:
                self.on_progress(url, task.to_dict())

    def run(self) -> Dict[str, Any]:
        """
        运行调度器

        返回：
            处理结果统计
        """
        self._running = True
        self.dashboard._start_time = time.time()

        self.dashboard.print()

        stats = self.state_manager.get_statistics()

        self.dashboard.print()

        return {
            'total': stats.get('total', 0),
            'completed': stats.get('completed', 0),
            'failed': stats.get('failed', 0),
            'pending': stats.get('pending', 0)
        }

    def stop(self) -> None:
        """停止调度器"""
        self._running = False


if __name__ == "__main__":
    print("=" * 60)
    print("调度器模块测试 (v3.0.0)")
    print("=" * 60)

    scheduler = VideoScheduler()

    test_urls = [
        "https://www.youtube.com/watch?v=test1",
        "https://www.bilibili.com/video/BV1test2",
    ]

    print("\n[TEST] 添加测试视频...")
    for url in test_urls:
        scheduler.add_video(url)

    print("\n[TEST] 进度大屏:")
    scheduler.dashboard.print()

    print("\n[TEST] 状态统计:")
    stats = scheduler.state_manager.get_statistics()
    for key, value in stats.items():
        print(f"  {key}: {value}")

    print("\n" + "=" * 60)
    print("[OK] 调度器模块测试完成")
