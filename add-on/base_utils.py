#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import os
import json
import time
import requests
import io
import asyncio
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv
from console_utils import get_logger, setup_windows_console

# 强制读取 .env 文件并覆盖当前环境变量
load_dotenv(override=True)
# 初始化 Windows 控制台编码
setup_windows_console()
# 获取日志器
_log = get_logger("INFO")
_log_warn = get_logger("WARN")
_log_err = get_logger("ERROR")
_log_debug = get_logger("DEBUG")

def _ts():
    """返回当前时间戳字符串"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _print(msg: str, prefix: str = ""):
    """
    统一日志输出函数：兼容旧接口，同时输出到终端和日志文件
    - DEBUG 级别: 仅写文件
    - INFO/WARN/ERROR: 同时输出到终端
    """
    if not prefix:
        _log.info(msg)
    elif prefix.upper() == "WARN":
        _log_warn.warning(msg)
    elif prefix.upper() == "ERROR":
        _log_err.error(msg)
    else:
        _log.info(f"[{prefix}] {msg}")

def _get_required_env(key: str) -> str:
    """安全获取环境变量，如果缺失则直接报错阻断，防止后续带着空数据裸奔"""
    val = os.getenv(key)
    if not val:
        raise ValueError(f"[配置致命错误] 缺少必须的环境变量: {key}，请检查 .env 文件！")
    return val


# ================= 探测代理端口是否真的活着，防止 10061 或 10053 错误
def check_proxy_availability(proxy_url: str) -> bool:
    if not proxy_url: return False
    import socket
    from urllib.parse import urlparse
    
    parsed = urlparse(proxy_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    
    try:
        # 尝试在 1 秒内建立 TCP 连接
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        return True
    except:
        return False
    

# ================= 智能代理选择 =================
def select_best_proxy() -> str:
    """
    智能选择最佳代理：
    1. 优先检测用户代理软件 (17890 / 17891 / 4780)
    2. 其次检测自带 clash.exe (37890)
    3. 返回可用的代理URL，如果没有则返回None
    """
    # 检测用户代理软件 - 尝试多个常用端口
    for port in [17890, 17891, 4780]:
        proxy_url = f"http://127.0.0.1:{port}"
        if check_proxy_availability(proxy_url):
            _print(f"检测到用户代理软件 ({port})，将使用 HTTP 代理", "WARN")
            return proxy_url

    # 检测自带 clash.exe (37890)
    if check_proxy_availability("socks5://127.0.0.1:37890"):
        _print(f"检测到自带代理 (37890)，将使用 SOCKS5 代理", "WARN")
        return "socks5://127.0.0.1:37890"

    _print("未检测到可用代理，将尝试直连", "WARN")
    return None

# 自动选择最佳代理
SELECTED_PROXY_URL = select_best_proxy()

# ================= 导出的配置常量 =================
AI_API_KEY: str = _get_required_env("AI_API_KEY")
AI_BASE_URL: str = os.getenv("AI_BASE_URL", "https://api.siliconflow.cn/v1")

# 【架构升级】：引入快慢双模型机制，在 .env 中配置
# 2026-04-24 测试结果：DeepSeek-V3.2 最稳定(100%成功率,5.88s,¥0.27/M)
AI_MODEL_CHEAP: str = os.getenv("AI_MODEL_CHEAP", "deepseek-ai/DeepSeek-V3.2") 
AI_MODEL_HEAVY: str = os.getenv("AI_MODEL_HEAVY", "deepseek-ai/DeepSeek-V3.2")

FEISHU_APP_ID: str = _get_required_env("FEISHU_APP_ID")
FEISHU_APP_SECRET: str = _get_required_env("FEISHU_APP_SECRET")
FEISHU_APP_TOKEN: str = _get_required_env("FEISHU_APP_TOKEN")
FEISHU_TABLE_ID: str = _get_required_env("FEISHU_TABLE_ID")
HISTORY_MAX_SIZE: int = int(os.getenv("HISTORY_MAX_SIZE", 20000))

# 设置环境变量代理（供 OpenAI 客户端使用）
if SELECTED_PROXY_URL:
    os.environ["HTTP_PROXY"] = SELECTED_PROXY_URL
    os.environ["HTTPS_PROXY"] = SELECTED_PROXY_URL

# 初始化全局 AI 客户端（添加超时和重试配置）
# 注意：max_retries 必须设为 0！禁用 urllib3 自动重试
# 因为 analyze_micro_feedback 中已有 for 循环手动重试机制（最多3次）
# 如果开启 urllib3 自动重试，会导致双重重试叠加，在 API 限流时产生严重超时
client = OpenAI(
    api_key=AI_API_KEY, 
    base_url=AI_BASE_URL,
    timeout=30.0,  # 30秒请求超时（稍微放宽一点）
    max_retries=0  # 禁用 urllib3 自动重试，依赖手动重试
)

# ================= 核心业务逻辑 =================

def get_feishu_token() -> str:
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}
    try:
        res = requests.post(url, json=payload, timeout=10)
        return res.json().get("tenant_access_token")
    except:
        return None

async def rate_limit(seconds: float) -> None:
    await asyncio.sleep(seconds)

def trim_history_map(history: dict, max_size: int) -> dict:
    if len(history) <= max_size:
        return history
    items = sorted(history.items(), key=lambda x: (x[1], x[0]))
    trimmed = items[-max_size:]
    return {k: v for k, v in trimmed}

# ================= 增量历史记录管理器（防崩溃回滚） =================
import shutil
from datetime import datetime

class IncrementalHistoryManager:
    """
    增量历史记录管理器：每成功处理一条就立即保存，避免批量保存时崩溃导致全量丢失。

    工作流程：
    1. load() 时自动创建 .bak 备份
    2. mark_success() 每次成功后立即增量保存到磁盘
    3. rollback() 可恢复到上次 load() 前的状态

    使用方式：
        manager = IncrementalHistoryManager("tencent_history.json")
        history = manager.load()
        # ... 处理帖子 ...
        manager.mark_success(comment_id, timestamp, title_prefix)  # 立即保存
        # ... 所有处理完成 ...
        manager.commit()  # 最终确认，删除备份
    """

    def __init__(self, history_file: str, max_size: int = 20000):
        self.history_file = history_file
        self.max_size = max_size
        self.backup_file = history_file + ".bak"
        self.history = {}
        self._backup_done = False

    def load(self) -> dict:
        """加载历史记录，同时创建备份"""
        self.history = {}
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            self.history[str(k)] = {
                                "timestamp": int(v.get("timestamp", 0)),
                                "title_prefix": v.get("title_prefix", "")
                            }
            except Exception:
                pass

        # 创建备份
        if os.path.exists(self.history_file):
            shutil.copy2(self.history_file, self.backup_file)
            self._backup_done = True

        return self.history

    def save(self) -> None:
        """保存历史记录到磁盘（完整保存）"""
        trimmed = trim_history_map(
            {k: v["timestamp"] for k, v in self.history.items()},
            self.max_size
        )
        migrated = {}
        for k, ts in trimmed.items():
            if k in self.history:
                migrated[k] = {"timestamp": ts, "title_prefix": self.history[k].get("title_prefix", "")}
            else:
                migrated[k] = {"timestamp": ts, "title_prefix": ""}

        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(migrated, f, ensure_ascii=False)

    def mark_success(self, comment_id: str, timestamp: int, title_prefix: str = "") -> None:
        """
        标记一条记录为成功处理，并立即增量保存到磁盘。
        这样即使程序崩溃，也只有最后一条记录可能丢失。
        """
        self.history[comment_id] = {
            "timestamp": timestamp,
            "title_prefix": title_prefix
        }
        # 立即增量保存：只写入新记录，不重写整个文件
        self._append_to_file(comment_id, timestamp, title_prefix)

    def _append_to_file(self, comment_id: str, timestamp: int, title_prefix: str) -> None:
        """追加单条记录到历史文件（增量写入）"""
        try:
            # 读取现有数据
            existing = {}
            if os.path.exists(self.history_file):
                try:
                    with open(self.history_file, 'r', encoding='utf-8') as f:
                        existing = json.load(f)
                except Exception:
                    existing = {}

            # 添加新记录
            existing[comment_id] = {"timestamp": timestamp, "title_prefix": title_prefix}

            # 限制大小（LRU淘汰）
            if len(existing) > self.max_size:
                items = sorted(existing.items(), key=lambda x: (x[1].get("timestamp", 0), x[0]))
                existing = {k: v for k, v in items[-self.max_size:]}

            # 写回文件
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False)
        except Exception:
            pass

    def rollback(self) -> dict:
        """回滚到备份文件，恢复到最后一次 load() 前的状态"""
        if self._backup_done and os.path.exists(self.backup_file):
            shutil.copy2(self.backup_file, self.history_file)
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    self.history = json.load(f)
            except Exception:
                self.history = {}
        return self.history

    def commit(self) -> None:
        """提交：删除备份文件，确认保存完成"""
        self._backup_done = False
        if os.path.exists(self.backup_file):
            os.remove(self.backup_file)

    def get_stats(self) -> dict:
        """获取历史记录统计信息"""
        return {
            "total": len(self.history),
            "backup_exists": os.path.exists(self.backup_file),
            "history_file": self.history_file
        }

# 用于 AI 调用的线程池（限制并发避免资源耗尽）
_ai_executor = None

def _get_ai_executor():
    global _ai_executor
    if _ai_executor is None:
        from concurrent.futures import ThreadPoolExecutor
        _ai_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ai_worker")
    return _ai_executor

# ================= 动态字段映射缓存（按分表独立缓存） =================
_feishu_field_cache = {}  # {"table_id": {"timestamp": 0, "fields": {}, "reverse": {}}}

def _fetch_and_cache_fields(table_id: str, force: bool = False) -> dict:
    """
    动态获取飞书表格字段并缓存，避免每次写入都调用API。
    按 table_id 独立缓存，force=True 时强制刷新。
    """
    import time as time_module

    now = time_module.time()
    cached = _feishu_field_cache.get(table_id, {})

    if not force and cached.get("timestamp") and now - cached["timestamp"] < 300 and cached.get("fields"):
        return cached["reverse"]

    token = get_feishu_token()
    if not token:
        return {}

    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{table_id}/fields"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            field_map = {item["field_name"]: item["field_name"] for item in items}
            _feishu_field_cache[table_id] = {"timestamp": now, "fields": field_map, "reverse": field_map}
            return field_map
    except:
        pass
    return {}

def _map_fields(fields: dict, table_id: str) -> dict:
    """
    将代码中的字段名映射到飞书实际的字段名。
    如果飞书字段与代码不一致，使用飞书的实际字段名。
    """
    field_map = _fetch_and_cache_fields(table_id)

    if not field_map:
        return fields

    mapped = {}
    skipped = []
    for code_name, value in fields.items():
        if code_name in field_map:
            mapped[code_name] = value
        else:
            matched = False
            for actual_name in field_map:
                # 模糊匹配：去除空格后比较（飞书字段名可能有多余空格）
                if actual_name.strip() == code_name.strip():
                    mapped[actual_name] = value  # 使用飞书实际字段名作为 key
                    matched = True
                    break
            if not matched:
                skipped.append(code_name)

    if skipped:
        _print(f"以下字段在飞书中未找到，将被跳过: {skipped}", "WARN")

    return mapped

# ================= 致命Bug 硬校验关键词（方案 B） =================
# 2026-07-29 新增：防止 AI 把普通 bug 误判为致命Bug
# 当 AI 分类为"致命Bug"但内容不含以下任何关键词时，自动降级为"一般Bug"
FATAL_KEYWORDS = [
    # 中文关键词
    "崩溃", "闪退", "卡死", "死机", "黑屏", "蓝屏", "白屏", "无法启动", "打不开",
    "坏档", "存档损坏", "存档丢失", "存档消失", "数据丢失", "数据清空", "数据清除",
    "无限加载", "卡加载", "无法进入", "进不去", "启动失败", "开机即崩", "一直崩溃",
    # 英文关键词（与 lower() 后内容匹配）
    "crash", "crashed", "crashes", "crashing", "freeze", "freezing", "frozen",
    "black screen", "unable to launch", "won't start", "wont start", "can't start",
    "cant start", "won't launch", "corrupt", "corrupted", "save lost", "data loss",
    "stuck on loading", "infinite loading", "startup",
]

def analyze_micro_feedback(content: str, context: str = None) -> dict:
    """
    微观单条分析：自动根据长度和内容复杂度路由模型，杜绝本地拦截，保证每条都过 AI。
    添加线程池超时保护，防止单条阻塞。
    支持模型降级：主模型失败时自动切换备用模型。
    支持上下文注入：当提供 context 时，会在 Prompt 中附加主帖背景，帮助 AI 理解回复的上下文。
    """
    from concurrent.futures import TimeoutError as FutureTimeoutError

    critical_keywords = ["崩溃", "闪退", "卡死", "坏档", "丢失", "500", "error", "crash", "bug"]
    needs_deep_thinking = any(kw in content.lower() for kw in critical_keywords)
    # 2026-07-20 调整：长文本阈值从 300 → 1000
    # 原因：V4-Flash 19-45s/次，频繁触发会让 124 篇 Reddit 追补耗时翻 10 倍
    # Qwen2.5-7B 0.55s 免费，质量对绝大多数舆情反馈够用
    is_long_text = len(content) > 1000

    # 模型路由：99% 流量走 Qwen2.5-7B-Instruct（免费极速）
    # 只有超长文本(>1000字)或 致命bug 词才走 V4-Flash
    target_model = AI_MODEL_HEAVY if (needs_deep_thinking or is_long_text) else AI_MODEL_CHEAP
    reason = "关键词触发" if needs_deep_thinking else ("长文本" if is_long_text else "短文本")
    _print(f"{reason} ({len(content)}字) -> 调用模型: {target_model}", "INFO")

    # 备用模型列表（按优先级排序，主模型失败后依次尝试）
    # 2026-07-20 模型路由策略（用户配置）：
    #   主用 AI_MODEL_CHEAP = THUDM/glm-4-9b-0414（免费、5.2s 平均速度）
    #   主用 AI_MODEL_HEAVY = THUDM/glm-4-9b-0414（免费、稳定）
    # 路由决策：
    #   - 所有文本统一走 GLM-4-9B（短文本 1.9s，长文本 8.5s）
    #   - 失败时自动 fallback 到 V4-Flash/Qwen2.5-7B/Qwen3-8B
    # fallback 顺序（任一模型失败后依次尝试）：
    #   1. V4-Flash（质量略高，但慢 40s+）
    #   2. Qwen2.5-7B（短文本极速 1.6s，长文本可能超时）
    #   3. Qwen3-8B（最后兜底）
    # 注意：GLM-4-9B 已在主路由，失败时 fallback 不会重复
    # 2026-07-20 智谱平台 fallback 列表（主模型 glm-4-flash 失败时依次尝试）
    fallback_models = [
        ("glm-4-flashx", "GLM-4-FlashX"),
        ("glm-4-air", "GLM-4-Air"),
    ]

    # 构建上下文部分
    context_section = ""
    if context:
        context_section = f"""
【主帖背景】
{context}

【分析要求】
- 以上是当前回复对应的主帖完整内容，请结合主帖背景分析下面的【待分析内容】
- 如果回复内容是情绪宣泄（如"同上"、"+1"、"顶顶"等），但主帖是负面反馈，请将分类调整为"纯情绪发泄"
- 注意识别回复是否在讨论主帖中提到的具体问题

"""

    prompt = f"""
你是一个极简主义游戏QA。请分析以下游戏反馈内容，返回严格JSON格式。

【极简主义红线】
1. 必须使用中文。
2. "ai_summary" 必须是以1. 2. 3. 换行排版的纯文本，严禁包含数组符号[ ]。
3. 严禁使用"玩家"、"用户"、"反馈"、"认为"、"希望"、"表示"等无意义主语。
4. 直接陈述事实和痛点，禁止冗余描述。

{context_section}【JSON Schema示例】
{{
    "ai_summary": "1. 导弹单位实际伤害与描述不符\n2. 多次测试稳定复现\n3. 建议修复伤害计算",
    "category": "一般Bug",
    "priority": "P2"
}}

【字段说明】
- ai_summary: 核心要点（带序号排版，每条言简意赅）
- category 判定标准（必须严格按以下六类判定）：
  * 战术讨论: 讨论打法/阵容/对局思路/单位搭配，无明确 bug 或改进诉求
  * 致命Bug: 游戏崩溃/闪退/无法启动/坏档/存档丢失/数据清空等，导致无法继续游玩
  * 一般Bug: 功能异常但可正常游玩（按钮失效、数值错误、显示/行为异常）
  * 玩法建议: 对现有机制的改进诉求（新增内容、调整规则）
  * 数值吐槽: 强度/平衡性抱怨（某单位/机制太强或太弱）
  * 纯情绪发泄: 无具体问题描述的抱怨
- category 必须从上述六类中选一[战术讨论, 玩法建议, 数值吐槽, 一般Bug, 致命Bug, 纯情绪发泄]
- priority: 必须选一[P0, P1, P2, P3]（仅致命Bug可标P0，一般Bug最高P2）

待分析内容：
{content}
"""

    def _call_api(model: str):
        """实际的 API 调用函数

        2026-07-20 关键优化：V4-Flash 默认开了 thinking 模式
        - 默认调用 4.82s，reasoning_content 275 字符
        - 加 thinking={"type": "disabled"} 后 1.36s，reasoning 0
        - 提速 3.5x，且 token 消耗大幅下降
        - 仅对 deepseek-ai/* 模型生效（其它厂商如 Qwen 无此参数）
        """
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}
        }
        # DeepSeek 系列模型需要显式关闭 thinking
        if model.startswith("deepseek-"):
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        response = client.chat.completions.create(**kwargs)
        return response

    def _validate_result(res: dict, raw_content: str = "") -> dict:
        """验证并修复 AI 返回结果，确保必有有效分类

        2026-07-29 方案B：新增"致命Bug 硬校验"
        - AI 倾向于把普通 bug 误判为致命Bug（实测 96% 致命Bug 不含致命关键词）
        - 当 AI 判定为致命Bug 但原始内容不含任何致命关键词时，自动降级为一般Bug
        """
        valid_categories = ["战术讨论", "致命Bug", "一般Bug", "玩法建议", "数值吐槽", "纯情绪发泄"]
        valid_priorities = ["P0", "P1", "P2", "P3"]

        if res.get("category") not in valid_categories:
            res["category"] = "玩法建议"

        # 致命Bug 硬校验：内容不含致命关键词则降级为一般Bug（宁轻勿重）
        if res.get("category") == "致命Bug" and raw_content:
            content_lower = raw_content.lower()
            if not any(kw in content_lower for kw in FATAL_KEYWORDS):
                res["category"] = "一般Bug"
                res["priority"] = "P2"  # 同步降级优先级
                _print("致命Bug 硬校验未通过：内容不含致命关键词，降级为一般Bug", "WARN")

        if res.get("priority") not in valid_priorities:
            res["priority"] = "P2"

        return res

    # 获取要尝试的模型列表（按优先级排序）
    models_to_try = []
    if target_model not in [m[0] for m in fallback_models]:
        # 如果当前路由的模型不在备用列表中，优先尝试
        models_to_try.append((target_model, target_model.split("/")[-1]))
    models_to_try.extend(fallback_models)

    # 去重，保持优先级顺序
    seen = set()
    unique_models = []
    for m in models_to_try:
        if m[0] not in seen:
            seen.add(m[0])
            unique_models.append(m)

    # 重试机制：每个模型最多尝试3次
    max_retries = 3

    for model_id, model_name in unique_models:
        _print(f"尝试模型: {model_name}", "INFO")

        for attempt in range(max_retries):
            try:
                executor = _get_ai_executor()
                future = executor.submit(_call_api, model_id)
                response = future.result(timeout=45)

                result = json.loads(response.choices[0].message.content)

                if "ai_summary" in result:
                    summary = result["ai_summary"]
                    if isinstance(summary, list):
                        summary = "\n".join([f"{i+1}. {str(item)}" if not str(item).startswith(str(i+1)) else str(item) for i, item in enumerate(summary)])
                    else:
                        summary = str(summary)

                    for noise in ["[", "]", "'", '"', "玩家反馈", "玩家表示", "玩家觉得", "用户认为", "用户希望", "反馈者"]:
                        summary = summary.replace(noise, "")
                    result["ai_summary"] = summary.strip()

                _print(f"{model_name} 调用成功", "INFO")
                return _validate_result(result, content)

            except FutureTimeoutError:
                if attempt < max_retries - 1:
                    _print(f"{model_name} 超时重试 ({attempt + 2}/{max_retries})...", "WARN")
                    continue
                _print(f"{model_name} 超时，切换备用模型...", "WARN")
                break

            except Exception as e:
                error_msg = str(e)[:50]
                if attempt < max_retries - 1:
                    _print(f"{model_name} 异常: {error_msg}，重试...", "WARN")
                    continue
                _print(f"{model_name} 失败: {error_msg}，切换备用模型...", "WARN")
                break

    _print("所有模型均失败，返回默认值", "ERROR")
    return {"ai_summary": "AI 处理失败", "category": "玩法建议", "priority": "P2"}

def build_feishu_record(post: dict, ai_res: dict, remark: str = "") -> dict:
    """
    统一的飞书字段映射器（支持动态备注）

    2026-07-28 适配新总表字段名：
    - 反馈来源→平台 | 玩家昵称→用户名 | 发帖时间→反馈时间
    - 原文链接→链接 | 原始反馈内容→未处理的原内容 | AI 总结与提炼→AI总结
    - 优先级→AI 优先级 | 媒体与附件链接→在地址栏输入链接打开图片
    - 删除：人工/AI 备注（remark 参数保留但不写入飞书）
    - 处理标签默认值改为"未处理"（只能由人工打标，AI 不参与）

    鲁棒性设计：
    - AI 偶尔会漏掉 ai_summary 字段（约 5% 概率）
    - 此时降级使用原始内容前 100 字作为总结
    - 仍带"⚠️ AI未返回总结"前缀，便于人工识别
    """
    safe_content = post.get("content", "")
    if len(safe_content) > 800:
        safe_content = safe_content[:800] + "..."

    # === AI 总结鲁棒提取 ===
    ai_summary = ""
    if ai_res:
        ai_summary = ai_res.get("ai_summary", "") or ""
    if not ai_summary or not ai_summary.strip():
        # AI 没返回或返回空：用原始内容前 100 字降级
        fallback = safe_content[:100].replace("\n", " ").strip()
        if fallback:
            ai_summary = f"⚠️ AI未返回总结 | 原文摘录: {fallback}"
        else:
            ai_summary = "⚠️ AI未返回总结且原文为空"

    fields = {
        "平台": post.get("platform", "Unknown"),
        "用户名": post.get("author", "匿名玩家"),
        "反馈时间": post.get("post_time_ms", 0),
        "链接": post.get("url", ""),
        "未处理的原内容": safe_content,
        "AI总结": ai_summary,
        "分类": ai_res.get("category", "玩法建议") if ai_res else "玩法建议",
        "AI 优先级": ai_res.get("priority", "P2") if ai_res else "P2",
        "处理标签": "未处理"
    }

    # remark 参数保留兼容性，但不再写入飞书（用户要求不写"备注"字段）
    # if remark:
    #     fields["备注"] = remark

    return fields

def report_channel_failure(channel: str, error_msg: str, traceback_text: str = "") -> None:
    """
    渠道抓取失败告警：写入一条醒目记录到飞书主表，让用户第一时间感知。
    不做重试、不静默——这是用户知情权的底线保障。

    2026-07-28 适配新总表字段名，删除"备注"字段写入。
    """
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    detail = f"时间: {now_str}\n渠道: {channel}\n错误: {error_msg}"
    if traceback_text:
        detail += f"\n堆栈: {traceback_text[:500]}"

    alert_record = {
        "平台": "🚨 系统告警",
        "用户名": "AI 舆情官",
        "反馈时间": int(time.time() * 1000),
        "链接": "",
        "未处理的原内容": detail,
        "AI总结": f"【{channel} 渠道故障】\n{error_msg[:200]}",
        "分类": "其它",
        "AI 优先级": "P0",
        "处理标签": "未处理"
    }
    try:
        write_to_feishu(alert_record)
        _print(f"渠道 [{channel}] 失败告警已写入飞书", "WARN")
    except Exception as e:
        _print(f"无法写入飞书失败告警: {e}", "ERROR")

def write_to_feishu(feishu_fields: dict, target_table_id: str = None) -> None:
    """
    带详细日志打印的飞书写入函数，支持动态分表路由
    """
    token = get_feishu_token()
    if not token:
        _print("飞书 Token 获取失败", "ERROR")
        return

    actual_table_id = target_table_id or FEISHU_TABLE_ID

    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_APP_TOKEN}/tables/{actual_table_id}/records"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    mapped_fields = _map_fields(feishu_fields, actual_table_id)
    payload = {"fields": mapped_fields}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        res_data = response.json()

        if res_data.get("code") == 0:
            rec_id = res_data.get("data", {}).get("record", {}).get("record_id", "未知")
            _print(f"写入成功，记录ID: {rec_id}", "INFO")
        else:
            _print(f"飞书写入异常: {res_data.get('msg')} (代码: {res_data.get('code')})", "ERROR")
            if "field_name not found" in str(res_data):
                _print("请检查子表列名是否与代码一致（注意空格）", "WARN")
    except Exception as e:
        _print(f"飞书网络通信失败: {e}", "ERROR")