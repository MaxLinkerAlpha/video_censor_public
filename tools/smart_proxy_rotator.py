#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smart_proxy_rotator.py - 代理节点诊断与监控工具 (v2.1)

[定位说明 - v4.0 更新]
本工具定位为**独立运维诊断工具**，不集成到主工作流中。

原因:
1. 主流程已集成 youtube_rate_limiter (降频模块)，可解决 90%+ 的封禁问题
2. 本工具用于处理剩余 10% 的长期IP封禁场景（需要手动切换节点）
3. 遵循 KISS 原则：不在主流程维护两套代理控制逻辑

核心功能:
1. 代理端口可达性检测
2. YouTube 字幕 API 可访问性测试
3. 节点健康度评分与历史记录
4. 智能推荐最佳可用节点
5. 持续监控模式

适用场景:
    ✅ IP 被长期封禁时的诊断排查
    ✅ 切换节点前的预测试验证
    ✅ 批量任务前的健康检查
    ❌ 不适用于: 主流程自动调用 (已由 youtube_rate_limiter 接管)

使用场景:
    # 场景1: 快速检查当前代理状态
    python smart_proxy_rotator.py --check

    # 场景2: 全面测试所有开放端口
    python smart_proxy_rotator.py --full-test

    # 场景3: 列出 config.yaml 中所有代理节点
    python smart_proxy_rotator.py --list-nodes

    # 场景4: 持续监控模式 (每5分钟检测一次)
    python smart_proxy_rotator.py --monitor --interval 300

依赖:
    - pyyaml (用于解析 config.yaml)
    - requests (用于 HTTP 测试)
    - youtube_transcript_api (用于字幕 API 测试)

版本历史:
    v2.0 (初始版): 作为代理轮换系统设计
    v2.1 (当前版): 重定位为诊断/监控工具，主流程降频已移交 youtube_rate_limiter
"""

import os
import sys
import json
import time
import socket
import logging
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional, Any
from collections import defaultdict
from pathlib import Path
from dataclasses import dataclass, field, asdict
from threading import Lock
import re

# ============================================================
# 常量与配置
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.yaml")
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
HEALTH_CACHE_FILE = os.path.join(DATA_DIR, ".proxy_health.json")
LOG_FILE = os.path.join(DATA_DIR, ".proxy_monitor.log")

# 当前使用的代理端口（从 .env 读取）
CURRENT_PROXY_PORT = 17891  # 默认值，会从 .env 动态读取

# YouTube 测试视频 ID 列表（选择高可用性的知名视频）
YT_TEST_VIDEO_IDS = [
    "dQw4w9WgXcQ",   # Rick Astley - Never Gonna Give You Up (最稳定的测试视频)
    "jNQXAC9IVRw",   # Me at the zoo (YouTube 第一个视频)
    "9bZkp7q19f0",   # PSY - Gangnam Style (高流量视频)
]

# 测试超时设置（秒）
TEST_TIMEOUT = 20
CONNECT_TIMEOUT = 10

# 健康度评分阈值
HEALTH_THRESHOLD = 0.6  # 低于此值认为节点不健康
SUCCESS_RATE_WEIGHT = 0.7  # 成功率权重
RECENT_FAILURE_WEIGHT = 0.3  # 最近失败次数权重

# 监控配置
DEFAULT_MONITOR_INTERVAL = 300  # 默认监控间隔（秒）
MAX_FAILURES_BEFORE_SWITCH = 3  # 连续失败多少次后建议切换
COOLDOWN_PERIOD = 60  # 冷却期（秒），避免频繁切换


@dataclass
class NodeHealth:
    """节点健康度数据结构"""
    name: str = ""
    region: str = ""
    port: int = 0
    total_checks: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    last_check_time: str = ""
    last_success_time: str = ""
    last_failure_reason: str = ""
    avg_response_time: float = 0.0
    health_score: float = 1.0  # 0.0 ~ 1.0，越高越健康
    is_current: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典以便序列化"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'NodeHealth':
        """从字典创建实例"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class ProxyHealthManager:
    """
    代理节点健康管理器

    负责维护所有节点的健康度数据，提供持久化存储和查询功能。
    采用单例模式确保全局只有一个管理器实例。
    """

    _instance: Optional['ProxyHealthManager'] = None
    _lock: Lock = Lock()

    def __new__(cls) -> 'ProxyHealthManager':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._health_data: Dict[str, NodeHealth] = {}
        self._setup_logging()  # 先初始化日志系统
        self._load_from_cache()  # 再加载缓存数据

    def _setup_logging(self) -> None:
        """配置日志系统"""
        os.makedirs(DATA_DIR, exist_ok=True)

        self.logger = logging.getLogger("SmartProxyRotator")
        self.logger.setLevel(logging.DEBUG)

        # 文件处理器（记录详细日志）
        fh = logging.FileHandler(LOG_FILE, encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh_format = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        fh.setFormatter(fh_format)
        self.logger.addHandler(fh)

        # 控制台处理器（只显示重要信息）
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch_format = logging.Formatter('[%(levelname)s] %(message)s')
        ch.setFormatter(ch_format)
        self.logger.addHandler(ch)

    def _load_from_cache(self) -> None:
        """从缓存文件加载健康度数据"""
        if not os.path.exists(HEALTH_CACHE_FILE):
            self.logger.debug("健康度缓存文件不存在，将创建新文件")
            return

        try:
            with open(HEALTH_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)

            for port_str, health_dict in data.items():
                try:
                    self._health_data[port_str] = NodeHealth.from_dict(health_dict)
                except Exception as e:
                    self.logger.warning(f"加载端口 {port_str} 数据失败: {e}")

            self.logger.info(f"已加载 {len(self._health_data)} 个节点的健康度数据")
        except Exception as e:
            self.logger.error(f"加载健康度缓存失败: {e}")

    def save_to_cache(self) -> bool:
        """保存健康度数据到缓存文件"""
        try:
            os.makedirs(os.path.dirname(HEALTH_CACHE_FILE), exist_ok=True)

            data = {
                port: health.to_dict()
                for port, health in self._health_data.items()
            }

            with open(HEALTH_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            self.logger.debug(f"已保存 {len(data)} 个节点的健康度数据")
            return True
        except Exception as e:
            self.logger.error(f"保存健康度数据失败: {e}")
            return False

    def update_node_health(
        self,
        port: int,
        success: bool,
        response_time: float = 0.0,
        reason: str = "",
        node_name: str = "",
        region: str = ""
    ) -> NodeHealth:
        """
        更新指定端口的健康度数据

        参数:
            port: 代理端口号
            success: 是否成功
            response_time: 响应时间（秒）
            reason: 失败原因（如果成功则为空）
            node_name: 节点名称（可选）
            region: 节点所属地区（可选）

        返回:
            更新后的 NodeHealth 对象
        """
        port_str = str(port)

        # 获取或创建健康度记录
        if port_str not in self._health_data:
            self._health_data[port_str] = NodeHealth(
                name=node_name,
                region=region,
                port=port
            )

        health = self._health_data[port_str]
        now = datetime.now().isoformat()

        # 更新基本信息
        if node_name and not health.name:
            health.name = node_name
        if region and not health.region:
            health.region = region

        # 更新统计数据
        health.total_checks += 1
        health.last_check_time = now

        if success:
            health.success_count += 1
            health.consecutive_failures = 0
            health.last_success_time = now
            health.last_failure_reason = ""

            # 更新平均响应时间（使用移动平均）
            if response_time > 0:
                if health.avg_response_time == 0:
                    health.avg_response_time = response_time
                else:
                    # 使用指数移动平均，权重 0.3
                    health.avg_response_time = (
                        0.7 * health.avg_response_time + 0.3 * response_time
                    )
        else:
            health.failure_count += 1
            health.consecutive_failures += 1
            health.last_failure_time = now
            health.last_failure_reason = reason[:100]  # 截断过长的错误信息

        # 重新计算健康度评分
        health.health_score = self._calculate_health_score(health)

        # 保存到缓存
        self.save_to_cache()

        return health

    def _calculate_health_score(self, health: NodeHealth) -> float:
        """
        计算节点健康度评分

        算法:
        - 基础分 = 成功率 * SUCCESS_RATE_WEIGHT
        - 惩罚项 = 最近连续失败次数 * 0.1
        - 最终得分 = max(0, 基础分 - 惩罚项)

        返回:
            0.0 ~ 1.0 之间的浮点数
        """
        if health.total_checks == 0:
            return 1.0  # 新节点默认满分

        # 计算成功率
        success_rate = health.success_count / health.total_checks

        # 基础分（成功率加权）
        base_score = success_rate * SUCCESS_RATE_WEIGHT

        # 惩罚因子（连续失败次数）
        failure_penalty = min(health.consecutive_failures * 0.1, 0.5)

        # 最终得分
        final_score = max(0.0, base_score - failure_penalty)

        return round(final_score, 3)

    def get_node_health(self, port: int) -> Optional[NodeHealth]:
        """获取指定端口的健康度信息"""
        return self._health_data.get(str(port))

    def get_all_healthy_nodes(self, threshold: float = HEALTH_THRESHOLD) -> List[NodeHealth]:
        """
        获取所有健康的节点（按健康度排序）

        参数:
            threshold: 健康度阈值，低于此值的节点将被过滤

        返回:
            按健康度降序排列的 NodeHealth 列表
        """
        healthy_nodes = [
            health for health in self._health_data.values()
            if health.health_score >= threshold
        ]

        # 按健康度降序排序
        healthy_nodes.sort(key=lambda x: (-x.health_score, x.consecutive_failures))

        return healthy_nodes

    def get_best_node(self) -> Optional[NodeHealth]:
        """获取最佳节点（健康度最高的）"""
        healthy = self.get_all_healthy_nodes()
        return healthy[0] if healthy else None

    def should_switch_node(self, port: int) -> Tuple[bool, str]:
        """
        判断是否应该切换当前节点

        返回:
            (是否需要切换, 原因说明)
        """
        health = self.get_node_health(port)
        if not health:
            return False, "无历史数据"

        # 检查连续失败次数
        if health.consecutive_failures >= MAX_FAILURES_BEFORE_SWITCH:
            return True, f"连续失败 {health.consecutive_failures} 次，超过阈值 {MAX_FAILURES_BEFORE_SWITCH}"

        # 检查健康度评分
        if health.health_score < HEALTH_THRESHOLD:
            return True, f"健康度过低 ({health.health_score:.2f} < {HEALTH_THRESHOLD})"

        # 检查是否有更好的节点可用
        best = self.get_best_node()
        if best and best.port != port and best.health_score > health.health_score + 0.2:
            return True, f"存在更优节点 ({best.name[:30]}... 评分 {best.health_score:.2f})"

        return False, "当前节点状态良好"


class YouTubeAccessTester:
    """
    YouTube 字幕 API 可访问性测试器

    专门针对 youtube_transcript_api 进行测试，
    因为这是我们实际使用的访问方式。
    """

    def __init__(self, timeout: int = TEST_TIMEOUT):
        self.timeout = timeout
        self.logger = logging.getLogger("SmartProxyRotator")

    def test_transcript_access(self, port: int) -> Tuple[bool, str, float]:
        """
        测试通过指定代理端口访问 YouTube 字幕 API

        参数:
            port: 代理端口号

        返回:
            (是否成功, 详细信息, 响应时间秒数)
        """
        start_time = time.time()

        # 设置环境变量
        os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{port}"
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{port}"

        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            from youtube_transcript_api.proxies import GenericProxyConfig

            proxy_url = f"http://127.0.0.1:{port}"
            ytt_api = YouTubeTranscriptApi(
                proxy_config=GenericProxyConfig(
                    http_url=proxy_url,
                    https_url=proxy_url
                )
            )

            # 尝试获取测试视频的字幕
            for video_id in YT_TEST_VIDEO_IDS:
                try:
                    transcript_list = ytt_api.fetch(video_id, languages=["en"])
                    if transcript_list and len(transcript_list) > 0:
                        elapsed = time.time() - start_time
                        info = f"成功获取 {len(transcript_list)} 条字幕片段 (视频: {video_id})"
                        self.logger.debug(f"Port {port}: {info}")
                        return True, info, elapsed

                except Exception as e:
                    error_msg = str(e).upper()

                    # IP 封禁检测
                    if "IP" in error_msg and ("BLOCK" in error_msg or "FORBID" in error_msg):
                        elapsed = time.time() - start_time
                        return False, "IP 被 YouTube 封禁", elapsed

                    # 视频不可用，尝试下一个
                    if any(keyword in error_msg for keyword in ["DISABLED", "NOT FOUND", "UNAVAILABLE"]):
                        continue

                    # 其他错误
                    elapsed = time.time() - start_time
                    return False, f"API 错误: {str(e)[:80]}", elapsed

            # 所有测试视频都不可用
            elapsed = time.time() - start_time
            return False, "所有测试视频均无可用字幕", elapsed

        except ImportError:
            elapsed = time.time() - start_time
            return False, "缺少 youtube_transcript_api 库", elapsed
        except Exception as e:
            elapsed = time.time() - start_time
            return False, f"连接错误: {str(e)[:80]}", elapsed


class ConfigParser:
    """配置文件解析器"""

    @staticmethod
    def parse_config_yaml() -> Dict[str, Any]:
        """
        解析 config.yaml 文件

        返回:
            包含配置信息的字典，包含:
            - mixed_port: 混合端口号
            - proxies: 代理节点列表
            - proxy_groups: 代理组信息
        """
        if not os.path.exists(CONFIG_FILE):
            return {"error": "config.yaml 不存在"}

        try:
            import yaml
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            return {
                "mixed_port": config.get("mixed-port", 0),
                "proxies": config.get("proxies", []),
                "proxy_groups": config.get("proxy-groups", [])
            }
        except ImportError:
            return {"error": "需要安装 pyyaml 库"}
        except Exception as e:
            return {"error": f"解析失败: {str(e)}"}

    @staticmethod
    def extract_nodes_by_region(proxies: List[Dict]) -> Dict[str, List[Dict]]:
        """
        按地区分组提取节点

        返回:
            {地区名称: [节点列表]}
        """
        regions = defaultdict(list)

        region_keywords = [
            ("日本", ["日本"]),
            ("美国", ["美国"]),
            ("新加坡", ["新加坡"]),
            ("香港", ["香港"]),
            ("韩国", ["韩国"]),
            ("台湾", ["台湾"]),
            ("英国", ["英国"]),
            ("德国", ["德国"]),
            ("法国", ["法国"]),
            ("加拿大", ["加拿大"]),
            ("澳大利亚", ["澳大利亚", "澳洲"]),
            ("其他", []),  # 兜底
        ]

        for proxy in proxies:
            name = proxy.get("name", "")
            matched = False

            for region, keywords in region_keywords[:-1]:  # 排除"其他"
                if any(kw in name for kw in keywords):
                    regions[region].append(proxy)
                    matched = True
                    break

            if not matched:
                regions["其他"].append(proxy)

        return dict(regions)


class EnvFileManager:
    """.env 文件管理器"""

    @staticmethod
    def read_current_proxy_port() -> Optional[int]:
        """从 .env 文件读取当前代理端口"""
        if not os.path.exists(ENV_FILE):
            return None

        try:
            with open(ENV_FILE, 'r', encoding='utf-8') as f:
                content = f.read()

            # 匹配 HTTP_PROXY 或 HTTPS_PROXY 行
            match = re.search(r'HTTP_PROXY=http://127\.0\.0\.1:(\d+)', content)
            if match:
                return int(match.group(1))
        except Exception as e:
            logging.getLogger("SmartProxyRotator").warning(f"读取 .env 失败: {e}")

        return None

    @staticmethod
    def update_proxy_port(new_port: int) -> bool:
        """
        更新 .env 文件中的代理端口

        参数:
            new_port: 新的端口号

        返回:
            是否更新成功
        """
        if not os.path.exists(ENV_FILE):
            print(f"[ERR] .env 文件不存在: {ENV_FILE}")
            return False

        try:
            with open(ENV_FILE, 'r', encoding='utf-8') as f:
                content = f.read()

            new_url = f"http://127.0.0.1:{new_port}"

            # 替换 HTTP_PROXY 和 HTTPS_PROXY
            content = re.sub(
                r'^HTTP_PROXY=http://[^\s]+',
                f'HTTP_PROXY={new_url}',
                content,
                flags=re.MULTILINE
            )
            content = re.sub(
                r'^HTTPS_PROXY=http://[^\s]+',
                f'HTTPS_PROXY={new_url}',
                content,
                flags=re.MULTILINE
            )

            with open(ENV_FILE, 'w', encoding='utf-8') as f:
                f.write(content)

            logger = logging.getLogger("SmartProxyRotator")
            logger.info(f"[OK] .env 代理端口已更新为: {new_url}")
            return True

        except Exception as e:
            logger = logging.getLogger("SmartProxyRotator")
            logger.error(f"更新 .env 失败: {e}")
            return False


class SmartProxyRotator:
    """
    智能代理轮换系统主类

    整合所有子模块，提供统一的接口供外部调用。

    使用示例:
        rotator = SmartProxyRotator()

        # 获取最佳代理 URL
        proxy_url = rotator.get_best_proxy_url()

        # 测试当前代理
        is_ok, msg = rotator.test_current_proxy()

        # 手动报告结果
        rotator.report_result(success=True, response_time=2.5)
    """

    def __init__(self):
        self.health_manager = ProxyHealthManager()
        self.tester = YouTubeAccessTester()
        self.config_parser = ConfigParser()
        self.env_manager = EnvFileManager()
        self.logger = logging.getLogger("SmartProxyRotator")

        # 读取当前代理端口
        self.current_port = EnvFileManager.read_current_proxy_port() or CURRENT_PROXY_PORT

    def scan_available_ports(self) -> List[int]:
        """
        扫描本地开放的代理端口

        返回:
            排序后的端口号列表
        """
        common_ports = [17891, 37890, 7890, 7891, 10809, 10808, 8080]

        # 从 config.yaml 获取混合端口
        config = self.config_parser.parse_config_yaml()
        if "mixed_port" in config and config["mixed_port"] > 0:
            mixed_port = config["mixed_port"]
            if mixed_port not in common_ports:
                common_ports.append(mixed_port)

        open_ports = []
        for port in set(common_ports):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex(("127.0.0.1", port))
            sock.close()

            if result == 0:
                open_ports.append(port)

        return sorted(open_ports)

    def test_single_port(self, port: int) -> Tuple[bool, str, float]:
        """
        测试单个端口的 YouTube 可访问性

        参数:
            port: 要测试的端口号

        返回:
            (是否成功, 信息描述, 响应时间)
        """
        self.logger.info(f"正在测试端口 {port}...")
        success, info, response_time = self.tester.test_transcript_access(port)

        # 更新健康度数据
        self.health_manager.update_node_health(
            port=port,
            success=success,
            response_time=response_time,
            reason="" if success else info
        )

        status = "[OK]" if success else "[FAIL]"
        self.logger.info(f"  Port {port} {status} {info} ({response_time:.2f}s)")

        return success, info, response_time

    def test_all_ports(self) -> Dict[int, Tuple[bool, str]]:
        """
        测试所有可用端口

        返回:
            {端口号: (是否成功, 信息)}
        """
        ports = self.scan_available_ports()
        if not ports:
            self.logger.warning("[WARN] 未发现任何开放端口!")
            return {}

        self.logger.info(f"\n开始测试 {len(ports)} 个代理端口...\n")
        results = {}

        for port in ports:
            success, info, _ = self.test_single_port(port)
            results[port] = (success, info)

            # 测试间隔，避免过于频繁
            time.sleep(1)

        return results

    def get_best_proxy_url(self) -> Optional[str]:
        """
        获取最佳代理 URL

        逻辑:
        1. 优先返回健康度最高的可用节点
        2. 如果没有健康节点，返回当前端口（降级模式）
        3. 如果当前端口也不可用，返回 None

        返回:
            代理 URL 字符串，如 "http://127.0.0.1:17891"
        """
        best_node = self.health_manager.get_best_node()

        if best_node and best_node.health_score >= HEALTH_THRESHOLD:
            return f"http://127.0.0.1:{best_node.port}"

        # 降级：使用当前端口
        if self.current_port:
            self.logger.warning(
                f"[WARN] 无健康节点可用，降级使用当前端口 {self.current_port}"
            )
            return f"http://127.0.0.1:{self.current_port}"

        return None

    def test_current_proxy(self) -> Tuple[bool, str]:
        """
        测试当前正在使用的代理

        返回:
            (是否可用, 详细信息)
        """
        success, info, _ = self.test_single_port(self.current_port)
        return success, info

    def check_and_suggest(self) -> Dict[str, Any]:
        """
        检查当前状态并给出建议

        返回:
            包含状态和建议的字典
        """
        current_health = self.health_manager.get_node_health(self.current_port)
        should_switch, reason = self.health_manager.should_switch_node(self.current_port)
        best_node = self.health_manager.get_best_node()

        result = {
            "current_port": self.current_port,
            "current_health": current_health.to_dict() if current_health else None,
            "should_switch": should_switch,
            "switch_reason": reason,
            "best_alternative": best_node.to_dict() if best_node else None,
            "available_ports": self.scan_available_ports(),
            "healthy_nodes": [
                h.to_dict() for h in self.health_manager.get_all_healthy_nodes()
            ]
        }

        return result

    def report_result(
        self,
        success: bool,
        response_time: float = 0.0,
        error_msg: str = ""
    ) -> None:
        """
        报告一次代理使用结果（供外部调用方使用）

        参数:
            success: 是否成功
            response_time: 响应时间
            error_msg: 错误消息（如果失败）
        """
        self.health_manager.update_node_health(
            port=self.current_port,
            success=success,
            response_time=response_time,
            reason=error_msg if not success else ""
        )

    def print_status_report(self) -> None:
        """打印详细的代理状态报告"""
        print("\n" + "=" * 70)
        print("  智能代理轮换系统 - 状态报告")
        print("=" * 70)

        # 当前端口信息
        print(f"\n[当前代理]")
        print(f"  端口: {self.current_port}")
        current = self.health_manager.get_node_health(self.current_port)
        if current:
            print(f"  健康度: {current.health_score:.2f}")
            print(f"  成功率: {current.success_count}/{current.total_checks}")
            print(f"  连续失败: {current.consecutive_failures}")
            if current.last_failure_reason:
                print(f"  上次失败: {current.last_failure_reason}")
        else:
            print("  状态: 无历史数据（尚未测试）")

        # 切换建议
        should_switch, reason = self.health_manager.should_switch_node(self.current_port)
        print(f"\n[切换建议]")
        if should_switch:
            print(f"  *** 建议: 是 ({reason}) ***")
        else:
            print(f"  建议: 否 ({reason})")

        # 最佳替代节点
        best = self.health_manager.get_best_node()
        if best and best.port != self.current_port:
            print(f"\n[最佳替代节点]")
            print(f"  端口: {best.port}")
            print(f"  名称: {best.name[:40]}...")
            print(f"  健康度: {best.health_score:.2f}")

        # 所有健康节点
        healthy = self.health_manager.get_all_healthy_nodes()
        print(f"\n[健康节点列表] (阈值 >= {HEALTH_THRESHOLD})")
        if healthy:
            for i, h in enumerate(healthy[:10], 1):  # 最多显示10个
                marker = " <-- 当前" if h.port == self.current_port else ""
                print(f"  {i}. Port {h.port:5d} | 评分: {h.health_score:.2f} | "
                      f"成功: {h.success_count}/{h.total_checks} | {h.region}{marker}")
        else:
            print("  (无)")

        # 可用端口
        available = self.scan_available_ports()
        print(f"\n[可用端口] 共 {len(available)} 个: {available}")

        print("\n" + "=" * 70 + "\n")


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(
        description="智能代理轮换系统 v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python smart_proxy_rotator.py --check         # 快速检查当前代理状态
  python smart_proxy_rotator.py --full-test     # 全面测试所有端口
  python smart_proxy_rotator.py --list-nodes    # 列出 config.yaml 所有节点
  python smart_proxy_rotator.py --report        # 打印详细状态报告
  python smart_proxy_rotator.py --monitor       # 持续监控模式
        """
    )

    parser.add_argument("--check", action="store_true",
                        help="快速检查当前代理状态")
    parser.add_argument("--full-test", action="store_true",
                        help="全面测试所有可用端口")
    parser.add_argument("--list-nodes", action="store_true",
                        help="列出 config.yaml 中的所有代理节点")
    parser.add_argument("--report", action="store_true",
                        help="打印详细的状态报告")
    parser.add_argument("--monitor", action="store_true",
                        help="持续监控模式（按 Ctrl+C 停止）")
    parser.add_argument("--interval", type=int, default=DEFAULT_MONITOR_INTERVAL,
                        help=f"监控间隔（秒），默认 {DEFAULT_MONITOR_INTERVAL}")

    args = parser.parse_args()

    rotator = SmartProxyRotator()

    if args.list_nodes:
        config = rotator.config_parser.parse_config_yaml()
        if "error" in config:
            print(f"[ERR] {config['error']}")
            return

        proxies = config.get("proxies", [])
        regions = rotator.config_parser.extract_nodes_by_region(proxies)

        print(f"\n{'='*70}")
        print(f"  config.yaml 代理节点概览 (共 {len(proxies)} 个)")
        print(f"{'='*70}\n")

        for region, nodes in sorted(regions.items()):
            types = set(n.get("type", "?") for n in nodes)
            print(f"[{region}] {len(nodes)} 个节点 (类型: {', '.join(types)})")
            for n in nodes[:3]:  # 每地区显示前3个
                name = n.get("name", "")[:50]
                port = n.get("port", 0)
                print(f"  - {name} (远程端口: {port})")
            if len(nodes) > 3:
                print(f"  ... 还有 {len(nodes)-3} 个节点")
            print()

        return

    if args.check:
        print("[*] 正在检查当前代理状态...\n")
        success, info = rotator.test_current_proxy()

        if success:
            print(f"[OK] 当前代理 (端口 {rotator.current_port}) 可用")
            print(f"     详情: {info}")
        else:
            print(f"[FAIL] 当前代理 (端口 {rotator.current_port}) 不可用")
            print(f"     原因: {info}")
            print("\n建议操作:")
            print("  1. 在代理软件 GUI 中切换到其他节点")
            print("  2. 运行 python smart_proxy_rotator.py --full-test 寻找可用端口")
            print("  3. 运行 python smart_proxy_rotator.py --report 查看详细状态")

        return

    if args.full_test:
        results = rotator.test_all_ports()

        print(f"\n{'='*50}")
        working_ports = [p for p, (ok, _) in results.items() if ok]

        if working_ports:
            print(f"[OK] 发现 {len(working_ports)} 个可用端口:")
            for p in working_ports:
                print(f"  - Port {p}: {results[p][1]}")

            # 如果当前端口不在可用列表中，建议切换
            if rotator.current_port not in working_ports:
                print(f"\n[WARNING] 当前端口 {rotator.current_port} 不可用！")
                print(f"建议切换到: Port {working_ports[0]}")
        else:
            print("[FAIL] 所有端口均被 YouTube 封禁！")
            print("\n解决方案:")
            print("  1. 在代理软件中手动切换到不同地区的节点")
            print("  2. 优先尝试: 美国、新加坡、日本节点")
            print("  3. 切换后重新运行此命令验证")

        return

    if args.report:
        rotator.print_status_report()
        return

    if args.monitor:
        print(f"[*] 启动持续监控模式 (间隔: {args.interval}s)")
        print("    按 Ctrl+C 停止\n")

        try:
            while True:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp}] ", end="", flush=True)

                success, info = rotator.test_current_proxy()

                if success:
                    print(f"[OK] Port {rotator.current_port}")
                else:
                    print(f"[FAIL] {info}")
                    # 检查是否应该切换
                    should, reason = rotator.health_manager.should_switch_node(
                        rotator.current_port
                    )
                    if should:
                        print(f"       >>> 建议: {reason}")

                time.sleep(args.interval)

        except KeyboardInterrupt:
            print("\n\n[*] 监控已停止")
            rotator.print_status_report()
        return

    # 默认: 显示帮助
    parser.print_help()


if __name__ == "__main__":
    main()
