#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core/run_workflow.py - 端到端工作流执行器 (v5.4.0)

替代 run.bat 中的中文 echo 部分, 避免 cmd.exe GBK 代码页乱码问题.

阶段分两类:
- 阻断阶段 (Fetcher/Prescreen/ScriptDedup/Orchestrator): 任一失败立即停止
- 可选阶段 (Stage 1.5 标记类): 失败或超时仅打印 WARN, 不阻断主流程;
  且带硬超时兜底 (v5.3.6), 防止脚本内部网络挂起导致整个工作流卡死

v5.4.0: 标记类阶段前移至 Stage 1.5 (Fetcher 之后、Prescreen 之前) —
Discord 补录视频当轮即可参与
机器初筛 -> ScriptDedup -> AI审核 全流程, 来源标记不再前置写高优先级,
AI 审核完成后由 Orchestrator 统一翻转。
"""
import os
import sys
import time
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT / "core"))

try:
    from version import __version__
except ImportError:
    __version__ = "unknown"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run_stage(name: str, cmd_args: list, timeout: int = 0) -> bool:
    log(f"[{name}] START: {' '.join(cmd_args)}")
    start = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, *cmd_args],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            timeout=timeout if timeout > 0 else None,
            stdin=subprocess.DEVNULL,
        )
        elapsed = time.time() - start
        if proc.returncode == 0:
            log(f"[{name}] OK ({elapsed:.1f}s)")
            return True
        log(f"[{name}] FAILED (exit={proc.returncode}, {elapsed:.1f}s)")
        return False
    except subprocess.TimeoutExpired:
        log(f"[{name}] TIMEOUT after {timeout}s")
        return False
    except Exception as e:
        log(f"[{name}] EXCEPTION: {e}")
        return False


def check_cookies() -> bool:
    """启动前强制校验 cookies: 有效则静默通过, 需更新则中止并提示。"""
    log("Cookie 快速检测 ...")
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tools" / "check_cookies.py")],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return proc.returncode == 0


def main() -> int:
    # v5.4.2: 记录本次运行起始时刻, 完整成功后写入运行锚点,
    #   供下次 ScriptDedup 计算"候选池/已标记"下界 (摆脱固定 7 天窗口)
    run_start_ms = int(time.time() * 1000)

    print("=" * 60)
    print(f"  Content Censor Workflow v{__version__}")
    print("  DeepSeek Edition (disabled thinking mode)")
    print("=" * 60)
    print()

    try:
        from run_anchor import describe as describe_anchor
        print(f"  {describe_anchor()}")
        print()
    except ImportError as e:
        print(f"  [WARN] 运行锚点模块不可用: {e}")
        print()

    # v5.3.1: 启动完整工作流前, 首先执行 cookies 快速检测
    if not check_cookies():
        print()
        print("=" * 60)
        print("  WORKFLOW ABORTED: cookies 未更新!")
        print("  请先更新 cookies (导出浏览器 Cookie 到 cookies.json 后运行 tools/refresh_cookies.py)")
        print("=" * 60)
        return 1

    # v5.4.0: Stage 1.5 标记类阶段 — 前移至 Fetcher 之后、Prescreen 之前,
    #   补录视频当轮参与全流程; 失败不阻断主流程, 仅打印警告
    optional_stages = [
        ("Stage 1.5: Discord 自荐标记",   ["core/discord_video_mark.py"]),
        ("Stage 1.5: 中文精选来源标记",   ["core/curated_cn_mark.py"]),
    ]

    stages = [
        ("Stage 1: Fetcher",         ["core/bilibili_youtube_once_for_all_fetcher.py"]),
        ("Stage 2: Prescreen (Bili)", ["core/prescreen.py", "--platform", "bilibili"]),
        ("Stage 2: Prescreen (YT)",   ["core/prescreen.py", "--platform", "youtube"]),
        ("Stage 2.5: ScriptDedup (Bili)", ["core/prescreen.py", "--platform", "bilibili", "--script"]),
        ("Stage 2.5: ScriptDedup (YT)",   ["core/prescreen.py", "--platform", "youtube", "--script"]),
        # v5.2.2: 全自动模式: --auto 跳过交互菜单 + input() 阻塞
        ("Stage 3: Orchestrator (Bili)", ["core/workflow_orchestrator.py", "--stage", "human-prescreen", "--platform", "bilibili", "--auto"]),
        ("Stage 3: Orchestrator (YT)",   ["core/workflow_orchestrator.py", "--stage", "human-prescreen", "--platform", "youtube", "--auto"]),
    ]

    # Stage 1 -> Stage 1.5 (阻断: Fetcher 失败立即停止)
    blocking_head = stages[:1]
    # Stage 2 Prescreen -> Stage 3 (阻断)
    blocking_tail = stages[1:]

    for name, args in blocking_head:
        if not run_stage(name, args):
            print()
            print("=" * 60)
            print(f"  WORKFLOW FAILED at: {name}")
            print("=" * 60)
            return 1

    # v5.4.0: Stage 1.5 — Fetcher 之后、Prescreen 之前; 失败不阻断, 继续后续 run_stage
    # v5.3.6: 增加硬超时兜底 (900s) — run_stage 对超时返回 False,
    #   即使脚本内部请求挂死 (DNS/代理半开连接等无超时场景) 也不会卡住整个工作流
    for name, args in optional_stages:
        if not run_stage(name, args, timeout=900):
            log(f"[WARN] {name} 失败或超时, 不阻断主流程, 继续执行后续阶段")

    for name, args in blocking_tail:
        if not run_stage(name, args):
            print()
            print("=" * 60)
            print(f"  WORKFLOW FAILED at: {name}")
            print("=" * 60)
            return 1

    print()
    print("=" * 60)
    print("  WORKFLOW COMPLETED SUCCESSFULLY")
    print("  Next step: open Feishu to review AI results")
    print("=" * 60)

    # v5.4.2: 仅在完整成功后写锚点 — 失败时保持旧锚点,
    #   下次窗口仍覆盖本次未处理的记录, 避免漏处理
    try:
        from run_anchor import set_last_run
        run_end_ms = int(time.time() * 1000)
        set_last_run(run_start_ms, run_end_ms)
        print(f"  [锚点已更新] 本次运行 {run_start_ms} ~ {run_end_ms}")
    except ImportError as e:
        print(f"  [WARN] 运行锚点写入失败 (不影响本次结果): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
