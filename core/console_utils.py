#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
console_utils.py - Windows 控制台 UTF-8 修复工具

用途：解决 Windows 控制台输出中文乱码问题
使用方法：在脚本最开始导入此模块即可自动修复

示例：
    from console_utils import fix_windows_console
    fix_windows_console()
"""

import sys
import os
import socket


PROXY_PORT_CANDIDATES = [17891, 7890, 7891, 10809, 10808, 1080, 8080, 37890, 47890]
_DETECTED_PROXY_PORT = None


def detect_proxy_port() -> int:
    """
    自动检测本地可用的代理端口 (v3.4.2)
    
    检测策略：
    1. 优先从 .env 文件读取配置
    2. 扫描常见代理端口列表
    3. 返回第一个可连接的端口
    
    返回：
        可用的代理端口号，如果都不可用返回 0
    """
    global _DETECTED_PROXY_PORT
    
    if _DETECTED_PROXY_PORT is not None:
        return _DETECTED_PROXY_PORT
    
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
    env_port = None
    
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        key, val = line.split('=', 1)
                        key = key.strip()
                        val = val.strip()
                        if key in ('HTTP_PROXY', 'HTTPS_PROXY') and val:
                            try:
                                port_str = val.split(':')[-1].rstrip('/')
                                env_port = int(port_str)
                                break
                            except (ValueError, IndexError):
                                pass
        except Exception:
            pass
    
    ports_to_check = [env_port] if env_port else []
    ports_to_check.extend([p for p in PROXY_PORT_CANDIDATES if p not in ports_to_check])
    
    for port in ports_to_check:
        if port and _test_port(port):
            _DETECTED_PROXY_PORT = port
            return port
    
    _DETECTED_PROXY_PORT = 0
    return 0


def _test_port(port: int) -> bool:
    """测试端口是否可连接"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', port))
        sock.close()
        return result == 0
    except Exception:
        return False


def get_proxy_url() -> str:
    """
    获取代理URL (v3.4.2)
    
    返回：
        代理URL字符串，如 http://127.0.0.1:17891
        如果没有可用代理返回空字符串
    """
    port = detect_proxy_port()
    if port:
        return f"http://127.0.0.1:{port}"
    return ""


def setup_proxy() -> None:
    """
    设置代理环境变量 (v3.4.2)
    
    自动检测可用代理端口并设置环境变量。
    仅用于 youtube_transcript_api 等需要代理的模块。
    
    注意：
        飞书API不需要代理，请勿全局设置代理。
    """
    proxy_url = get_proxy_url()
    if proxy_url:
        os.environ['HTTP_PROXY'] = proxy_url
        os.environ['HTTPS_PROXY'] = proxy_url
        os.environ['http_proxy'] = proxy_url
        os.environ['https_proxy'] = proxy_url
        print(f"  [INFO] Auto-detected proxy: {proxy_url}", flush=True)
    else:
        print("  [WARN] No proxy detected, YouTube access may fail", flush=True)


def fix_windows_console() -> None:
    """
    修复 Windows 控制台 UTF-8 编码问题
    
    解决的问题：
    1. Windows 默认使用 GBK 编码，导致中文输出乱码
    2. Python 的 print() 在 Windows 控制台可能抛出 UnicodeEncodeError
    3. 某些特殊字符（如 emoji）无法正常显示
    
    实现原理：
    1. 设置环境变量 PYTHONIOENCODING=utf-8
    2. 重新包装 sys.stdout 和 sys.stderr 为 UTF-8 编码
    3. 调用 Windows API 启用控制台虚拟终端序列（支持颜色和特殊字符）
    """
    if sys.platform != 'win32':
        return
    
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    
    import io
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer,
        encoding='utf-8',
        errors='replace',
        line_buffering=True
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer,
        encoding='utf-8',
        errors='replace',
        line_buffering=True
    )
    
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)


def safe_print(text: str, end: str = '\n', flush: bool = True) -> None:
    """
    安全打印函数，确保 UTF-8 输出
    
    参数：
        text: 要打印的文本
        end: 结尾字符，默认换行
        flush: 是否立即刷新缓冲区
    
    说明：
        此函数在 fix_windows_console() 之后使用，
        确保所有输出都是 UTF-8 编码，不会出现乱码。
    """
    try:
        print(text, end=end, flush=flush)
    except UnicodeEncodeError:
        print(text.encode('utf-8', errors='replace').decode('utf-8'), end=end, flush=flush)


if __name__ == "__main__":
    fix_windows_console()
    print("[OK] Windows console UTF-8 fix completed")
    print(f"[INFO] Detected proxy port: {detect_proxy_port()}")
    print(f"[INFO] Proxy URL: {get_proxy_url()}")
    print("[TEST] Chinese output: Hello World!")
