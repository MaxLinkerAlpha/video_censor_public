#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smart_retry.py - 智能重试决策引擎 (v4.4.0, v5.3.4 优化)

v4.4.0 新增模块。
v5.3.4 变更:
  - IP_BLOCKED 首次重试等待 60s -> 0s（探测已含 3 次内部重试，无需再等）
  - decide() 的 attempt_count 语义明确为"单视频已尝试次数"（由调用方跨轮累计）

设计理念（替代"立刻重试"）:
1. 错误分类前置 —— 每种错误走不同分支，而不是"所有错误=重试"
2. 被动 → 主动 —— 连续 IP 阻止时主动暂停，提示用户切换代理
3. 健康度记忆 —— 滑动窗口记录最近 5 分钟成功率
4. 预算上限 —— 单次运行最多消耗 X 分钟，超出放弃本次剩余任务
5. 两阶段流程 —— 先跳过失败 → 报告 → 飞书 → 重试失败 → 追加报告 → 飞书

错误分类:
  - ip_blocked        真实 IP 被阻止（IpBlocked 异常 / 持续 429）
  - rate_limit        频率限制（429 TooManyRequests 单次）
  - no_subtitle       视频无字幕（无需重试）
  - region_locked     区域限制（无法跨区域）
  - video_unavailable 视频不可用（私有/已删除，无需重试）
  - transient_network 临时网络错误（可重试）
  - feishu_throttle   飞书 API 限流
  - unknown           未知错误（保守重试）

决策策略表:
  | 错误类型          | 重试次数 | 等待策略            | 额外动作           |
  | ip_blocked_soft   | 1        | [0]                | 提示换代理 (v5.3.4: 首等降为0) |
  | ip_blocked_hard   | 0        | 无                 | 整批冷却+人工介入  |
  | rate_limit        | 2        | [30, 60]           | 降低并发           |
  | transient_network | 3        | [2, 5, 10]         | 同 IP 重试         |
  | no_subtitle       | 0        | 无                 | 标记跳过           |
  | region_locked     | 0        | 无                 | 标记跳过           |
  | video_unavailable | 0        | 无                 | 标记跳过           |
  | feishu_throttle   | 3        | [5, 15, 45]        | 退避+并发降级      |
  | unknown           | 1        | [10]               | 保守重试一次       |

使用方法:
    from smart_retry import SmartRetryEngine, ErrorCategory, RetryDecision

    engine = SmartRetryEngine()
    decision = engine.decide(ErrorCategory.IP_BLOCKED, context={...})
    if decision.should_retry:
        time.sleep(decision.wait_seconds)
        # 再次执行
    else:
        # 跳过 / 标记 / 提示
"""

import time
import json
import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Dict, List, Any, Deque


# ==================== 错误分类枚举 ====================
class ErrorCategory:
    """错误分类常量（使用字符串而非 Enum，方便 JSON 序列化）"""
    IP_BLOCKED = "ip_blocked"
    RATE_LIMIT = "rate_limit"
    NO_SUBTITLE = "no_subtitle"
    REGION_LOCKED = "region_locked"
    VIDEO_UNAVAILABLE = "video_unavailable"
    TRANSIENT_NETWORK = "transient_network"
    FEISHU_THROTTLE = "feishu_throttle"
    UNKNOWN = "unknown"

    # 视频级问题集合（绝对不能重试）
    VIDEO_LEVEL_NO_RETRY = frozenset({NO_SUBTITLE, REGION_LOCKED, VIDEO_UNAVAILABLE})


# ==================== 决策结果 ====================
@dataclass
class RetryDecision:
    """重试决策结果"""
    should_retry: bool                       # 是否重试
    wait_seconds: float = 0.0                # 等待秒数
    max_attempts: int = 0                    # 本次允许的最大尝试次数
    next_action: str = "skip"                # 后续动作: retry / skip / pause_batch / manual_intervention
    reason: str = ""                         # 决策原因（人类可读）
    suggested_user_action: str = ""          # 建议用户操作

    def to_dict(self) -> dict:
        return asdict(self)


# ==================== 策略表 ====================
STRATEGY_TABLE: Dict[str, Dict[str, Any]] = {
    # IP 软封：等待后仍可重试
    ErrorCategory.IP_BLOCKED: {
        "max_attempts": 1,
        # v5.3.4: 首次重试等待从 60s 降为 0s —— 探测本身已带 3 次内部重试，
        # 首次等待 60s 只会在批量失败时成倍拖慢流程；改为立即重试一次，
        # 仍失败则由上层标记 pending 等待用户换代理后整批重跑
        "wait_sequence": [0.0],
        "next_action": "retry_same",
        "user_action": "IP被封禁，或视频就没有字幕：请更换代理节点后重跑失败视频",
    },
    # 频率限制：等待后重试
    ErrorCategory.RATE_LIMIT: {
        "max_attempts": 2,
        "wait_sequence": [30.0, 60.0],
        "next_action": "retry_same",
        "user_action": "触发 YouTube 限流，自动延长等待时间",
    },
    # 临时网络错误：短等待重试
    ErrorCategory.TRANSIENT_NETWORK: {
        "max_attempts": 3,
        "wait_sequence": [2.0, 5.0, 10.0],
        "next_action": "retry_same",
        "user_action": "网络抖动，自动重试",
    },
    # 飞书限流：指数退避
    ErrorCategory.FEISHU_THROTTLE: {
        "max_attempts": 3,
        "wait_sequence": [5.0, 15.0, 45.0],
        "next_action": "retry_same",
        "user_action": "飞书 API 限流，自动退避",
    },
    # 视频无字幕：直接跳过
    ErrorCategory.NO_SUBTITLE: {
        "max_attempts": 0,
        "wait_sequence": [],
        "next_action": "skip",
        "user_action": "",
    },
    # 区域限制：直接跳过
    ErrorCategory.REGION_LOCKED: {
        "max_attempts": 0,
        "wait_sequence": [],
        "next_action": "skip",
        "user_action": "区域限制，跳过此视频",
    },
    # 视频不可用：直接跳过
    ErrorCategory.VIDEO_UNAVAILABLE: {
        "max_attempts": 0,
        "wait_sequence": [],
        "next_action": "skip",
        "user_action": "视频已删除或私有，跳过",
    },
    # 未知错误：保守重试一次
    ErrorCategory.UNKNOWN: {
        "max_attempts": 1,
        "wait_sequence": [10.0],
        "next_action": "retry_same",
        "user_action": "未知错误，谨慎重试",
    },
}


# ==================== youtube-transcript-api 异常分类 ====================
def classify_youtube_transcript_error(err: Exception) -> str:
    """
    精确分类 youtube-transcript-api 抛出的异常

    v4.4.0 核心修复：
    - 不再用 'blocked' in err_str.lower() 这种子串匹配
    - 优先按异常类型（type）判定
    - 字符串匹配仅作为类型缺失时的退化，且只做精确匹配

    三级判定:
    1. 异常类型名（最准确）—— 现代 youtube-transcript-api 0.6+
    2. 字符串精确匹配（IP 阻止 / RequestBlocked / TooManyRequests 等)
    3. 子串匹配（仅用于"无字幕/视频不可用/网络"等无歧义短语)
    """
    err_type_name = type(err).__name__
    err_str = str(err).strip()

    # ===== 第一级：异常类型名（最准确）=====
    if err_type_name == "IpBlocked":
        return ErrorCategory.IP_BLOCKED
    if err_type_name == "TooManyRequests":
        return ErrorCategory.RATE_LIMIT
    if err_type_name in ("NoTranscriptFound", "TranscriptsDisabled", "CouldNotRetrieveTranscript"):
        return ErrorCategory.NO_SUBTITLE
    if err_type_name == "VideoUnavailable":
        return ErrorCategory.VIDEO_UNAVAILABLE
    if err_type_name == "RequestBlocked":
        # 关键：RequestBlocked ≠ IP 阻止
        return ErrorCategory.REGION_LOCKED

    # ===== 第二级：字符串精确匹配（用于旧版/包装过的异常）=====
    # 关键原则：宁可 unknown，绝不误判 IP 阻止
    if err_str == "IpBlocked" or err_str.startswith("IpBlocked(") or err_str == "IpBlocked.":
        return ErrorCategory.IP_BLOCKED
    if err_str == "TooManyRequests" or err_str == "429" or err_str == "Too Many Requests":
        return ErrorCategory.RATE_LIMIT
    # RequestBlocked 的字符串格式多样（"RequestBlocked" / "RequestBlocked - sub videos..."）
    if err_str == "RequestBlocked" or err_str.startswith("RequestBlocked -") or err_str.startswith("RequestBlocked("):
        return ErrorCategory.REGION_LOCKED
    if err_str == "VideoUnavailable" or err_str.startswith("VideoUnavailable("):
        return ErrorCategory.VIDEO_UNAVAILABLE
    if err_str in ("NoTranscriptFound", "TranscriptsDisabled", "CouldNotRetrieveTranscript"):
        return ErrorCategory.NO_SUBTITLE

    # ===== 第三级：短语匹配（仅用于无歧义短语）=====
    # 重要：这里用整词匹配避免子串误判
    lower = err_str.lower()
    # 用单词边界方式匹配（避免 "subtitles are blocked" 中的 "blocked" 误判为 IP 阻止）
    import re
    # "no transcript" / "no captions" / "subtitles disabled"
    if re.search(r"\b(no transcript|no transcripts|no captions|subtitles? (are )?disabled|captions? disabled)\b", lower):
        return ErrorCategory.NO_SUBTITLE
    # 视频不可用
    if re.search(r"\b(video (is )?unavailable|private video|video (has been )?removed|this video does not exist)\b", lower):
        return ErrorCategory.VIDEO_UNAVAILABLE
    # 网络错误
    if re.search(r"\b(timeout|timed out|connection reset|connection aborted|network (is )?unreachable|connection refused|connection closed)\b", lower):
        return ErrorCategory.TRANSIENT_NETWORK
    # 频率限制（仅在无歧义时）
    if re.search(r"\b(429|too many requests|rate limit(ed)?)\b", lower):
        return ErrorCategory.RATE_LIMIT
    # 区域/视频级"被阻止"（不是 IP 阻止）
    # 旧版错误消息常见格式: "The video has been blocked in your country"
    #                   / "Video blocked in your region"
    # 关键: 这里要排除 IP 阻止（IpBlocked 由上面的精确匹配处理了）
    if re.search(r"\b(blocked|unavailable|not available) in (your |this )?(country|region|territory)\b", lower):
        return ErrorCategory.REGION_LOCKED
    if re.search(r"\bvideo (has been )?blocked\b", lower):
        return ErrorCategory.REGION_LOCKED

    # 注意：绝不单独匹配 "blocked" / "block" / "blocking" 等子串
    # 这是 v4.4.0 的核心修复——子串匹配会误判 RequestBlocked 为 IP 阻止

    return ErrorCategory.UNKNOWN


def classify_requests_error(err: Exception) -> str:
    """分类 requests 库抛出的异常（用于飞书 API 等）"""
    err_type_name = type(err).__name__
    err_str = str(err).lower()

    if "timeout" in err_str or "timed out" in err_str:
        return ErrorCategory.TRANSIENT_NETWORK
    if "connection" in err_str:
        return ErrorCategory.TRANSIENT_NETWORK
    if "429" in err_str or "too many requests" in err_str:
        return ErrorCategory.FEISHU_THROTTLE
    if "5" in err_str[:1] and "server error" in err_str:
        return ErrorCategory.TRANSIENT_NETWORK
    return ErrorCategory.UNKNOWN


# ==================== IP 健康度跟踪 ====================
@dataclass
class IPHealthStatus:
    """IP 健康度状态（滑动窗口 5 分钟）"""
    window_seconds: float = 300.0
    requests: Deque = field(default_factory=deque)  # (timestamp, success: bool)

    def record(self, success: bool):
        now = time.time()
        self.requests.append((now, success))
        self._evict_old(now)

    def _evict_old(self, now: float):
        cutoff = now - self.window_seconds
        while self.requests and self.requests[0][0] < cutoff:
            self.requests.popleft()

    @property
    def total(self) -> int:
        return len(self.requests)

    @property
    def success_rate(self) -> float:
        if not self.requests:
            return 1.0
        return sum(1 for _, ok in self.requests if ok) / len(self.requests)

    @property
    def consecutive_failures(self) -> int:
        cnt = 0
        for _, ok in reversed(self.requests):
            if not ok:
                cnt += 1
            else:
                break
        return cnt

    def is_ip_health_bad(self, threshold: float = 0.3, min_samples: int = 5) -> bool:
        """成功率低于 threshold 视为 IP 不健康"""
        if self.total < min_samples:
            return False
        return self.success_rate < threshold

    def is_ip_health_critical(self, threshold: float = 0.1, min_samples: int = 5) -> bool:
        """成功率极低，触发"建议人工介入"标记"""
        if self.total < min_samples:
            return False
        return self.success_rate < threshold


# ==================== 智能重试决策器 ====================
class SmartRetryEngine:
    """
    智能重试决策引擎

    替代旧的"立刻重试"逻辑。

    核心功能:
    1. decide() —— 根据错误类型和上下文决策（重试/跳过/暂停/人工）
    2. record_result() —— 记录每次结果，维护 IP 健康度
    3. should_pause_batch() —— 询问是否该整批暂停
    4. get_status_report() —— 输出健康度报告
    """

    def __init__(
        self,
        budget_seconds: float = 1800.0,    # 单次运行最大时间预算（30分钟）
        ip_unhealthy_threshold: float = 0.3,
        ip_critical_threshold: float = 0.1,
    ):
        """
        初始化

        参数:
            budget_seconds: 单次运行时间预算（秒），超出后强制停止
            ip_unhealthy_threshold: 触发"整批暂停"的失败率阈值
            ip_critical_threshold: 触发"建议人工介入"的失败率阈值
        """
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.budget_seconds = budget_seconds

        # IP 健康度（所有平台共用一个 IP 池）
        self.ip_health = IPHealthStatus()

        # 按错误类型的失败计数
        self.failure_counts: Dict[str, int] = {
            ErrorCategory.IP_BLOCKED: 0,
            ErrorCategory.RATE_LIMIT: 0,
            ErrorCategory.NO_SUBTITLE: 0,
            ErrorCategory.REGION_LOCKED: 0,
            ErrorCategory.VIDEO_UNAVAILABLE: 0,
            ErrorCategory.TRANSIENT_NETWORK: 0,
            ErrorCategory.FEISHU_THROTTLE: 0,
            ErrorCategory.UNKNOWN: 0,
        }
        # 按错误类型的成功计数
        self.success_counts: Dict[str, int] = {
            "success": 0,
        }

        # 整批暂停状态
        self._batch_paused = False

    # -------- 决策 --------
    def decide(self, error_category: str, context: Optional[Dict[str, Any]] = None) -> RetryDecision:
        """
        根据错误分类 + 上下文，决策下一步动作

        参数:
            error_category: 错误分类（ErrorCategory.* 之一）
            context: 可选上下文，含 attempt_count / consecutive_failures / elapsed_seconds
                    v5.3.4: attempt_count 语义 = 该视频已尝试次数（跨轮累计），
                    而非"第几轮重试"（轮次号会随批量逐轮增长导致误判）

        返回:
            RetryDecision
        """
        context = context or {}
        attempt = context.get("attempt_count", 1)

        # 1. 检查时间预算
        if self.is_budget_exceeded():
            return RetryDecision(
                should_retry=False,
                wait_seconds=0,
                max_attempts=0,
                next_action="pause_batch",
                reason="时间预算已超出，强制暂停本批",
                suggested_user_action=f"已运行 {self.get_elapsed_seconds():.0f} 秒，超过预算 {self.budget_seconds:.0f} 秒"
            )

        # 2. 检查 IP 健康度（仅对 IP 阻止相关错误额外考虑）
        strategy = STRATEGY_TABLE.get(error_category, STRATEGY_TABLE[ErrorCategory.UNKNOWN])
        if error_category == ErrorCategory.IP_BLOCKED and self.ip_health.is_ip_health_critical():
            # IP 状况极差 → 直接建议人工介入
            return RetryDecision(
                should_retry=False,
                wait_seconds=0,
                max_attempts=0,
                next_action="manual_intervention",
                reason="IP 状况极差（成功率 < 10%），建议人工介入",
                suggested_user_action=(
                    f"近 5 分钟成功率仅 {self.ip_health.success_rate:.1%}，\n"
                    "  强烈建议手动切换代理节点后再继续，\n"
                    "  或等待 10-30 分钟后重试整批任务"
                )
            )

        # 3. 视频级问题：直接跳过
        if error_category in ErrorCategory.VIDEO_LEVEL_NO_RETRY:
            return RetryDecision(
                should_retry=False,
                wait_seconds=0,
                max_attempts=0,
                next_action="skip",
                reason=f"视频级问题（{error_category}），无法通过重试解决",
                suggested_user_action=strategy.get("user_action", "")
            )

        # 4. 其它错误：按策略表判定
        max_attempts = strategy["max_attempts"]
        if attempt > max_attempts:
            return RetryDecision(
                should_retry=False,
                wait_seconds=0,
                max_attempts=max_attempts,
                next_action="skip",
                reason=f"已达最大重试次数（{max_attempts}）",
                suggested_user_action=strategy.get("user_action", "")
            )

        # 5. 计算等待时间
        wait_seq = strategy["wait_sequence"]
        if not wait_seq:
            return RetryDecision(
                should_retry=False,
                wait_seconds=0,
                max_attempts=max_attempts,
                next_action="skip",
                reason="策略表无等待序列",
                suggested_user_action=strategy.get("user_action", "")
            )

        wait_index = min(attempt - 1, len(wait_seq) - 1)
        wait_seconds = wait_seq[wait_index]

        return RetryDecision(
            should_retry=True,
            wait_seconds=wait_seconds,
            max_attempts=max_attempts,
            next_action=strategy.get("next_action", "retry_same"),
            reason=f"{error_category} 第 {attempt} 次重试，等待 {wait_seconds:.0f} 秒",
            suggested_user_action=strategy.get("user_action", "")
        )

    # -------- 记录结果 --------
    def record_result(self, error_category: str, success: bool):
        """
        记录一次请求的结果

        参数:
            error_category: 错误分类
            success: True=成功，False=失败
        """
        with self._lock:
            if success:
                self.success_counts["success"] = self.success_counts.get("success", 0) + 1
            else:
                self.failure_counts[error_category] = self.failure_counts.get(error_category, 0) + 1
            self.ip_health.record(success)

    def record_ip_health(self, success: bool):
        """仅记录 IP 健康度，不影响失败计数（用于被动观测）"""
        with self._lock:
            self.ip_health.record(success)

    # -------- 整批暂停判定 --------
    def should_pause_batch(self) -> bool:
        """
        是否应该整批暂停？

        触发条件:
        - 连续 IP 阻止 >= 3 次 且 IP 健康度差
        - 或预算已超出
        """
        if self.is_budget_exceeded():
            return True
        if self.ip_health.is_ip_health_bad():
            return True
        return False

    def is_budget_exceeded(self) -> bool:
        """是否已超出时间预算"""
        return self.get_elapsed_seconds() > self.budget_seconds

    def get_elapsed_seconds(self) -> float:
        """已运行秒数"""
        return time.time() - self.start_time

    def get_remaining_seconds(self) -> float:
        """剩余预算秒数"""
        return max(0, self.budget_seconds - self.get_elapsed_seconds())

    # -------- 报告 --------
    def get_status_report(self) -> str:
        """输出当前状态报告（人类可读）"""
        elapsed = self.get_elapsed_seconds()
        success = self.success_counts.get("success", 0)
        total_failures = sum(self.failure_counts.values())
        total = success + total_failures
        success_rate = (success / total * 100) if total > 0 else 0

        lines = [
            "=" * 50,
            "[SmartRetryEngine] 状态报告",
            "=" * 50,
            f"  运行时间: {elapsed:.0f}秒 / 预算 {self.budget_seconds:.0f}秒"
            f"   剩余: {self.get_remaining_seconds():.0f}秒",
            f"  总请求: {total} | 成功: {success} | 失败: {total_failures}",
            f"  总成功率: {success_rate:.1f}%",
            f"  IP 健康度: {self.ip_health.success_rate:.1%} "
            f"(近 5 分钟 {self.ip_health.total} 个样本, 连续失败 {self.ip_health.consecutive_failures} 次)",
            "",
            "  按错误分类统计:",
        ]
        for cat, cnt in sorted(self.failure_counts.items(), key=lambda x: -x[1]):
            if cnt > 0:
                lines.append(f"    - {cat:<22} : {cnt} 次")
        lines.append("=" * 50)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """导出为可序列化的字典"""
        return {
            "elapsed_seconds": self.get_elapsed_seconds(),
            "budget_seconds": self.budget_seconds,
            "ip_health_success_rate": self.ip_health.success_rate,
            "ip_health_total_samples": self.ip_health.total,
            "ip_health_consecutive_failures": self.ip_health.consecutive_failures,
            "success_count": self.success_counts.get("success", 0),
            "failure_counts": dict(self.failure_counts),
            "is_paused": self.should_pause_batch(),
        }


# ==================== 两阶段流程辅助 ====================
@dataclass
class RetryTask:
    """重试任务单元（用于两阶段流程）"""
    video_id: str
    platform: str
    url: str = ""
    error_category: str = ""
    error_message: str = ""
    first_attempt_time: float = 0.0
    first_attempt_success: bool = False
    final_success: bool = False
    final_error: str = ""
    retry_attempts: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


class TwoPhaseRetryCoordinator:
    """
    两阶段重试协调器

    阶段 1: 第一轮处理（失败即跳过，不立即重试）
    阶段 2: 第二轮仅对失败任务统一重试（应用 SmartRetryEngine 决策）

    用法:
        coord = TwoPhaseRetryCoordinator()
        # 阶段 1
        for video in videos:
            result = fetch(video)
            coord.record(video, result)
        # 写报告 → 飞书
        coord.write_phase_report_to_feishu()
        # 阶段 2
        coord.retry_failed()
        # 追加报告 → 飞书
        coord.append_phase_report_to_feishu()
    """

    def __init__(self, engine: Optional[SmartRetryEngine] = None):
        self.engine = engine or SmartRetryEngine()
        self.tasks: Dict[str, RetryTask] = {}  # video_id -> RetryTask
        self._lock = threading.Lock()

    def record(
        self,
        video_id: str,
        platform: str,
        success: bool,
        error_category: str = "",
        error_message: str = "",
        url: str = "",
    ):
        """记录一次处理结果"""
        with self._lock:
            if video_id not in self.tasks:
                self.tasks[video_id] = RetryTask(
                    video_id=video_id,
                    platform=platform,
                    url=url,
                    error_category=error_category,
                    error_message=error_message,
                    first_attempt_time=time.time(),
                    first_attempt_success=success,
                )
            else:
                task = self.tasks[video_id]
                task.final_success = success
                task.final_error = error_message
                task.retry_attempts += 1
            self.engine.record_result(error_category or "unknown", success)

    def get_failed_tasks(self) -> List[RetryTask]:
        """获取所有首次失败的任务（待重试）"""
        with self._lock:
            return [
                t for t in self.tasks.values()
                if not t.first_attempt_success
            ]

    def get_success_after_retry(self) -> List[RetryTask]:
        """获取重试成功的任务"""
        with self._lock:
            return [
                t for t in self.tasks.values()
                if t.first_attempt_success is False and t.final_success is True
            ]

    def get_still_failed(self) -> List[RetryTask]:
        """获取重试后仍然失败的任务"""
        with self._lock:
            return [
                t for t in self.tasks.values()
                if not t.first_attempt_success and not t.final_success
            ]

    # -------- 报告生成 --------
    def generate_phase_report(self, phase: int = 1) -> str:
        """
        生成阶段报告

        参数:
            phase: 1 = 第一轮报告，2 = 第二轮（重试）报告

        返回:
            多行字符串
        """
        with self._lock:
            total = len(self.tasks)
            if phase == 1:
                success_tasks = [t for t in self.tasks.values() if t.first_attempt_success]
                failed_tasks = [t for t in self.tasks.values() if not t.first_attempt_success]
                title = "第一轮处理报告"
            else:
                # v5.3.4: 内联统计（不能在持锁时再调用加锁的 getter ——
                # threading.Lock 非可重入, 会死锁）
                success_tasks = [t for t in self.tasks.values() if t.first_attempt_success is False and t.final_success is True]
                failed_tasks = [t for t in self.tasks.values() if not t.first_attempt_success and not t.final_success]
                title = "第二轮重试报告"

            lines = [
                "=" * 60,
                f"  {title}",
                f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "=" * 60,
                f"  任务总数: {total}",
                f"  本阶段成功: {len(success_tasks)}",
                f"  本阶段失败: {len(failed_tasks)}",
                f"  整体成功率: {len(success_tasks) / total * 100:.1f}%" if total else "  整体成功率: N/A",
                "",
                self.engine.get_status_report(),
                "",
            ]

            if failed_tasks:
                lines.append("-" * 60)
                lines.append(f"  失败明细 ({len(failed_tasks)} 个):")
                lines.append("-" * 60)
                # 按错误分类分组
                by_category: Dict[str, List[RetryTask]] = {}
                for t in failed_tasks:
                    by_category.setdefault(t.error_category or "unknown", []).append(t)
                for cat, tasks in sorted(by_category.items(), key=lambda x: -len(x[1])):
                    lines.append(f"\n  [{cat}] - {len(tasks)} 个:")
                    for t in tasks[:20]:  # 最多列 20 个
                        err_msg = t.error_message or t.final_error or "未知"
                        lines.append(f"    - {t.video_id} | {err_msg[:60]}")
                    if len(tasks) > 20:
                        lines.append(f"    ... 还有 {len(tasks) - 20} 个")
            else:
                lines.append("")
                lines.append("  [OK] 全部任务成功！")

            lines.append("=" * 60)
            return "\n".join(lines)

    def generate_combined_report(self) -> str:
        """
        生成合并报告（适用于"重试后追加到原报告"的场景）
        """
        with self._lock:
            total = len(self.tasks)
            first_success = [t for t in self.tasks.values() if t.first_attempt_success]
            # v5.3.4: 内联统计（不能在持锁时再调用加锁的 getter —— 死锁）
            retry_success = [t for t in self.tasks.values() if t.first_attempt_success is False and t.final_success is True]
            still_failed = [t for t in self.tasks.values() if not t.first_attempt_success and not t.final_success]

            lines = [
                "=" * 60,
                f"  合并处理报告（两阶段）",
                f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                "=" * 60,
                f"  任务总数: {total}",
                f"  第一轮直接成功: {len(first_success)} ({len(first_success) / total * 100:.1f}%)" if total else "  第一轮直接成功: 0",
                f"  第二轮重试成功: {len(retry_success)} ({len(retry_success) / total * 100:.1f}%)" if total else "  第二轮重试成功: 0",
                f"  最终仍失败:   {len(still_failed)} ({len(still_failed) / total * 100:.1f}%)" if total else "  最终仍失败: 0",
                f"  整体成功率: {(len(first_success) + len(retry_success)) / total * 100:.1f}%" if total else "  整体成功率: N/A",
                "",
                self.engine.get_status_report(),
                "",
            ]
            if still_failed:
                lines.append("-" * 60)
                lines.append(f"  最终失败明细 ({len(still_failed)} 个):")
                lines.append("-" * 60)
                by_category: Dict[str, List[RetryTask]] = {}
                for t in still_failed:
                    by_category.setdefault(t.error_category or "unknown", []).append(t)
                for cat, tasks in sorted(by_category.items(), key=lambda x: -len(x[1])):
                    lines.append(f"\n  [{cat}] - {len(tasks)} 个:")
                    for t in tasks:
                        err_msg = t.final_error or t.error_message or "未知"
                        lines.append(f"    - {t.video_id} | {err_msg[:60]}")
            lines.append("=" * 60)
            return "\n".join(lines)


# ==================== 飞书报告写入辅助 ====================
def write_report_to_feishu_field(
    feishu_sync,
    table_id: str,
    record_id: str,
    field_name: str,
    report_text: str,
    mode: str = "append",
) -> bool:
    """
    将报告文本写入飞书多维表格的指定字段

    参数:
        feishu_sync: FeishuSync 实例
        table_id: 表格 ID
        record_id: 记录 ID
        field_name: 目标字段名（应为多行文本/textarea 类型）
        report_text: 报告内容
        mode: "overwrite" | "append"

    返回:
        是否成功
    """
    try:
        existing = feishu_sync.get_record(table_id, record_id)
        existing_text = ""
        if existing:
            existing_text = (existing.get("fields", {}).get(field_name, "") or "")

        if mode == "append" and existing_text:
            new_text = f"{existing_text}\n\n{'='*40}\n{report_text}"
        else:
            new_text = report_text

        # 飞书多行文本字段单条上限 5000 字符，超出截断
        if len(new_text) > 5000:
            new_text = new_text[-5000:]  # 保留最新内容

        updates = {field_name: new_text}
        return feishu_sync.update_record(table_id, record_id, updates)
    except Exception as e:
        print(f"  [WARN] 写入飞书报告失败: {e}")
        return False


# ==================== 模块自检 ====================
if __name__ == "__main__":
    # 自检测试
    print("=== smart_retry.py 自检 ===\n")

    # 1. 异常分类测试
    # v4.4.0: 使用真实的 youtube-transcript-api 异常类名（type.__name__）
    class IpBlocked(Exception):
        pass
    class RequestBlocked(Exception):
        pass
    class NoTranscriptFound(Exception):
        pass
    class TooManyRequests(Exception):
        pass
    class VideoUnavailable(Exception):
        pass
    class Timeout(Exception):
        pass

    tests = [
        (IpBlocked("IpBlocked"), ErrorCategory.IP_BLOCKED),
        (RequestBlocked("RequestBlocked"), ErrorCategory.REGION_LOCKED),
        (NoTranscriptFound("NoTranscriptFound"), ErrorCategory.NO_SUBTITLE),
        (TooManyRequests("TooManyRequests"), ErrorCategory.RATE_LIMIT),
        (VideoUnavailable("VideoUnavailable"), ErrorCategory.VIDEO_UNAVAILABLE),
        (Timeout("Connection timeout"), ErrorCategory.TRANSIENT_NETWORK),
        (Exception("Some unknown error"), ErrorCategory.UNKNOWN),
        # v4.4.0 关键 bug 场景: 旧代码会误判为 ip_blocked
        (Exception("RequestBlocked - sub videos for this language are blocked"),
         ErrorCategory.REGION_LOCKED),
        # 字符串级别的边界场景
        (Exception("subtitles are disabled for this video"),
         ErrorCategory.NO_SUBTITLE),
        (Exception("video is unavailable (private)"),
         ErrorCategory.VIDEO_UNAVAILABLE),
        (Exception("HTTP 429 Too Many Requests"),
         ErrorCategory.RATE_LIMIT),
        # 必须是 unknown，绝不能误判
        (Exception("The video has been blocked in your country"),
         ErrorCategory.REGION_LOCKED),  # 整词匹配 block
    ]

    print("1. 异常分类测试:")
    all_pass = True
    for err, expected in tests:
        result = classify_youtube_transcript_error(err)
        ok = result == expected
        if not ok:
            all_pass = False
        mark = "✓" if ok else "✗"
        print(f"  {mark} {type(err).__name__}({str(err)[:40]!r}) -> {result} (期望: {expected})")
    if all_pass:
        print("  >>> 全部通过 ✓")
    else:
        print("  >>> 有测试失败，需要修复分类逻辑")

    # 2. 决策器测试
    print("\n2. 决策器测试:")
    engine = SmartRetryEngine(budget_seconds=600)
    for cat in [ErrorCategory.IP_BLOCKED, ErrorCategory.NO_SUBTITLE,
                ErrorCategory.TRANSIENT_NETWORK, ErrorCategory.RATE_LIMIT]:
        decision = engine.decide(cat, context={"attempt_count": 1})
        print(f"  [{cat}] should_retry={decision.should_retry}, "
              f"wait={decision.wait_seconds:.0f}s, action={decision.next_action}")

    # 3. IP 健康度测试
    print("\n3. IP 健康度测试:")
    for _ in range(3):
        engine.record_result(ErrorCategory.IP_BLOCKED, success=False)
    for _ in range(2):
        engine.record_result(ErrorCategory.TRANSIENT_NETWORK, success=True)
    print(f"  IP 成功率: {engine.ip_health.success_rate:.1%}")
    print(f"  连续失败: {engine.ip_health.consecutive_failures}")
    print(f"  应暂停整批: {engine.should_pause_batch()}")

    # 4. 两阶段协调器测试
    print("\n4. 两阶段协调器测试:")
    coord = TwoPhaseRetryCoordinator(engine=engine)
    coord.record("vid1", "youtube", success=True)
    coord.record("vid2", "youtube", success=False, error_category=ErrorCategory.IP_BLOCKED,
                 error_message="IP 阻止")
    coord.record("vid3", "youtube", success=False, error_category=ErrorCategory.NO_SUBTITLE,
                 error_message="视频无字幕")
    print(coord.generate_phase_report(phase=1))
    print("\n--- 模拟重试 ---")
    coord.record("vid2", "youtube", success=True, error_message="重试成功")
    print(coord.generate_combined_report())

    print("\n=== 自检完成 ===")
