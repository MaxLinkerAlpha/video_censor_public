#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
youtube_rate_limiter.py - YouTube API 智能速率限制器 (v1.0)

专门解决 YouTube 字幕 API 批量请求时的 IP 封禁问题。

核心功能:
1. 自适应速率控制 - 根据成功/失败动态调整请求间隔
2. 分批处理机制 - 避免一次性大量请求触发风控
3. 智能退避策略 - 指数退避 + 抖动，模拟人类行为
4. 健康度监控 - 实时检测 IP 状态，及时预警

设计理念:
- **尊重平台规则**: YouTube 允许 ~10-15次/分钟，我们控制在 8-12次/分钟
- **拟人化行为**: 随机延迟 + 变化间隔，避免被识别为机器人
- **优雅降级**: 即使遇到封禁也能自动恢复，不会完全中断

使用方法:
    # 作为独立模块使用
    from youtube_rate_limiter import YouTubeRateLimiter
    limiter = YouTubeRateLimiter()

    for video_url in video_list:
        limiter.wait_before_request()
        result = fetch_youtube_subtitle(video_url)
        limiter.record_result(success=result is not None)

依赖:
    - time, random (Python 标准库)
"""

import time
import random
import threading
from datetime import datetime
from typing import Optional, Tuple, List
from dataclasses import dataclass, field


@dataclass
class RateLimitConfig:
    """速率限制配置参数"""
    
    # 基础配置
    base_interval: float = 4.5          # 基础请求间隔 (秒) - 推荐 3-5秒
    random_jitter: float = 2.5         # 随机抖动范围 (±秒) - 模拟人类行为
    
    # 分批处理配置
    batch_size: int = 6                # 每批处理视频数量 - 推荐 5-8个
    batch_cooldown: float = 50.0       # 批次间冷却时间 (秒) - 推荐 45-60秒
    
    # 失败退避配置
    initial_backoff: float = 8.0       # 初始退避时间 (秒)
    max_backoff: float = 120.0         # 最大退避时间 (秒)
    backoff_multiplier: float = 2.0     # 退避乘数 (指数增长)
    max_consecutive_failures: int = 3   # 连续失败多少次后触发长冷却
    
    # 成功加速配置
    success_threshold: int = 8         # 连续成功多少次后可微调加速
    acceleration_factor: float = 0.9   # 加速因子 (<1.0 表示减少间隔)
    min_interval: float = 2.5          # 最小允许间隔 (秒) - 安全底线
    
    # 冷却配置
    long_cooldown: float = 90.0        # 长冷却时间 (秒) - 触发后等待
    recovery_interval: float = 6.0     # 恢复后的初始间隔 (秒)


@dataclass
class RequestStatistics:
    """请求统计信息"""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    current_interval: float = 4.5
    last_request_time: float = 0.0
    last_success_time: float = 0.0
    last_failure_time: float = 0.0
    last_failure_reason: str = ""
    batches_completed: int = 0
    total_wait_time: float = 0.0


class YouTubeRateLimiter:
    """
    YouTube API 智能速率限制器
    
    使用示例:
        limiter = YouTubeRateLimiter(config=RateLimitConfig(
            base_interval=5.0,
            batch_size=6
        ))
        
        videos = ["url1", "url2", ..., "url31"]
        
        # 方式1: 自动分批处理
        for batch in limiter.split_into_batches(videos):
            print(f"开始处理批次 {limiter.stats.batches_completed + 1}")
            for url in batch:
                limiter.wait_before_request()
                # ... 执行请求 ...
                limiter.record_result(success=True/False)
            
            limiter.wait_batch_cooldown()  # 批次间冷却
        
        # 方式2: 手动控制
        for url in videos:
            if limiter.should_pause_for_cooldown():
                limiter.enter_long_cooldown()
            limiter.wait_before_request()
            # ... 执行请求 ...
    """
    
    def __init__(self, config: Optional[RateLimitConfig] = None):
        """
        初始化速率限制器
        
        参数:
            config: 配置对象，如果为 None 则使用默认配置
        """
        self.config = config or RateLimitConfig()
        self.stats = RequestStatistics(
            current_interval=self.config.base_interval
        )
        self._lock = threading.Lock()
        self._batch_counter = 0
        
        # 初始化日志
        import logging
        self.logger = logging.getLogger("YouTubeRateLimiter")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(logging.INFO)
            formatter = logging.Formatter('[%(levelname)s] %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)
    
    def calculate_next_interval(self) -> float:
        """
        计算下一次请求的等待间隔
        
        算法:
        1. 从基础间隔开始
        2. 添加随机抖动 (±jitter)
        3. 如果连续成功超过阈值 → 微调加速
        4. 如果有失败记录 → 应用退避
        5. 确保不低于最小间隔
        
        返回:
            应该等待的秒数
        """
        with self._lock:
            interval = self.config.base_interval
            
            # 1. 添加随机抖动 (模拟人类行为的不可预测性)
            jitter = random.uniform(-self.config.random_jitter, self.config.random_jitter)
            interval += jitter
            
            # 2. 连续成功加速 (但要保持安全底线)
            if (self.stats.consecutive_successes >= self.config.success_threshold 
                and self.stats.consecutive_failures == 0):
                
                acceleration = self.config.acceleration_factor ** (
                    self.stats.consecutive_successes - self.config.success_threshold
                )
                interval *= acceleration
            
            # 3. 连续失败退避 (指数增长)
            if self.stats.consecutive_failures > 0:
                backoff = self.config.initial_backoff * (
                    self.config.backoff_multiplier ** (self.stats.consecutive_failures - 1)
                )
                backoff = min(backoff, self.config.max_backoff)
                interval = max(interval, backoff)
            
            # 4. 确保不低于最小间隔 (安全底线)
            interval = max(interval, self.config.min_interval)
            
            # 更新当前间隔
            self.stats.current_interval = round(interval, 2)
            
            return self.stats.current_interval
    
    def wait_before_request(self, reason: str = "") -> float:
        """
        在发起请求前调用此方法进行等待
        
        参数:
            reason: 等待原因说明 (用于日志)
        
        返回:
            实际等待的时间 (秒)
        """
        interval = self.calculate_next_interval()
        
        self.logger.debug(f"等待 {interval:.2f}秒... {reason}")
        
        start_wait = time.time()
        time.sleep(interval)
        actual_wait = time.time() - start_wait
        
        with self._lock:
            self.stats.total_wait_time += actual_wait
            self.stats.last_request_time = time.time()
        
        return actual_wait
    
    def record_result(self, success: bool, error_msg: str = "") -> None:
        """
        记录一次请求的结果
        
        必须在每次请求完成后调用，用于调整后续速率
        
        参数:
            success: 是否成功
            error_msg: 失败原因 (可选)
        """
        with self._lock:
            self.stats.total_requests += 1
            
            if success:
                self.stats.successful_requests += 1
                self.stats.consecutive_successes += 1
                self.stats.consecutive_failures = 0  # 重置失败计数
                self.stats.last_success_time = time.time()
            else:
                self.stats.failed_requests += 1
                self.stats.consecutive_failures += 1
                self.stats.consecutive_successes = 0  # 重置成功计数
                self.stats.last_failure_time = time.time()
                self.stats.last_failure_reason = error_msg[:100]
    
    def should_pause_for_cooldown(self) -> bool:
        """
        检查是否应该进入冷却期
        
        返回:
            True 如果连续失败次数达到阈值
        """
        return self.stats.consecutive_failures >= self.config.max_consecutive_failures
    
    def enter_long_cooldown(self, custom_duration: Optional[float] = None) -> float:
        """
        进入长冷却期 (当检测到可能的IP封禁时调用)
        
        参数:
            custom_duration: 自定义冷却时长 (秒)，如果为None则使用配置值
        
        返回:
            实际冷却时间 (秒)
        """
        duration = custom_duration or self.config.long_cooldown
        
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logger.warning(
            f"\n[{timestamp}] *** 触发长冷却模式 ***"
        )
        self.logger.warning(
            f"    原因: 连续 {self.stats.consecutive_failures} 次失败"
        )
        if self.stats.last_failure_reason:
            self.logger.warning(
                f"    上次错误: {self.stats.last_failure_reason}"
            )
        self.logger.warning(
            f"    将等待 {duration:.0f} 秒以避免进一步封禁..."
        )
        self.logger.warning(
            f"    建议: 此时可考虑切换代理节点\n"
        )
        
        start_cool = time.time()
        time.sleep(duration)
        actual_cool = time.time() - start_cool
        
        # 重置状态
        with self._lock:
            self.stats.consecutive_failures = 0
            self.stats.current_interval = self.config.recovery_interval
        
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logger.info(f"[{timestamp}] 冷却完成，恢复正常请求")
        
        return actual_cool
    
    def split_into_batches(self, items: List) -> List[List]:
        """
        将列表分割成批次
        
        参数:
            items: 待分割的列表 (如视频URL列表)
        
        返回:
            分割后的批次列表 [[batch1], [batch2], ...]
        """
        batch_size = self.config.batch_size
        batches = []
        
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            batches.append(batch)
        
        self.logger.info(
            f"将 {len(items)} 个项目分为 {len(batches)} 批 "
            f"(每批最多 {batch_size} 个)"
        )
        
        return batches
    
    def wait_batch_cooldown(self, batch_num: Optional[int] = None) -> float:
        """
        批次间冷却等待
        
        在每批处理完成后调用
        
        参数:
            batch_num: 当前批次号 (用于日志)
        
        返回:
            实际等待时间 (秒)
        """
        with self._lock:
            self.stats.batches_completed += 1
            current_batch = batch_num or self.stats.batches_completed
        
        cooldown = self.config.batch_cooldown
        
        # 添加少量随机抖动 (避免固定模式)
        jitter = random.uniform(-5, 5)
        cooldown = max(cooldown + jitter, 30)  # 至少30秒
        
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logger.info(
            f"\n[{timestamp}] === 第 {current_batch} 批完成 ==="
        )
        self.logger.info(
            f"    统计: 本批处理完成"
        )
        self.logger.info(
            f"    冷却: 等待 {cooldown:.0f} 秒..."
        )
        
        start_cool = time.time()
        time.sleep(cooldown)
        actual_cool = time.time() - start_cool
        
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logger.info(f"[{timestamp}] 冷却完成，继续下一批\n")
        
        return actual_cool
    
    def get_status_report(self) -> str:
        """
        生成当前状态报告
        
        返回:
            格式化的状态字符串
        """
        stats = self.stats
        success_rate = (
            stats.successful_requests / stats.total_requests * 100
            if stats.total_requests > 0 else 0
        )
        
        report = f"""
╔════════════════════════════════════════════╗
║     YouTube 速率限制器 - 状态报告           ║
╠════════════════════════════════════════════╣
║  请求统计:                                   ║
║    总请求数: {stats.total_requests:<6}                       ║
║    成功: {stats.successful_requests:<6} | 失败: {stats.failed_requests:<6}              ║
║    成功率: {success_rate:>5.1f}%                           ║
║                                              ║
║  当前状态:                                   ║
║    连续成功: {stats.consecutive_successes:<3} | 连续失败: {stats.consecutive_failures:<3}           ║
║    当前间隔: {stats.current_interval:.2f}s                        ║
║                                              ║
║  批次信息:                                   ║
║    已完成批次: {stats.batches_completed:<3}                         ║
║    总等待时间: {stats.total_wait_time:.1f}s                      ║
║                                              ║
║  上次操作:                                   ║
║    最后成功: {datetime.fromtimestamp(stats.last_success_time).strftime('%H:%M:%S') if stats.last_success_time else 'N/A':<10}              ║
║    最后失败: {datetime.fromtimestamp(stats.last_failure_time).strftime('%H:%M:%S') if stats.last_failure_time else 'N/A':<10}              ║
╚════════════════════════════════════════════╝
"""
        return report


# ==================== 便捷函数 ====================

def create_conservative_limiter() -> YouTubeRateLimiter:
    """
    创建保守型速率限制器 (适用于严格环境)
    
    特点:
    - 较长的请求间隔 (6秒)
    - 较小的批次大小 (5个)
    - 较长的冷却时间 (60秒)
    
    适用场景:
    - IP 容易被封的环境
    - 首次运行或测试阶段
    """
    config = RateLimitConfig(
        base_interval=6.0,
        random_jitter=3.0,
        batch_size=5,
        batch_cooldown=60.0,
        success_threshold=10,
        long_cooldown=120.0
    )
    return YouTubeRateLimiter(config=config)


def create_balanced_limiter() -> YouTubeRateLimiter:
    """
    创建均衡型速率限制器 (推荐日常使用)
    
    特点:
    - 平衡的请求间隔 (4.5秒)
    - 适中的批次大小 (6个)
    - 合理的冷却时间 (50秒)
    
    适用场景:
    - 日常批量处理任务
    - 效率和稳定性的平衡点
    """
    return YouTubeRateLimiter()


def create_aggressive_limiter() -> YouTubeRateLimiter:
    """
    创建激进型速率限制器 (仅限低风险环境)
    
    ⚠️ 警告: 仅在确定不会被封的环境使用！
    
    特点:
    - 较短的请求间隔 (3秒)
    - 较大的批次大小 (8个)
    - 较短的冷却时间 (35秒)
    
    适用场景:
    - 有多个代理IP轮换
    - 紧急任务需要快速完成
    """
    config = RateLimitConfig(
        base_interval=3.0,
        random_jitter=1.5,
        batch_size=8,
        batch_cooldown=35.0,
        success_threshold=15,
        min_interval=2.0
    )
    return YouTubeRateLimiter(config=config)


# ==================== 测试代码 ====================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="YouTube 速率限制器测试工具 v1.0"
    )
    parser.add_argument("--mode", choices=["conservative", "balanced", "aggressive"],
                        default="balanced", help="速率限制模式")
    parser.add_argument("--simulate", type=int, default=20,
                        help="模拟请求数量 (默认20)")
    parser.add_argument("--fail-rate", type=float, default=0.2,
                        help="模拟失败率 0.0-1.0 (默认0.2)")
    
    args = parser.parse_args()
    
    print("\n" + "="*60)
    print("  YouTube 智能速率限制器 - 模拟测试")
    print("="*60 + "\n")
    
    # 选择模式
    if args.mode == "conservative":
        limiter = create_conservative_limiter()
        mode_name = "保守模式"
    elif args.mode == "aggressive":
        limiter = create_aggressive_limiter()
        mode_name = "激进模式"
    else:
        limiter = create_balanced_limiter()
        mode_name = "均衡模式"
    
    print(f"运行模式: {mode_name}")
    print(f"模拟请求: {args.simulate} 个 (失败率: {args.fail_rate:.0%})\n")
    
    # 分批处理
    all_items = [f"video_{i}" for i in range(args.simulate)]
    batches = limiter.split_into_batches(all_items)
    
    start_total = time.time()
    
    for batch_idx, batch in enumerate(batches, 1):
        print(f"\n{'='*50}")
        print(f"📦 开始处理第 {batch_idx}/{len(batches)} 批 ({len(batch)} 个视频)")
        print(f"{'='*50}\n")
        
        for item in batch:
            # 等待
            wait_time = limiter.wait_before_request()
            
            # 模拟请求结果
            is_success = random.random() > args.fail_rate
            error_msg = "Simulated IP block" if not is_success else ""
            
            # 记录结果
            limiter.record_result(success=is_success, error_msg=error_msg)
            
            # 显示状态
            status = "✓" if is_success else "✗"
            print(f"  [{status}] {item:<15} | "
                  f"间隔: {limiter.stats.current_interval:5.2f}s | "
                  f"等待: {wait_time:5.2f}s", end="")
            
            if not is_success:
                print(f" | 错误: {error_msg}")
                
                # 检查是否需要冷却
                if limiter.should_pause_for_cooldown():
                    print(f"\n  ⚠️  连续失败达到阈值，进入冷却...")
                    limiter.enter_long_cooldown()
            else:
                print()
        
        # 批次间冷却 (最后一批除外)
        if batch_idx < len(batches):
            limiter.wait_batch_cooldown(batch_idx)
    
    total_time = time.time() - start_total
    
    # 打印最终报告
    print(limiter.get_status_report())
    
    print(f"\n⏱️  总耗时: {total_time:.1f}秒 ({total_time/60:.1f}分钟)")
    print(f"📊 平均每个请求: {total_time/args.simulate:.2f}秒")
