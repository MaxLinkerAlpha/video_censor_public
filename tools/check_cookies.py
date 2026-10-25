#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/check_cookies.py - Cookies 快速检测脚本 (v5.3.10)

在启动完整工作流前调用, 检查 Bilibili / YouTube 的登录 Cookie 是否有效 (未过期、已更新)。

判定标准 (任一命中即视为"需要更新", 退出码 1):
  1. cookie 文件不存在
  2. 关键登录 Cookie 缺失 (B站: SESSDATA/bili_jct/DedeUserID; YT: __Secure-3PSID/SAPISID/SID/HSID/LOGIN_INFO)
  3. 关键登录 Cookie 的 expirationDate 已过期 (早于当前时间)
  4. 文件修改时间距今超过 STALE_DAYS 天 (默认 7 天, 视为陈旧)
  5. 服务端实时校验失败 (v5.3.10 新增):
     - Bilibili: 调用 nav 接口, 若服务端返回未登录 (isLogin=false) 则视为失效

v5.3.10 修复 (假阳性 BUG):
    原实现只做"离线检查" (文件在不在 / 关键项缺不缺 / expirationDate 过没过期 / 文件几天没更新)。
    但 B站 SESSDATA 的 expirationDate 通常还有数月、文件才几天, 因此离线检查一律通过 ——
    即使服务端早已判定登录失效。2026-09-20 周常工作流即因此漏检, 导致 Stage 3 (B站) 19 条
    视频全部误判"无字幕"并写入飞书错误数据。
    现增加服务端实时校验 (nav 接口) 作为权威判据。

    容错原则: 若网络不可达导致"无法校验" (而非"校验失败"), 打印 WARN 但不拦截工作流,
    避免把假阳性改成"网络抖动就block"的假阴性。

全部通过时静默跳过 (退出码 0), 方便嵌入工作流入口自动执行。

用法:
    python tools/check_cookies.py            # 同时检查 Bilibili + YouTube
    python tools/check_cookies.py --bilibili # 仅检查 Bilibili
    python tools/check_cookies.py --youtube  # 仅检查 YouTube
"""
import os
import sys
import json
import time
from pathlib import Path
from typing import Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

BILI_JSON = PROJECT_ROOT / "bilibili_cookies.json"
YT_JSON = PROJECT_ROOT / "youtube_cookies.json"
ENV_FILE = PROJECT_ROOT / ".env"

# 关键登录 Cookie: 缺失或过期即判定"需要更新"
BILI_KEY_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID")
YT_KEY_COOKIES = ("__Secure-3PSID", "__Secure-1PSID", "SAPISID", "SID", "HSID", "LOGIN_INFO")

# 文件修改时间超过该天数视为陈旧, 需要重新导出
STALE_DAYS = 7

# Bilibili 登录态校验接口 (isLogin 字段为权威判据)
BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILI_NAV_TIMEOUT = 15


def _check_file(path: Path, key_cookies, label: str, quiet_ok: bool) -> bool:
    """检查单个 cookie 文件, 返回 True 表示有效 (通过), False 表示需要更新。"""
    problems = []

    if not path.exists():
        problems.append(f"文件不存在: {path.name}")
        _print(label, f"[FAIL] 文件不存在: {path.name}")
        return False

    # 1. 文件修改时间是否陈旧
    mtime = path.stat().st_mtime
    age_days = (time.time() - mtime) / 86400.0
    if age_days > STALE_DAYS:
        problems.append(f"文件修改时间距今 {age_days:.1f} 天 (> {STALE_DAYS} 天), 可能已过期")

    # 2. 解析 cookie, 检查关键登录项
    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except Exception as e:
        problems.append(f"文件解析失败: {e}")
        _print(label, f"[FAIL] {path.name} 解析失败: {e}")
        return False

    by_name = {c.get("name"): c for c in cookies if isinstance(c, dict)}
    now = time.time()
    for name in key_cookies:
        if name not in by_name:
            problems.append(f"缺失关键 Cookie: {name}")
            continue
        exp = by_name[name].get("expirationDate")
        # 仅当存在明确的过期时间且已过期才判失败; session cookie 无过期时间视为有效
        if exp and isinstance(exp, (int, float)) and exp < now:
            problems.append(f"Cookie {name} 已过期 ({time.strftime('%Y-%m-%d', time.localtime(exp))})")

    if problems:
        _print(label, f"[FAIL] {path.name}:")
        for p in problems:
            _print(label, f"  - {p}")
        return False

    if not quiet_ok:
        _print(label, f"[OK] {path.name}: 登录 Cookie 有效 (文件 {age_days:.0f} 天前更新)")
    return True


def _print(label: str, msg: str) -> None:
    print(f"  [{label}] {msg}", flush=True)


def _cookie_header_from_json(path: Path) -> str:
    """从 cookie JSON 文件构建 Cookie 请求头字符串 (无有效内容时返回空串)"""
    if not path.exists():
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _print("Bili", f"[WARN] {path.name} 解析失败, 跳过该来源: {e}")
        return ""
    if not isinstance(cookies, list):
        _print("Bili", f"[WARN] {path.name} 格式非列表, 跳过该来源")
        return ""
    parts = [
        f'{c["name"]}={c["value"]}'
        for c in cookies
        if isinstance(c, dict) and c.get("name") and c.get("value") is not None
    ]
    return "; ".join(parts)


def _cookie_header_from_env() -> str:
    """从 .env 读取 BILIBILI_COOKIE (错误提示引导用户更新的位置)"""
    if not ENV_FILE.exists():
        return ""
    try:
        # 安全读取基线: UTF-8 + errors=replace, 防编码损坏
        with open(ENV_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("BILIBILI_COOKIE="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as e:
        _print("Bili", f"[WARN] 读取 .env 失败, 跳过该来源: {e}")
    return ""


def _probe_bilibili_login(cookie_header: str) -> Tuple[str, str]:
    """调用 B站 nav 接口校验登录态 (权威判据)

    返回 (status, detail):
      "ok"          -> 服务端确认已登录
      "invalid"     -> 服务端确认未登录 (Cookie 真失效)
      "unreachable" -> 网络不可达 / 接口异常, 无法判定
    """
    if not cookie_header:
        return "invalid", "Cookie 内容为空"

    try:
        import requests
    except ImportError:
        return "unreachable", "requests 未安装, 无法联网校验"

    try:
        res = requests.get(
            BILI_NAV_URL,
            headers={
                "Cookie": cookie_header,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com",
            },
            timeout=BILI_NAV_TIMEOUT,
        )
    except requests.RequestException as e:
        # 捕获具体网络异常并降级为"无法判定", 不吞异常
        return "unreachable", f"{type(e).__name__}: {e}"

    try:
        data = res.json()
    except ValueError:
        return "unreachable", f"HTTP {res.status_code} 返回非 JSON"

    if data.get("code") != 0:
        return "invalid", f"接口返回 code={data.get('code')} msg={data.get('msg')}"

    payload = data.get("data") or {}
    if payload.get("isLogin"):
        return "ok", f"服务端确认已登录 (用户: {payload.get('uname', '')})"
    return "invalid", "服务端返回未登录 (isLogin=false), Cookie 已失效"


def _check_bilibili_live(quiet_ok: bool) -> bool:
    """Bilibili 服务端实时校验: 只要有一个 Cookie 来源通过即视为有效

    来源按"实际会被使用的优先级"排序: cookie JSON 文件 -> .env BILIBILI_COOKIE
    """
    candidates = [
        ("bilibili_cookies.json", _cookie_header_from_json(BILI_JSON)),
        (".env BILIBILI_COOKIE", _cookie_header_from_env()),
    ]
    candidates = [(name, ck) for name, ck in candidates if ck]

    if not candidates:
        _print("Bili", "[FAIL] 未找到任何 Bilibili Cookie (json 与 .env 均为空)")
        return False

    unreachable: list = []
    for name, ck in candidates:
        status, detail = _probe_bilibili_login(ck)
        if status == "ok":
            if not quiet_ok:
                _print("Bili", f"[OK] {name}: {detail}")
            return True
        if status == "unreachable":
            unreachable.append(f"{name}: {detail}")
        else:
            _print("Bili", f"[FAIL] {name}: {detail}")

    # 全部来源都因网络问题无法判定 -> 提示但不拦截 (避免网络抖动导致假阴性)
    if len(unreachable) == len(candidates):
        _print("Bili", "[WARN] 无法联网校验 B站登录态, 已跳过实时校验:")
        for d in unreachable:
            _print("Bili", f"  - {d}")
        return True

    _print("Bili", "[FAIL] 所有 Bilibili Cookie 来源均未通过服务端登录校验")
    return False


def main() -> int:
    only_bili = "--bilibili" in sys.argv
    only_yt = "--youtube" in sys.argv
    do_both = not only_bili and not only_yt

    # quiet_ok: 全部通过时尽量精简, 仅打印一行总结果
    ok = True

    if do_both or only_bili:
        # 先做离线检查 (文件/关键项/有效期/文件年龄), 再做服务端实时校验 (权威判据)
        if _check_file(BILI_JSON, BILI_KEY_COOKIES, "Bili", quiet_ok=True):
            if not _check_bilibili_live(quiet_ok=True):
                ok = False
        else:
            ok = False

    if do_both or only_yt:
        if not _check_file(YT_JSON, YT_KEY_COOKIES, "YT", quiet_ok=True):
            ok = False

    if ok:
        # 静默跳过: 仅一行, 便于嵌入工作流
        print("[check_cookies] OK: Bilibili + YouTube cookies 有效, 无需更新", flush=True)
        return 0

    print()
    print("=" * 60)
    print("  COOKIES 需要更新!")
    print("  Bilibili:  请重新导出浏览器 Cookie 到 bilibili_cookies.json")
    print("  YouTube:   请重新导出浏览器 Cookie 到 youtube_cookies.json")
    print("  更新后运行: python tools/refresh_cookies.py")
    print("=" * 60)
    return 1


if __name__ == "__main__":
    sys.exit(main())
