#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_client.py - AI服务层模块 (v5.3.0)

v5.3.0 变更: DeepSeek API - disabled thinking mode (thinking: {"type": "disabled"})

功能：
- AI API调用（支持多模型fallback）
- 模型选择（根据内容长度选择省钱/强力模型）
- AI评分结果解析
- 错误日志记录

使用方法：
    from ai_client import call_api, select_model, parse_appeal_score

    model, desc = select_model(content_length)
    result = call_api(content, system_prompt, model)
    score, summary, details = parse_appeal_score(result)
"""

import re
import json
import time
import datetime
import urllib.request
import urllib.error
import threading
from typing import Tuple, List
from concurrent.futures import ThreadPoolExecutor

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CONFIG


_log_lock = threading.Lock()
_ai_executor = ThreadPoolExecutor(max_workers=CONFIG['max_workers'], thread_name_prefix="ai_worker")


def select_model(content_length: int) -> Tuple[list, str]:
    """
    根据内容长度选择合适的 AI 模型列表 (v3.5.0: 返回完整列表以支持备选链)
    
    返回 (模型列表, 描述字符串)
    """
    threshold = CONFIG.get('cheap_threshold', 5000)
    if content_length <= threshold:
        models = CONFIG['model_cheap']
        if isinstance(models, str):
            models = [models]
        return list(models), f"省钱模型 ({models[0]})"
    models = CONFIG['model_heavy']
    if isinstance(models, str):
        models = [models]
    return list(models), f"强力模型 ({models[0]})"


def log_failed_ai_response(ai_result: str, error_type: str, error_detail: str = "") -> None:
    """记录 AI 调用失败日志"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, "data", "ai_parse_errors.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _log_lock:
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"时间: {timestamp}\n")
            f.write(f"错误类型: {error_type}\n")
            if error_detail:
                f.write(f"错误详情: {error_detail}\n")
            f.write(f"AI返回内容:\n{ai_result}\n")


def parse_appeal_score(ai_result: str) -> Tuple[int, str, dict]:
    """解析 AI 吸引力评分结果"""
    try:
        json_match = re.search(r'```json\s*([\s\S]*?)\s*```', ai_result)
        if json_match:
            data = json.loads(json_match.group(1))
        else:
            json_start = ai_result.find('{')
            json_end = ai_result.rfind('}') + 1
            if json_start != -1 and json_end > json_start:
                data = json.loads(ai_result[json_start:json_end])
            else:
                print(f"\n[ERROR] JSON解析失败 - AI返回内容: {ai_result[:500]}", flush=True)
                log_failed_ai_response(ai_result, "JSON格式解析失败", "无法找到有效的JSON对象")
                return 50, "解析失败", {}

        score = data.get('score', 50)
        summary = data.get('summary', '')

        details = {
            'game_relevance': data.get('game_relevance', '未知'),
            'info_value': data.get('info_value', '未知'),
            'target_audience': data.get('target_audience', '未知'),
            'innovation': data.get('innovation', '未知'),
            'clarity': data.get('clarity', '未知')
        }

        return score, summary, details
    except Exception as e:
        print(f"\n[ERROR] JSON解析异常: {e} - AI返回内容: {ai_result[:500]}", flush=True)
        log_failed_ai_response(ai_result, "JSON解析异常", str(e))
        return 50, "解析失败", {}


def call_api(content: str, system_prompt: str, model: str = None, content_length: int = 0) -> str:
    """调用 AI API（支持多模型 fallback 和超时重试）"""
    if model is None:
        model = CONFIG['model_heavy'][0] if isinstance(CONFIG['model_heavy'], list) else CONFIG['model_heavy']

    if content_length == 0:
        content_length = len(content)

    if model is not None:
        model_list = [model] if isinstance(model, str) else model
    elif content_length <= CONFIG.get('cheap_threshold', 5000):
        model_list = CONFIG['model_cheap'] if isinstance(CONFIG['model_cheap'], list) else [CONFIG['model_cheap']]
    else:
        model_list = CONFIG['model_heavy'] if isinstance(CONFIG['model_heavy'], list) else [CONFIG['model_heavy']]

    last_error = ""

    for model_index, current_model in enumerate(model_list):
        if model_index > 0:
            print(f"\n      -> [INFO] 切换到备用模型: {current_model}", flush=True)

        headers: dict = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {CONFIG['api_key']}"
        }
        payload: dict = {
            "model": current_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content}
            ],
            "temperature": 0.0,
            "extra_body": {"thinking": {"type": "disabled"}}
        }

        data: bytes = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(CONFIG["base_url"], data=data, headers=headers, method="POST")

        for attempt in range(1, 3):
            try:
                with urllib.request.urlopen(req, timeout=180) as response:
                    res_data = json.loads(response.read().decode('utf-8'))
                    return res_data["choices"][0]["message"]["content"].strip()
            except TimeoutError as e:
                last_error = f"超时 (内容长度: {content_length}字符)"
                wait_time = 30 * attempt
                print(f"\n      -> [WARN] 模型 {current_model} 请求超时，休眠 {wait_time} 秒后重试 ({attempt}/2)...", flush=True)
                time.sleep(wait_time)
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}"
                if e.code == 400:
                    return f"[ERR] 模型 {current_model} 不支持此请求: HTTP 400"
                wait_time = 15 * attempt
                if e.code in (429, 500, 502, 503, 504):
                    print(f"\n      -> [WARN] 模型 {current_model} 限流/拥堵 (HTTP {e.code})，休眠 {wait_time} 秒后重试 ({attempt}/2)...", flush=True)
                    time.sleep(wait_time)
                else:
                    break
            except urllib.error.URLError as e:
                last_error = f"网络异常: {str(e)}"
                wait_time = 15 * attempt
                print(f"\n      -> [WARN] 模型 {current_model} 网络异常，休眠 {wait_time} 秒后重试 ({attempt}/2)...", flush=True)
                time.sleep(wait_time)
            except Exception as e:
                last_error = f"未知异常: {str(e)}"
                break

        if model_index < len(model_list) - 1:
            print(f"      -> [WARN] 模型 {current_model} 调用失败，尝试备用模型...", flush=True)

    return f"[ERR] 所有模型调用失败: {last_error}"


def call_api_thread(content: str, system_prompt: str, model: str = None, content_length: int = 0) -> str:
    """在线程池中调用 AI API"""
    future = _ai_executor.submit(call_api, content, system_prompt, model, content_length)
    return future.result(timeout=600)


def get_ai_executor() -> ThreadPoolExecutor:
    """获取 AI 任务线程池"""
    return _ai_executor
