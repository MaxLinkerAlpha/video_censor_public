#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workflow_orchestrator.py - 顶层调度器 (v4.3.0)

功能：
1. 协调整个工作流：采集→初筛→人工初筛→字幕获取→评分审核→飞书同步
2. 支持断点续传
3. 支持单独运行某个阶段
4. 支持人工初筛暂停机制（含撤销+重跑菜单）
5. 增强错误诊断报告（代理、风控、飞书连接等）

v3.5.0 更新：
- 采集后新增交互式菜单：撤销B站/YouTube抓取并重跑
- 集成 rollback_fetcher 实现一键回滚
- 增强各阶段错误诊断与用户友好提示

使用方法：
    # 运行完整流程（含交互菜单）
    python workflow_orchestrator.py --full

    # 运行完整流程（自动模式，跳过交互）
    python workflow_orchestrator.py --full --auto

    # 仅运行初筛
    python workflow_orchestrator.py --stage prescreen

    # 仅运行从人工初筛开始的后续流程
    python workflow_orchestrator.py --stage human-prescreen
"""

import os
import sys
import json
import argparse
import subprocess
import time
from datetime import datetime
from typing import List, Dict, Optional, Any


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = SCRIPT_DIR
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
FETCHER_SCRIPT = os.path.join(SCRIPT_DIR, "bilibili_youtube_once_for_all_fetcher.py")
PRESCREEN_SCRIPT = os.path.join(SCRIPT_DIR, "prescreen.py")
SUBTITLE_SCRIPT = os.path.join(PARENT_DIR, "main.py")
VIDEO_URLS_FILE = os.path.join(PARENT_DIR, "video_urls.txt")
PRIORITY_MAP_FILE = os.path.join(PARENT_DIR, "video_priority_map.json")
ROLLBACK_SCRIPT = os.path.join(PARENT_DIR, "tools", "rollback_fetcher.py")

sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, PARENT_DIR)
sys.path.insert(0, os.path.join(PARENT_DIR, "platforms"))

from feishu_fields import (
    BILI_F_BVID, YT_F_VIDEO_ID, BILI_F_AI_REVIEW, YT_F_AI_REVIEW,
    BILI_AI_RESULT_PASS, YT_AI_RESULT_SAFE,
    BILI_HUMAN_HIGH, BILI_HUMAN_MEDIUM, BILI_HUMAN_LOW, BILI_HUMAN_SELECTED,
    BILI_HUMAN_REJECTED,
    YT_HUMAN_SELECTED, YT_HUMAN_HIGH, YT_HUMAN_MEDIUM, YT_HUMAN_LOW,
    YT_HUMAN_REJECTED,
    YT_F_HUMAN_PRESCREEN, BILI_F_HUMAN_PRESCREEN,
    BILI_F_CREATED_AT, BILI_F_TITLE,
    BILI_F_D1, BILI_F_D2, YT_F_D1, YT_F_D2, YT_F_TITLE,
    YT_F_SUBTITLE_STATUS, YT_SUBTITLE_SUCCESS, YT_SUBTITLE_FAILED, YT_SUBTITLE_PENDING,
    YT_F_TOTAL_SCORE, YT_F_RATING, YT_F_D1, YT_F_D2, YT_F_D3, YT_F_D4,
    YT_F_VIDEO_ID as YT_F_VIDEO_ID_CONST,
    YT_F_AI_REVIEW_REPORT, YT_F_CREATED_AT,
)

# v4.5.0: 智能重试引擎（按需导入）
_SMART_RETRY_AVAILABLE = False

# v5.2.2: 提升到模块级，供两个重试循环共享
# v5.3.3: MAX_RETRY_ROUNDS 语义修正 - 表示"重试轮数"（不含初始轮）
#   - MAX_RETRY_ROUNDS = 1 → 实际允许 1 轮初始处理 + 1 轮重试 = 2 轮总处理
#   - 重试条件: if round_num > MAX_RETRY_ROUNDS + 1: break
#   - 这样 round_num=2 允许通过 (即 1 轮重试)，round_num=3 时 break
#   - 用户要求: 每个 YouTube 视频重试一次即可，失败后提示用户更换节点重跑
# v5.3.4: 统一两个重试循环的终止判断 —— 均使用 retry_round（已执行的重试轮数）:
#   if retry_round > MAX_RETRY_ROUNDS: break
#   （完整流程初始轮不计入 retry_round；单独重试第一轮即 retry_round=1）
MAX_RETRY_ROUNDS = 1
try:
    from smart_retry import (
        ErrorCategory,
        SmartRetryEngine,
        TwoPhaseRetryCoordinator,
    )
    _SMART_RETRY_AVAILABLE = True
except ImportError:
    pass

TARGET_VIDEO_COUNT = 8

PRIORITY_ORDER = [BILI_HUMAN_HIGH, BILI_HUMAN_MEDIUM, BILI_HUMAN_LOW, BILI_HUMAN_SELECTED, ""]


class WorkflowOrchestrator:
    """工作流调度器 (v4.0 - 支持平台/阶段选择)"""

    def __init__(self):
        self.results = {}
        self.start_time = datetime.now()
        self.qualified_videos = []
        self._auto_mode = False
        self._fetch_stdout = ""
        self._target_platform = "all"  # v4.0: 目标平台 (all/bilibili/youtube)
        self._days_filter = 7  # v4.2: 时间过滤天数 (1=今天, 7=本周, 0=全部)

    def _build_video_url(self, platform: str, video_id: str) -> str:
        if not video_id:
            return ""
        if platform.lower() == "bilibili":
            return f"https://www.bilibili.com/video/{video_id}/"
        else:
            return f"https://www.youtube.com/watch?v={video_id}"

    def _extract_url(self, link: Any) -> str:
        if not link:
            return ""
        if isinstance(link, str):
            return link
        if isinstance(link, list) and link:
            first = link[0]
            if isinstance(first, dict):
                return str(first.get("text", first.get("link", first.get("url", str(first)))))
            return str(first)
        if isinstance(link, dict):
            return str(link.get("text", link.get("link", link.get("url", ""))))
        return str(link)

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        icons = {"INFO": "[*]", "OK": "[OK]", "ERROR": "[ERROR]", "WARN": "[WARN]", "TIP": "[TIP]"}
        print(f"{timestamp} {icons.get(level, '[*]')} {message}")

    def _diagnose_proxy(self) -> bool:
        """
        v3.5.0: 检测代理是否可用
        
        返回 True 表示代理正常，False 表示异常
        """
        try:
            sys.path.insert(0, SCRIPT_DIR)
            from console_utils import detect_proxy_port, get_proxy_url
            
            proxy_url = get_proxy_url()
            if not proxy_url:
                self.log("未检测到代理服务。YouTube API 和 B站搜索可能需要代理才能访问。", "TIP")
                self.log("解决方案:", "TIP")
                self.log("  1. 启动代理软件 (Clash/V2Ray 等)", "TIP")
                self.log("  2. 确认代理端口在 17891/7890/10809 等常见端口", "TIP")
                self.log("  3. 或在 .env 中设置 HTTP_PROXY / HTTPS_PROXY", "TIP")
                return False
            
            port = detect_proxy_port()
            if port:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                result = sock.connect_ex(("127.0.0.1", port))
                sock.close()
                
                if result != 0:
                    self.log(f"代理端口 {port} 配置了但无法连接！代理可能未启动或端口错误。", "ERROR")
                    self.log(f"当前配置的代理: {proxy_url}", "ERROR")
                    return False
                
                self.log(f"代理检测通过: 127.0.0.1:{port}", "OK")
                return True
            else:
                self.log("代理 URL 已配置但无法解析端口号，请检查格式是否正确", "WARN")
                return False
        except Exception:
            return True

    def _diagnose_bilibili_fetch_result(self, stdout: str) -> None:
        """
        v3.5.0: 分析 Bilibili 抓取输出，诊断常见问题并给出建议
        """
        if not stdout:
            return
        
        lower_out = stdout.lower()
        
        if "未获取到任何视频数据" in stdout or "empty" in lower_out:
            self.log("[诊断] Bilibili 抓取返回空结果，可能原因:", "WARN")
            
            if "风控" in stdout or "验证码" in stdout or "captcha" in lower_out:
                self.log("  -> 触发了 B站风控/验证码拦截", "ERROR")
                self.log("  解决方案:", "TIP")
                self.log("     1. 更新 bilibili_cookies.json (从浏览器重新导出 Cookie)", "TIP")
                self.log("     2. 降低抓取频率，避免短时间大量请求", "TIP")
                self.log("     3. 尝试设置 BILI_HEADLESS=False 用有头模式运行", "TIP")
            
            elif "timeout" in lower_out or "超时" in stdout:
                self.log("  -> 页面加载超时（可能网络慢或被墙）", "ERROR")
                self.log("  解决方案:", "TIP")
                self.log("     1. 检查代理是否正常运行", "TIP")
                self.log("     2. 尝试切换代理节点", "TIP")
            
            else:
                self.log("  -> 搜索结果为空（关键词无匹配或页面结构变化）", "WARN")
                self.log("  建议: 手动打开 https://search.bilibili.com/all?keyword=你的游戏名 确认是否有结果", "TIP")
        
        elif "现有数据已是最新" in stdout or "重复" in stdout:
            self.log("[诊断] Bilibili 抓取成功但全部为历史重复记录，无需推送新数据", "INFO")

    def run_script(self, script_path: str, args: List[str] = None, capture: bool = False) -> tuple:
        cmd = [sys.executable, script_path] + (args or [])
        self.log(f"执行命令: {' '.join(cmd)}")

        # Windows 下 text=True 默认按 cp936 解码子进程输出会乱码;
        # 强制 UTF-8 解码, 并向子进程注入 PYTHONIOENCODING 保证其 stdout 也是 UTF-8
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"

        try:
            result = subprocess.run(
                cmd, capture_output=capture, text=True,
                encoding="utf-8", errors="replace",
                env=child_env, timeout=3600,
            )
            if capture:
                return result.returncode, result.stdout, result.stderr
            return result.returncode, "", ""
        except subprocess.TimeoutExpired:
            self.log(f"脚本执行超时: {script_path}", "ERROR")
            return -1, "", ""
        except Exception as e:
            self.log(f"脚本执行失败: {e}", "ERROR")
            return -1, "", ""

    def stage_fetch(self) -> bool:
        """阶段1: 视频采集"""
        self.log("=" * 50)
        self.log("阶段1: 视频采集")
        self.log("=" * 50)

        if not os.path.exists(FETCHER_SCRIPT):
            self.log(f"采集脚本不存在: {FETCHER_SCRIPT}", "ERROR")
            return False

        code, stdout, stderr = self.run_script(FETCHER_SCRIPT, capture=True)
        self._fetch_stdout = stdout

        if code == 0:
            self.log("视频采集完成", "OK")
            self.results["fetch"] = {"success": True, "stdout": stdout}
            
            # v3.5.0: 诊断性输出分析
            self._diagnose_bilibili_fetch_result(stdout)
            
            return True
        else:
            self.log(f"视频采集失败: {stderr or stdout}", "ERROR")
            
            # v3.5.0: 根据错误类型给出针对性建议
            err_text = (stderr + stdout).lower()
            if "timeout" in err_text or "超时" in err_text:
                self.log("  [诊断] 页面加载超时，最常见原因是:", "ERROR")
                self.log("     A) 代理未启动或端口不匹配 → 运行前请确认代理已开启", "TIP")
                self.log("     B) B站风控导致无法访问 → 更新 cookies.json 后重试", "TIP")
                self._diagnose_proxy()
            elif "playwright" in err_text:
                self.log("  [诊断] Playwright 浏览器引擎错误:", "ERROR")
                self.log("     请运行: playwright install chromium", "TIP")
            elif "proxy" in err_text or "connection" in err_text:
                self.log("  [诊断] 网络连接问题，正在检测代理状态...", "WARN")
                self._diagnose_proxy()
            
            self.results["fetch"] = {"success": False, "error": stderr or stdout}
            return False

    def stage_prescreen(self, platform: str = "bilibili") -> bool:
        """阶段2: 初筛"""
        self.log("=" * 50)
        self.log(f"阶段2: 初筛 ({platform})")
        self.log("=" * 50)

        if not os.path.exists(PRESCREEN_SCRIPT):
            self.log(f"初筛脚本不存在: {PRESCREEN_SCRIPT}", "ERROR")
            return False

        sys.path.insert(0, SCRIPT_DIR)
        try:
            from prescreen import Prescreen
            ps = Prescreen()
            results = ps.run(platform)

            passed = sum(1 for r in results if r["passed"])
            rejected = sum(1 for r in results if not r["passed"])
            
            self.log(f"初筛完成: {passed}/{len(results)} 通过, {rejected} 拒绝", "OK")
            
            # v3.5.0: 如果全部拒绝，给出提示
            if len(results) > 0 and passed == 0:
                self.log(f"[诊断] {platform} 初筛结果: 全部被拒绝", "WARN")
                self.log(f"  可能原因: 视频时长不在 3-45 分钟范围，或发布时间超过 21 天", "TIP")
            
            self.results[f"prescreen_{platform}"] = {"success": True, "passed": passed, "total": len(results)}
            return True

        except Exception as e:
            self.log(f"初筛失败: {e}", "ERROR")
            self.results[f"prescreen_{platform}"] = {"success": False, "error": str(e)}
            return False

    def stage_script_prescreen(self, platform: str = "all") -> bool:
        """阶段 2.5: 脚本自动去重 (v5.2.0 - 仅当周视频 + 替代人工初筛)

        在机器初筛 Passed 基础上, 按 up/youtuber + 语言视频数规则挑选,
        写回 Human Prescreen (Selected/Rejected) + Notes.

        v5.2.0: 传入 self._days_filter, ScriptPrescreen 只处理当周写入的 Passed 视频
          (而非全表历史数据), 避免 "同 UP 去重" 砍掉历史视频时波及当周新视频。

        参数:
            platform: "all" / "bilibili" / "youtube"
        """
        self.log("=" * 60)
        self.log(f"阶段 2.5: 脚本自动去重 (v5.2.0 当周过滤) - 平台: {platform}, days={self._days_filter}")
        self.log("=" * 60)

        sys.path.insert(0, SCRIPT_DIR)
        try:
            from prescreen import ScriptPrescreen
        except ImportError as e:
            self.log(f"导入 ScriptPrescreen 失败: {e}", "ERROR")
            return False

        platforms = ["bilibili", "youtube"] if platform == "all" else [platform]
        overall_ok = True

        for plat in platforms:
            try:
                sp = ScriptPrescreen()
                stats = sp.run(platform=plat, days_filter=self._days_filter)
                stats["success"] = stats.get("failed", 0) == 0  # v5.3.1: 补充 success 键, 避免总结误报 FAIL
                self.log(
                    f"[{plat}] 脚本去重完成: 保留 {stats['kept']} | 丢弃 {stats['dropped']} | 写入失败 {stats['failed']}",
                    "OK" if stats["failed"] == 0 else "WARN",
                )
                self.results[f"script_prescreen_{plat}"] = stats
            except Exception as e:
                self.log(f"[{plat}] 脚本去重失败: {e}", "ERROR")
                self.results[f"script_prescreen_{plat}"] = {"success": False, "error": str(e)}
                overall_ok = False

        return overall_ok

    def _rollback_and_refetch_platform(self, platform: str) -> bool:
        """
        v3.5.0: 撤销指定平台的最近一次抓取，然后重新抓取
        
        参数:
            platform: "bilibili" 或 "youtube"
        
        返回:
            True 表示重跑成功
        """
        self.log(f"\n{'='*50}")
        self.log(f"撤销并重跑: {platform.upper()}")
        self.log(f"{'='*50}")
        
        # Step 1: 执行回滚
        self.log(f"[1/3] 正在查找 {platform} 的最近快照...")
        rollback_code, rollback_out, rollback_err = self.run_script(
            ROLLBACK_SCRIPT,
            args=["--list"],
            capture=True
        )
        
        # Step 2: 执行带 confirm 的回滚
        self.log(f"[2/3] 正在执行回滚操作...")
        rollback_code, _, _ = self.run_script(
            ROLLBACK_SCRIPT,
            args=["--platform", platform, "--confirm", "--skip-excel"],
            capture=True
        )
        
        if rollback_code != 0:
            self.log(f"回滚执行失败 (code={rollback_code})，将直接重跑抓取", "WARN")
            self.log("提示: 如果飞书中有残留的新增记录，可以稍后用 rollback_fetcher.py 手动清理", "TIP")
        else:
            self.log(f"{platform.upper()} 回滚完成", "OK")
        
        # Step 3: 重跑对应平台的抓取
        self.log(f"[3/3] 正在重跑 {platform.upper()} 抓取...")
        
        if platform == "bilibili":
            # 使用单平台 Bilibili 抓取脚本
            bili_only_script = os.path.join(PARENT_DIR, "_run_bili_only.py")
            if os.path.exists(bili_only_script):
                code, out, err = self.run_script(bili_only_script, capture=True)
                self._diagnose_bilibili_fetch_result(out)
                success = (code == 0)
            else:
                # 回退到完整 fetcher
                code, _, _ = self.run_script(FETCHER_SCRIPT, capture=True)
                success = (code == 0)
        else:
            # YouTube 只能通过完整 fetcher 重跑（目前没有单独的 YouTube 脚本）
            code, out, _ = self.run_script(FETCHER_SCRIPT, capture=True)
            success = (code == 0)
        
        if success:
            self.log(f"{platform.upper()} 重跑完成", "OK")
        else:
            self.log(f"{platform.upper()} 重跑失败，请检查上方错误信息", "ERROR")
        
        return success

    def _show_post_fetch_menu(self) -> str:
        """
        v3.5.0: 显示采集后的交互菜单
        
        返回用户选择的操作代码
        """
        print(f"\n{'='*55}")
        print(f"  采集 & 初筛已完成，请选择下一步操作:")
        print(f"{'='*55}")
        print(f"  [1] 继续执行后续流程 (人工初筛 -> 字幕 -> 评分)")
        print(f"  [2] 撤销本次 BILIBILI 抓取 并重新抓取 B站")
        print(f"  [3] 撤销本次 YOUTUBE 抓取 并重新抓取 YouTube")
        print(f"  [4] 同时撤销 B站+YouTube 并全部重跑")
        print(f"  [5] 仅查看当前采集统计信息")
        print(f"  [0] 退出程序")
        print(f"{'='*55}")
        
        try:
            choice = input("\n  请输入选项 [0-5]: ").strip()
            return choice
        except (EOFError, KeyboardInterrupt):
            return "0"

    def _show_fetch_stats(self) -> None:
        """显示当前采集结果的统计摘要"""
        print(f"\n{'='*50}")
        print(f"  当前采集结果统计")
        print(f"{'='*50}")
        
        stdout = self._fetch_stdout or ""
        
        if "新增" in stdout and "条" in stdout:
            import re
            bili_match = re.search(r'本次新增\s*(\d+)\s*条', stdout)
            yt_match = re.search(r'YouTube.*?新增\s*(\d+)\s*条', stdout, re.DOTALL)
            
            if bili_match:
                print(f"  [Bilibili] 本次新增: {bili_match.group(1)} 条")
            if yt_match:
                print(f"  [YouTube]   本次新增: {yt_match.group(1)} 条")
        else:
            print(f"  (未能从输出中提取统计数据)")
        
        # 从 prescreen 结果中获取
        for plat in ["bilibili", "youtube"]:
            key = f"prescreen_{plat}"
            if key in self.results:
                r = self.results[key]
                if r.get("success"):
                    print(f"  [{plat.capitalize()}] 初筛: {r.get('passed', 0)}/{r.get('total', 0)} 通过")
        
        # 快照信息
        if os.path.exists(os.path.join(PARENT_DIR, "data", ".rollback_snapshots")):
            snap_dir = os.path.join(PARENT_DIR, "data", ".rollback_snapshots")
            snaps = [f for f in os.listdir(snap_dir) if f.endswith(".json") and not f.endswith("_rolledback.json")]
            if snaps:
                print(f"\n  [回滚] 可用快照: {len(snaps)} 个")
                for s in sorted(snaps)[-3:]:
                    print(f"           - {s}")
        
        print(f"{'='*50}\n")

    def _collect_priority_videos(self, days_filter: int = None) -> List[Dict[str, Any]]:
        sys.path.insert(0, SCRIPT_DIR)
        from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID, YT_TABLE_ID

        sync = FeishuSync()
        sync.set_bitable_token(BILI_BITABLE_TOKEN)

        priority_only = []
        seen_urls = set()

        platforms = [
            ("bilibili", BILI_TABLE_ID, BILI_F_BVID),
            ("youtube", YT_TABLE_ID, YT_F_VIDEO_ID),
        ]

        for platform, table_id, id_field in platforms:
            video_data = sync.get_videos_for_human_prescreen(platform, days_filter=days_filter)

            priority_videos = video_data.get("priority", [])
            forced_include_videos = video_data.get("forced_include", [])

            self.log(f"  [{platform}] 已标记优先级: {len(priority_videos)} 个")
            self.log(f"  [{platform}] 人工强行入选: {len(forced_include_videos)} 个")

            for v in priority_videos + forced_include_videos:
                url = self._extract_url(v.get("link", ""))
                if not url and v.get("video_id"):
                    url = self._build_video_url(platform, v["video_id"])

                if url and url not in seen_urls:
                    seen_urls.add(url)
                    priority_only.append({
                        "platform": platform,
                        "video_id": v.get("video_id", ""),
                        "title": v.get("title", ""),
                        "url": url,
                        "human_prescreen": v.get("human_prescreen", ""),
                        "d1_score": v.get("d1_score", 0),
                        "d2_score": v.get("d2_score", 0),
                        "source": v.get("source", "priority"),
                    })

        return priority_only

    def _collect_pending_videos_sorted(self, days_filter: int = None) -> List[Dict[str, Any]]:
        sys.path.insert(0, SCRIPT_DIR)
        from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID, YT_TABLE_ID

        sync = FeishuSync()
        sync.set_bitable_token(BILI_BITABLE_TOKEN)

        pending = []
        seen_urls = set()

        platforms = [
            ("bilibili", BILI_TABLE_ID, BILI_F_BVID),
            ("youtube", YT_TABLE_ID, YT_F_VIDEO_ID),
        ]

        for platform, table_id, id_field in platforms:
            video_data = sync.get_videos_for_human_prescreen(platform, days_filter=days_filter)
            pending_videos = video_data.get("pending", [])

            self.log(f"  [{platform}] 待标记视频: {len(pending_videos)} 个")

            for v in pending_videos:
                url = self._extract_url(v.get("link", ""))
                if not url and v.get("video_id"):
                    url = self._build_video_url(platform, v["video_id"])

                if url and url not in seen_urls:
                    seen_urls.add(url)
                    pending.append({
                        "platform": platform,
                        "video_id": v.get("video_id", ""),
                        "title": v.get("title", ""),
                        "url": url,
                        "human_prescreen": v.get("human_prescreen", ""),
                        "d1_score": v.get("d1_score", 0),
                        "d2_score": v.get("d2_score", 0),
                    })

        pending.sort(key=lambda x: (x.get("d2_score", 0) + x.get("d1_score", 0)), reverse=True)
        return pending

    def _supplement_videos(self, current_list: List[Dict], all_passed: List[Dict]) -> List[Dict]:
        if len(current_list) >= TARGET_VIDEO_COUNT:
            return current_list

        seen_urls = {v["url"] for v in current_list}
        supplement = []

        for v in all_passed:
            if v["url"] in seen_urls:
                continue
            if v.get("human_prescreen") == BILI_HUMAN_REJECTED:
                continue

            supplement.append(v)
            seen_urls.add(v["url"])

        supplement.sort(key=lambda x: (x.get("d2_score", 0) + x.get("d1_score", 0)), reverse=True)

        needed = TARGET_VIDEO_COUNT - len(current_list)
        added = 0

        for v in supplement:
            if added >= needed:
                break
            if v["url"] not in {x["url"] for x in current_list}:
                v["source"] = "supplement"
                current_list.append(v)
                added += 1

        return current_list

    def stage_human_prescreen(self, auto_continue: bool = False) -> bool:
        """阶段3: 人工初筛 (v3.5.0)"""
        self.log("=" * 50)
        self.log("阶段3: 人工初筛 (v3.5.0)")
        self.log("=" * 50)

        self.log("收集优先级视频...")
        priority_videos = self._collect_priority_videos(days_filter=self._days_filter)

        priority_count = len(priority_videos)
        self.log(f"优先级视频总数: {priority_count} 个")

        if priority_count == 0:
            self.log("没有找到优先级视频", "WARN")
            self.log("  可能原因:", "TIP")
            self.log("     1. 初筛阶段没有产生 '通过' 的记录", "TIP")
            self.log("     2. 飞书表格中没有标记 '高优先级/中优先级/入选' 的视频", "TIP")
            self.log("     3. 时间过滤排除了所有近期记录", "TIP")
            self.results["human_prescreen"] = {"success": False, "error": "No priority videos"}
            return False

        urls = [v["url"] for v in priority_videos if v.get("url")]
        with open(VIDEO_URLS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(urls) + "\n")

        priority_map = {}
        for v in priority_videos:
            if v.get("url"):
                priority_map[v["url"]] = {
                    "human_priority": v.get("human_prescreen", "normal"),
                    "platform": v.get("platform", ""),
                    "title": v.get("title", ""),
                }
        with open(PRIORITY_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(priority_map, f, ensure_ascii=False, indent=2)

        self.qualified_videos = priority_videos
        self.log(f"已写入 {len(urls)} 个优先级视频到 video_urls.txt", "OK")
        self.log(f"已写入优先级映射到 {PRIORITY_MAP_FILE}", "OK")

        print("\n优先级视频列表:")
        for i, v in enumerate(priority_videos, 1):
            print(f"  {i}. [{v['platform']}] {v['title'][:40]}... (来源:{v['source']})")

        self.results["human_prescreen"] = {"success": True, "count": len(priority_videos)}
        return True

    def stage_subtitle_with_supplement(self) -> bool:
        """字幕获取 + 评分审核 (v3.2.0)"""
        self.log("=" * 50)
        self.log("阶段4: 字幕获取 + 评分审核 (v3.2.0)")
        self.log("=" * 50)

        if not os.path.exists(SUBTITLE_SCRIPT):
            self.log(f"字幕脚本不存在: {SUBTITLE_SCRIPT}", "ERROR")
            return False

        sys.path.insert(0, SCRIPT_DIR)
        try:
            # 已迁移: video_subtitle_censor7 -> main.py
            sys.path.insert(0, PARENT_DIR)
            from main import main as subtitle_main

            subtitle_main()

            self.log("字幕获取 + 评分审核完成", "OK")
            self.results["subtitle"] = {"success": True}
            return True

        except Exception as e:
            self.log(f"字幕处理失败: {e}", "ERROR")
            self.results["subtitle"] = {"success": False, "error": str(e)}
            return False

    def stage_subtitle(self, video_file: str = None) -> bool:
        """阶段4: 字幕获取 + 评分审核"""
        self.log("=" * 50)
        self.log("阶段4: 字幕获取 + 评分审核")
        self.log("=" * 50)

        if not os.path.exists(SUBTITLE_SCRIPT):
            self.log(f"字幕脚本不存在: {SUBTITLE_SCRIPT}", "ERROR")
            return False

        sys.path.insert(0, SCRIPT_DIR)
        try:
            # 已迁移: video_subtitle_censor7 -> main.py
            sys.path.insert(0, PARENT_DIR)
            from main import main as subtitle_main
            subtitle_main()

            self.log("字幕获取 + 评分审核完成", "OK")
            self.results["subtitle"] = {"success": True}
            return True

        except Exception as e:
            self.log(f"字幕处理失败: {e}", "ERROR")
            self.results["subtitle"] = {"success": False, "error": str(e)}
            return False

    def stage_feishu_sync(self) -> bool:
        """阶段5: 飞书同步确认"""
        self.log("=" * 50)
        self.log("阶段5: 飞书同步确认")
        self.log("=" * 50)

        sys.path.insert(0, SCRIPT_DIR)
        try:
            from feishu_sync import FeishuSync
            sync = FeishuSync()
            sync.init_fields(platform="bilibili")
            sync.init_fields(platform="youtube")

            self.log("飞书字段初始化完成 (B站 + YouTube)", "OK")
            self.results["feishu_sync"] = {"success": True}
            return True

        except Exception as e:
            self.log(f"飞书同步失败: {e}", "ERROR")
            self.results["feishu_sync"] = {"success": False, "error": str(e)}
            return False

    def stage_discord_video_mark(self, dry_run: bool = False) -> bool:
        """阶段 2.1a: Discord videos-n-streams 自荐标记 (v5.3.5)

        拉取 Discord 频道最近消息中的 YouTube 链接, 匹配 YT 表并将
        Human Prescreen 为空的记录标记为 Selected。失败不阻断主流程。
        """
        self.log("=" * 50)
        self.log(f"阶段 2.1a: Discord 自荐标记 (v5.3.5){' [dry-run]' if dry_run else ''}")
        self.log("=" * 50)

        script = os.path.join(SCRIPT_DIR, "discord_video_mark.py")
        if not os.path.exists(script):
            self.log(f"脚本不存在: {script}", "ERROR")
            self.results["discord_video_mark"] = {"success": False, "error": "script not found"}
            return False

        args = ["--dry-run"] if dry_run else []
        code, stdout, stderr = self.run_script(script, args=args, capture=True)
        # 子进程已实时输出日志, 这里仅补摘要
        success = (code == 0)
        self.results["discord_video_mark"] = {"success": success, "code": code}
        if success:
            self.log("Discord 自荐标记完成", "OK")
        else:
            self.log(f"Discord 自荐标记失败 (code={code}): {(stderr or stdout)[:150]}", "WARN")
        return success

    def stage_curated_cn_mark(self, dry_run: bool = False) -> bool:
        """阶段 2.1b: 中文精选表高优先级标记 (v5.3.5)

        读取中文精选表的 BV/短链/标题, 匹配 B站表并将
        人工初筛 为空的记录标记为 高优先级。失败不阻断主流程。
        """
        self.log("=" * 50)
        self.log(f"阶段 2.1b: 中文精选高优标记 (v5.3.5){' [dry-run]' if dry_run else ''}")
        self.log("=" * 50)

        script = os.path.join(SCRIPT_DIR, "curated_cn_mark.py")
        if not os.path.exists(script):
            self.log(f"脚本不存在: {script}", "ERROR")
            self.results["curated_cn_mark"] = {"success": False, "error": "script not found"}
            return False

        args = ["--dry-run"] if dry_run else []
        code, stdout, stderr = self.run_script(script, args=args, capture=True)
        success = (code == 0)
        self.results["curated_cn_mark"] = {"success": success, "code": code}
        if success:
            self.log("中文精选高优标记完成", "OK")
        else:
            self.log(f"中文精选高优标记失败 (code={code}): {(stderr or stdout)[:150]}", "WARN")
        return success

    # ============================================================
    # v5.4.0: 来源视频 (Discord 自荐 / 中文精选) 优先级管理
    #   架构: 来源视频不再前置写 High Priority, 以普通身份走完整流程,
    #   AI 审核完成后才翻转 Human Prescreen 字段; 最终 AI 完成数 < 8 时
    #   复活被 ScriptDedup 淘汰的来源视频并补跑 AI (凑满 8 条)
    # ============================================================

    @staticmethod
    def _is_ai_done(result: Dict) -> bool:
        """判定单条结果是否真正完成了 AI 审核+评分 (无字幕的 WARN 不算)"""
        return bool(
            result.get("success")
            and result.get("has_subtitle")
            and str(result.get("censor_result", "") or "").strip()
        )

    def _flip_source_priority(self, feishu, results: List[Dict]) -> int:
        """v5.4.0: AI 审核完成后, 将来源视频的 Human Prescreen 翻转为 High Priority

        条件: 本轮 AI 审核真正完成 (_is_ai_done) 且在来源注册表中
        (source_registry.json, Discord 自荐/中文精选)。
        返回翻转成功的条数。
        """
        from source_registry import is_source_video
        # v5.4.1 修复: 表 ID 此前只在其它函数内局部导入, 此处直接引用触发 NameError,
        #   导致 B站翻转整段被 except 吞掉 (日志: name 'BILI_TABLE_ID' is not defined)
        from feishu_sync import BILI_TABLE_ID, YT_TABLE_ID

        if not results:
            return 0

        # 建立 platform -> (table_id, vid->(record_id, 当前人工初筛), 拒绝值) 索引
        # v5.4.4: 索引里额外带上"当前 Human Prescreen", 用于跳过已被去重淘汰的记录
        indexes: Dict[str, tuple] = {}
        try:
            for platform, table_id, id_field, human_field, reject_value in (
                ("bilibili", BILI_TABLE_ID, BILI_F_BVID, BILI_F_HUMAN_PRESCREEN, BILI_HUMAN_REJECTED),
                ("youtube", YT_TABLE_ID, YT_F_VIDEO_ID, YT_F_HUMAN_PRESCREEN, YT_HUMAN_REJECTED),
            ):
                idx: Dict[str, tuple] = {}
                for rec in feishu.get_records(table_id):
                    fields = rec.get("fields", {})
                    vid = str(fields.get(id_field, "") or "").strip()
                    if not vid:
                        continue
                    cur = fields.get(human_field, "")
                    if isinstance(cur, list):
                        cur = cur[0] if cur else ""
                    idx[vid] = (str(rec.get("record_id", "") or ""), str(cur or "").strip())
                indexes[platform] = (table_id, idx, reject_value)
        except Exception as e:
            self.log(f"来源优先级翻转失败 (建索引异常): {e}", "ERROR")
            return 0

        flipped = 0
        for r in results:
            if not self._is_ai_done(r):
                continue
            platform = str(r.get("platform") or "").lower()
            vid = str(r.get("video_id") or "").strip()
            if not vid and r.get("input"):
                try:
                    from platforms.video_parser import parse_video_url
                    vid, p, _ = parse_video_url(str(r["input"]))
                    platform = platform or str(p or "").lower()
                except Exception:
                    continue
            if not vid or platform not in indexes:
                continue
            if not is_source_video(platform, vid):
                continue

            table_id, idx, reject_value = indexes[platform]
            rec_info = idx.get(vid)
            if not rec_info:
                self.log(f"  [FLIP-WARN] {vid} 在注册表中但未找到飞书记录", "WARN")
                continue
            record_id, cur_human = rec_info
            # v5.4.4 修复: 来源翻转不得覆盖"已被去重淘汰"的记录 ——
            #   否则一条被 ScriptDedup 判为拒绝的视频会在 AI 跑完后被翻回入选,
            #   绕过语言分组去重 (实测: B站中文组 10 条 > 8, 频道C 占 2 条)。
            if cur_human == reject_value:
                self.log(f"  [FLIP-SKIP] {vid} 当前为 {cur_human} (已被去重淘汰), 跳过翻转", "WARN")
                continue
            field = BILI_F_HUMAN_PRESCREEN if platform == "bilibili" else YT_F_HUMAN_PRESCREEN
            value = BILI_HUMAN_HIGH if platform == "bilibili" else YT_HUMAN_HIGH
            if feishu.update_record(table_id, record_id, {field: value}):
                flipped += 1
                self.log(f"  [FLIP] {vid} -> {value} (来源推荐视频, AI 审核已完成)", "OK")
            else:
                self.log(f"  [FLIP-FAIL] {vid} 写入失败", "WARN")
        return flipped

    def _resurrect_source_videos(self, feishu, ai_done: int) -> List[Dict]:
        """v5.4.0: 最终 AI 完成数 < 8 时, 复活被 ScriptDedup 淘汰的来源视频并补跑 AI

        用户规则: 来源视频参与去重且不豁免, 但最终入选 < 8 时豁免凑总数;
        复活后必须补跑 AI 审核 (通过后才翻转为 High Priority)。
        候选范围: Human Prescreen = Rejected/拒绝 + 来源注册表命中 + 当周写入。
        排序: D1+D2 得分降序, 取 (8 - ai_done) 条。
        """
        if ai_done >= TARGET_VIDEO_COUNT:
            return []

        from source_registry import is_source_video
        # v5.4.1 修复: 同 _flip_source_priority, 表 ID 需在此处显式导入
        from feishu_sync import BILI_TABLE_ID, YT_TABLE_ID

        self.log(f"本轮 AI 审核完成 {ai_done} 个 (< {TARGET_VIDEO_COUNT}), 查找可复活的来源视频...")

        # 当周时间过滤 (与 get_videos_for_human_prescreen 同口径)
        import datetime as _dt
        if self._days_filter > 0:
            today_start = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff_ms = int((today_start - _dt.timedelta(days=self._days_filter - 1)).timestamp() * 1000)
        else:
            cutoff_ms = 0

        def _norm_select(val) -> str:
            if isinstance(val, list) and val:
                return str(val[0]).strip()
            return str(val or "").strip()

        candidates: List[Dict] = []
        for platform, table_id, id_field, title_field, human_field, reject_val, d1_f, d2_f, created_f in (
            ("bilibili", BILI_TABLE_ID, BILI_F_BVID, BILI_F_TITLE,
             BILI_F_HUMAN_PRESCREEN, BILI_HUMAN_REJECTED, BILI_F_D1, BILI_F_D2, BILI_F_CREATED_AT),
            ("youtube", YT_TABLE_ID, YT_F_VIDEO_ID, YT_F_TITLE,
             YT_F_HUMAN_PRESCREEN, YT_HUMAN_REJECTED, YT_F_D1, YT_F_D2, YT_F_CREATED_AT),
        ):
            try:
                records = feishu.get_records(table_id)
            except Exception as e:
                self.log(f"[{platform}] 读取记录失败, 跳过复活候选: {e}", "WARN")
                continue
            for rec in records:
                fields = rec.get("fields", {})
                if _norm_select(fields.get(human_field)) != reject_val:
                    continue  # 只复活被淘汰的 (机器初筛拒绝的不复活)
                if cutoff_ms > 0:
                    ct = fields.get(created_f, 0)
                    ct_ms = ct if isinstance(ct, (int, float)) and ct and ct > 0 else 0
                    if ct_ms and ct_ms < cutoff_ms:
                        continue  # 只看当周写入
                vid = str(fields.get(id_field, "") or "").strip()
                if not vid or not is_source_video(platform, vid):
                    continue
                try:
                    d1 = float(fields.get(d1_f, 0) or 0)
                    d2 = float(fields.get(d2_f, 0) or 0)
                except (TypeError, ValueError):
                    d1 = d2 = 0.0
                candidates.append({
                    "platform": platform,
                    "video_id": vid,
                    "url": self._build_video_url(platform, vid),
                    "rank": d1 + d2,
                    "title": str(fields.get(title_field, "") or "")[:40],
                })

        if not candidates:
            self.log("没有可复活的来源视频 (被去重淘汰的 Discord 自荐/中文精选)", "INFO")
            return []

        candidates.sort(key=lambda c: c["rank"], reverse=True)
        need = TARGET_VIDEO_COUNT - ai_done
        take = candidates[:need]
        self.log(f"复活候选共 {len(candidates)} 个, 取前 {len(take)} 个补跑 AI:")
        for c in take:
            self.log(f"  - [{c['platform']}] {c['title']} ({c['url'][:60]})")

        from main import run_fetch_phase_videos, resolve_ai_futures, sync_results_to_feishu

        urls = [c["url"] for c in take]
        results = run_fetch_phase_videos(urls, "来源视频复活补跑AI", interval=0.5)
        resolve_ai_futures(results)

        ok_results = [r for r in results if r.get("success")]
        if ok_results:
            synced = sync_results_to_feishu(ok_results)
            self.log(f"复活补跑: 成功 {len(ok_results)} | 飞书同步 {synced} 条", "OK")
        else:
            self.log("复活补跑: 无成功结果", "WARN")

        # 复活成功的来源视频同样翻转 High Priority
        flipped = self._flip_source_priority(feishu, results)
        self.log(f"复活补跑翻转优先级: {flipped} 个", "OK")
        return results

    def run_full(self) -> bool:
        """
        运行完整工作流 (v3.5.0)
        
        新增: 采集+初筛完成后显示交互菜单，支持撤销+重跑
        """
        self.log("=" * 60)
        self.log("开始执行完整工作流 (v3.5.0)")
        self.log("=" * 60)

        # v3.5.0: 预检代理状态
        self.log("[预检] 检测网络环境...")
        self._diagnose_proxy()

        # === 阶段1: 采集 ===
        fetch_ok = self.stage_fetch()

        # === 阶段2: 初筛 (两个平台) ===
        bili_prescreen_ok = self.stage_prescreen("bilibili")
        yt_prescreen_ok = self.stage_prescreen("youtube")

        # === v3.5.0: 交互菜单 ===
        if not self._auto_mode:
            menu_choice = self._show_post_fetch_menu()
            
            while menu_choice not in ("0", "1", ""):
                if menu_choice == "2":
                    # _run_bili_only.py v2.0 已内置初筛，无需重复调用 stage_prescreen
                    self._rollback_and_refetch_platform("bilibili")
                elif menu_choice == "3":
                    # YouTube 重走完整 fetcher（不含初筛），需手动补初筛
                    self._rollback_and_refetch_platform("youtube")
                    self.stage_prescreen("youtube")
                elif menu_choice == "4":
                    # B站脚本已含初筛，YouTube 需补初筛
                    self._rollback_and_refetch_platform("bilibili")
                    self._rollback_and_refetch_platform("youtube")
                    self.stage_prescreen("youtube")
                elif menu_choice == "5":
                    self._show_fetch_stats()
                else:
                    self.log(f"无效选项: '{menu_choice}'，请重新选择", "WARN")
                
                menu_choice = self._show_post_fetch_menu()
            
            if menu_choice == "0":
                self.log("用户选择退出", "INFO")
                return False

        # === 阶段3-5: 后续流程 ===
        stages = [
            ("人工初筛", lambda: self.stage_human_prescreen()),
            ("字幕+评分", lambda: self.stage_subtitle()),
            ("飞书确认", lambda: self.stage_feishu_sync()),
        ]

        all_success = True
        for name, stage_func in stages:
            success = stage_func()
            if not success:
                self.log(f"阶段失败: {name}", "WARN")
                all_success = False

        elapsed = (datetime.now() - self.start_time).total_seconds()

        self.log("=" * 60)
        self.log("工作流执行完成")
        self.log(f"总耗时: {int(elapsed // 60)}分 {int(elapsed % 60)}秒")

        for stage, result in self.results.items():
            status = "OK" if result.get("success") else "FAIL"
            self.log(f"  {stage}: {status}", status)

        self.log("=" * 60)

        return all_success

    def run_from_human_prescreen(self) -> bool:
        """从人工初筛阶段开始运行 (v5.2.0 - 仅当周视频 + 分离流水线)

        v5.2.0 核心规则 (全局约束, 在整个流程中生效):
          ⚠️ 只处理【当周写入飞书】的视频 (按 CREATED_AT 过滤, 默认 days=7)
          ⚠️ 不对全表历史数据进行同 UP 去重
          ⚠️ ScriptPrescreen 只标记 Prescreen=Passed + Human=空 + 当周 的记录

        v5.0.0 重大变更:
        - 自动调用 stage_script_prescreen() 替代人工初筛
        - 取消人工等待, 直接进入 AI 审核流水线
        - B站: 一次性跑完 → 生成报告 → 写入飞书 (不重试)
        - YouTube: 第1轮(不重试) → 生成报告 → 写入飞书 → 多轮重试循环
        """
        self.log("=" * 60)
        self.log(f"从脚本初筛阶段开始执行 (v5.0.0 自动初筛版) [平台: {self._target_platform}, 时间过滤: {self._days_filter}天]")
        self.log("=" * 60)

        # v5.0.0: 先自动执行脚本去重, 替代人工初筛
        self.log("\n[v5.0.0] 自动执行脚本初筛 (替代人工初筛)...")
        script_ok = self.stage_script_prescreen(platform=self._target_platform)
        if not script_ok:
            self.log("脚本初筛执行有错误, 仍继续后续流程", "WARN")

        # ============================================================
        # 阶段3: 收集优先级视频
        # ============================================================
        self.log("=" * 50)
        self.log("阶段3: 收集优先级视频")
        self.log("=" * 50)

        priority_videos = self._collect_priority_videos(days_filter=self._days_filter)
        priority_count = len(priority_videos)

        # v4.0: 平台过滤
        if self._target_platform != "all":
            original_count = priority_count
            priority_videos = [
                v for v in priority_videos
                if v.get("platform", "").lower() == self._target_platform.lower()
            ]
            filtered_count = len(priority_videos)
            self.log(f"平台过滤: {self._target_platform} | {original_count} -> {filtered_count} 个")

        if priority_count == 0 or len(priority_videos) == 0:
            self.log("没有找到优先级视频，退出", "ERROR")
            return False

        # 拆分为两个平台
        bili_videos = [v for v in priority_videos if v.get("platform", "").lower() == "bilibili"]
        yt_videos = [v for v in priority_videos if v.get("platform", "").lower() == "youtube"]

        # v5.2.1: YouTube 跳过已有 AI Review 的视频 (避免重复处理)
        if yt_videos:
            # 初始化飞书连接
            from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID
            from feishu_fields import YT_F_AI_REVIEW, YT_F_VIDEO_ID as YT_F_VIDEO_ID_CONST
            _feishu = FeishuSync()
            _feishu.set_bitable_token(BILI_BITABLE_TOKEN)
            _yt_records = _feishu.get_records(YT_TABLE_ID)
            ai_done_ids = set()
            for r in _yt_records:
                f = r.get("fields", {})
                ai = str(f.get(YT_F_AI_REVIEW, "") or "").strip()
                if ai:
                    vid = str(f.get(YT_F_VIDEO_ID_CONST, "")).strip()
                    if vid:
                        ai_done_ids.add(vid)
            yt_before = len(yt_videos)
            yt_videos = [v for v in yt_videos if v["video_id"] not in ai_done_ids]
            self.log(f"[YouTube] 跳过已有AI Review: {yt_before - len(yt_videos)} | 待处理: {len(yt_videos)}")

        self.log(f"B站优先级视频: {len(bili_videos)} 个 | YouTube: {len(yt_videos)} 个")

        # 初始化飞书连接（两个平台共用）
        from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID, YT_TABLE_ID
        feishu = FeishuSync()
        feishu.set_bitable_token(BILI_BITABLE_TOKEN)

        # 导入 main.py 模块级函数
        from main import (
            fetch_and_process_single,
            run_fetch_phase_videos,
            resolve_ai_futures,
            sync_results_to_feishu,
        )
        from reporter import write_report, print_summary

        # 报告保存目录
        report_dir = os.path.join(PARENT_DIR, "data", "reports")
        os.makedirs(report_dir, exist_ok=True)

        # v5.4.0: 汇总所有平台的 AI 结果 (含重试轮), 供末尾来源优先级翻转 + <8 复活补跑
        all_ai_results: List[Dict] = []

        all_success = True

        # ============================================================
        # Bilibili 流水线 — 一次性跑完，不重试 (v4.5.0)
        # ============================================================
        if bili_videos:
            self.log("=" * 50)
            self.log(f"[B站流水线] 开始处理 {len(bili_videos)} 个视频 (一次性，不重试)")
            self.log("=" * 50)

            bili_urls = [v["url"] for v in bili_videos]
            bili_results = run_fetch_phase_videos(bili_urls, "B站字幕获取", interval=0.5)

            # 解析 AI 结果
            self.log("[B站] 等待AI审核+评分任务完成...")
            resolve_ai_futures(bili_results)

            # 统计
            bili_success = [r for r in bili_results if r.get("success")]
            bili_no_sub = [r for r in bili_results if r.get("success") and not r.get("has_subtitle")]
            bili_fail = [r for r in bili_results if not r.get("success")]

            self.log(f"[B站] 成功: {len(bili_success)} | 无字幕: {len(bili_no_sub)} | 失败: {len(bili_fail)}")

            # 生成报告
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            bili_report_path = os.path.join(report_dir, f"bili_{timestamp}.md")
            write_report(bili_results, bili_report_path)
            self.log(f"[B站] 报告已保存: {bili_report_path}", "OK")

            # 写入飞书
            self.log("[B站] 写入飞书...")
            synced = sync_results_to_feishu(bili_results)
            self.log(f"[B站] 飞书同步完成: {synced} 条", "OK")

            print_summary(bili_results)
            self.results["bilibili"] = {"success": True, "total": len(bili_results), "synced": synced}
            all_ai_results.extend(bili_results)  # v5.4.0

        # ============================================================
        # YouTube 流水线 — 第1轮 + 多轮重试 (v4.5.0)
        # ============================================================
        if yt_videos:
            self.log("=" * 50)
            self.log(f"[YouTube流水线] 开始处理 {len(yt_videos)} 个视频")
            self.log(f"  策略: 第1轮不重试 → 报告 → 飞书 → 多轮重试(需用户确认)")
            self.log("=" * 50)

            # 预加载飞书 YouTube 记录索引
            yt_records = feishu.get_records(YT_TABLE_ID)
            yt_index: Dict[str, str] = {}
            for rec in yt_records:
                fields = rec.get("fields", {})
                vid = str(fields.get(YT_F_VIDEO_ID, "")).strip()
                if vid:
                    yt_index[vid] = rec.get("record_id", "")

            yt_urls = [v["url"] for v in yt_videos]
            round_num = 1

            # ========== 第1轮: 不重试 ==========
            self.log(f"[YouTube] 第{round_num}轮: 字幕获取 (不重试)...")
            yt_results = run_fetch_phase_videos(yt_urls, f"YouTube第{round_num}轮", interval=0.3)

            # 解析 AI 结果
            self.log(f"[YouTube] 第{round_num}轮: 等待AI审核+评分任务...")
            resolve_ai_futures(yt_results)
            all_ai_results.extend(yt_results)  # v5.4.0

            # 分离成功/失败
            yt_success = [r for r in yt_results if r.get("success")]
            yt_failed = [r for r in yt_results if not r.get("success")]

            self.log(f"[YouTube] 第{round_num}轮: 成功 {len(yt_success)} | 失败 {len(yt_failed)}")

            # 生成第1轮报告
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            yt_report_path = os.path.join(report_dir, f"yt_round{round_num}_{timestamp}.md")
            write_report(yt_results, yt_report_path)
            self.log(f"[YouTube] 第{round_num}轮报告已保存: {yt_report_path}", "OK")

            # 写入飞书（仅成功视频）
            self.log(f"[YouTube] 第{round_num}轮: 写入飞书(成功视频)...")
            synced = sync_results_to_feishu(yt_success)
            self.log(f"[YouTube] 第{round_num}轮飞书同步: {synced} 条", "OK")

            # 同时标记失败视频的飞书状态为 failed
            self._sync_youtube_failed_status(feishu, yt_failed, yt_index, YT_TABLE_ID)

            print_summary(yt_results)

            # ========== 第2轮起: 多轮重试循环 ==========
            failed_urls = [r["input"] for r in yt_failed]

            # v5.3.4: 智能重试引擎提升到 while 循环外创建、跨轮复用 ——
            # 原实现在每轮循环内 new SmartRetryEngine()，导致 IP 健康度滑动窗口每轮重置；
            # 现在窗口跨轮累积，连续多轮失败会正确触发整批暂停/人工介入提示
            engine = None
            coordinator = None
            video_attempts: Dict[str, int] = {}  # v5.3.4: url -> 单视频已尝试次数（跨轮累计）
            if _SMART_RETRY_AVAILABLE:
                engine = SmartRetryEngine(budget_seconds=1800.0)
                coordinator = TwoPhaseRetryCoordinator(engine=engine)

            while failed_urls:
                round_num += 1
                retry_round = round_num - 1  # v5.3.4: 已执行的重试轮数（初始轮不计入）

                # v5.3.4: 统一终止条件（与 stage_retry_youtube_failed 一致）:
                #   retry_round > MAX_RETRY_ROUNDS 时终止
                if retry_round > MAX_RETRY_ROUNDS:
                    self.log(f"  [AUTO] 已达最大重试轮数 ({MAX_RETRY_ROUNDS} 轮重试)，剩余 {len(failed_urls)} 个失败视频终止", "WARN")
                    self.log(f"  这些视频很可能是视频级问题 (no_subtitle/region_locked)，无法通过重试解决", "TIP")
                    break

                print(f"\n{'='*60}")
                print(f"  [YouTube] 第{round_num}轮重试 — {len(failed_urls)} 个失败视频")
                print(f"{'='*60}")
                print(f"\n  [重要提醒] 请在继续前确认:")
                print(f"    1. YouTube Cookies 是否是最新的? (youtube_cookies.json)")
                print(f"    2. 代理节点是否已更换? (避免同一IP被持续封锁)")
                print(f"    3. 如果当前IP已被封锁，请立即切换代理节点")
                print(f"\n  已失败视频:")
                for r in yt_failed[-10:]:  # 最多显示最近10个
                    err_short = str(r.get("error", ""))[:60]
                    print(f"    - {r['input'][:60]}... ({err_short})")

                try:
                    user_input = input(f"\n  按 Enter 开始第{round_num}轮重试 (输入 q 退出): ").strip().lower()
                except EOFError:
                    # 非交互模式自动继续
                    user_input = ""

                if user_input == "q":
                    self.log(f"[YouTube] 用户选择退出重试，剩余 {len(failed_urls)} 个失败视频", "WARN")
                    break

                # 应用智能等待（v5.3.4: engine 已在循环外创建并跨轮复用）
                if engine and engine.should_pause_batch():
                    print(f"\n  [PAUSE] IP健康度过低 ({engine.ip_health.success_rate:.1%})，建议手动暂停")
                    if self._auto_mode:
                        self.log(f"  [AUTO] 自动模式: 忽略 IP 健康度警告，继续重试", "WARN")
                    else:
                        print(f"  请确认已切换代理节点后按 Enter 继续...")
                        try:
                            input()
                        except EOFError:
                            pass

                # 重试本轮的失败视频
                if engine and coordinator:
                    retry_results = self._retry_youtube_smart(
                        failed_urls, yt_urls, round_num, engine, coordinator, video_attempts
                    )
                else:
                    # 退化路径：直接用主函数重试
                    retry_results = run_fetch_phase_videos(
                        failed_urls, f"YouTube第{round_num}轮重试", interval=1.0
                    )

                # 解析 AI 结果
                resolve_ai_futures(retry_results)
                all_ai_results.extend(retry_results)  # v5.4.0

                # 分离本轮成功/失败
                new_success = [r for r in retry_results if r.get("success")]
                new_failed = [r for r in retry_results if not r.get("success")]

                self.log(f"[YouTube] 第{round_num}轮重试: 成功 {len(new_success)} | 失败 {len(new_failed)}")

                # 生成第N轮报告
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                yt_report_path = os.path.join(report_dir, f"yt_round{round_num}_{timestamp}.md")
                write_report(retry_results, yt_report_path)
                self.log(f"[YouTube] 第{round_num}轮报告已保存: {yt_report_path}", "OK")

                # 写入飞书（本轮新成功视频）
                if new_success:
                    self.log(f"[YouTube] 第{round_num}轮: 写入飞书(新成功视频)...")
                    synced = sync_results_to_feishu(new_success)
                    self.log(f"[YouTube] 第{round_num}轮飞书同步: {synced} 条", "OK")

                # 更新失败列表
                failed_urls = [r["input"] for r in new_failed]
                yt_failed = new_failed

                if not failed_urls:
                    self.log(f"[YouTube] 全部成功! 重试完成", "OK")
                    break

            # YouTube 最终统计
            all_yt_success = [r for r in yt_results if r.get("success")]
            self.results["youtube"] = {
                "success": len(failed_urls) == 0,
                "total": len(yt_videos),
                "final_success": len(yt_success) + sum(
                    1 for r in retry_results if r.get("success")
                ) if 'retry_results' in dir() else len(all_yt_success),
                "still_failed": len(failed_urls),
            }

        # ============================================================
        # v5.4.0: 来源视频优先级翻转 + 最终入选 <8 时复活补跑
        #   仅对 AI 审核真正完成的来源视频翻转 High Priority;
        #   AI 完成数 < 8 时复活被 ScriptDedup 淘汰的来源视频补跑 AI。
        #   失败不阻断主流程 (结果已同步, 不因此判整个工作流 FAIL)
        # ============================================================
        try:
            ai_done = sum(1 for r in all_ai_results if self._is_ai_done(r))
            flipped = self._flip_source_priority(feishu, all_ai_results)
            self.log(f"来源视频优先级翻转: {flipped} 个 (本轮 AI 审核完成 {ai_done} 个)", "OK")

            if ai_done < TARGET_VIDEO_COUNT:
                resurrect_results = self._resurrect_source_videos(feishu, ai_done)
                all_ai_results.extend(resurrect_results)
            self.results["source_flip_resurrect"] = {"success": True, "flipped": flipped, "ai_done": ai_done}
        except Exception as e:
            self.log(f"来源视频翻转/复活阶段异常 (不阻断): {type(e).__name__}: {e}", "WARN")
            self.results["source_flip_resurrect"] = {"success": False, "error": str(e)}

        # ============================================================
        # v5.4.4: 最终一致性收敛 (兜底校验)
        #   按「语言_Language(AI 判定)」+ 频道 复查: 某语言通过 AI 数 > 8 时,
        #   该语言内每个频道只保留总分最高的 1 条。
        #   必须放在"来源翻转/复活"之后 —— 翻转可能把已淘汰视频翻回入选,
        #   这一步负责收口。失败不阻断主流程。
        # ============================================================
        try:
            from prescreen import enforce_final_language_dedup
            stats = enforce_final_language_dedup(feishu=feishu, days_filter=self._days_filter)
            self.log(f"语言×频道一致性收敛: 纳入统计 {stats.get('checked')} 条, "
                     f"收敛 {stats.get('fixed')} 条, 明细 {stats.get('detail')}", "OK")
            self.results["lang_author_dedup"] = {"success": True, **stats}
        except Exception as e:
            self.log(f"语言×频道一致性收敛异常 (不阻断): {type(e).__name__}: {e}", "WARN")
            self.results["lang_author_dedup"] = {"success": False, "error": str(e)}

        # ============================================================
        # 最终总结
        # ============================================================
        self.log("=" * 60)
        self.log("工作流执行完成 (v4.5.0)")
        for stage, result in self.results.items():
            status = "OK" if result.get("success") else "FAIL"
            self.log(f"  {stage}: {status} (详情: {result})", status)
        self.log("=" * 60)

        # 清理临时文件
        if os.path.exists(VIDEO_URLS_FILE):
            try:
                with open(VIDEO_URLS_FILE, "w", encoding="utf-8") as f:
                    f.write("")
                self.log("已自动清空 video_urls.txt", "OK")
            except Exception:
                pass

        if os.path.exists(PRIORITY_MAP_FILE):
            try:
                os.remove(PRIORITY_MAP_FILE)
                self.log("已删除优先级映射文件", "OK")
            except Exception:
                pass

        return True

    def _sync_youtube_failed_status(self, feishu, failed_results, yt_index, yt_table_id):
        """将失败视频的飞书状态标记 (v5.3.4)

        v5.3.4 优化: 区分两种失败状态 ——
          - 视频级问题 (no_subtitle / region_locked / video_unavailable):
            重试无法解决 → 标记 failed (永久失败, 不再重试)
          - 可重试问题 (ip_blocked / rate_limit / transient_network / unknown):
            换代理/换时间后可恢复 → 标记 pending (保持"待处理", 供下次单独重试拉取)
        无法分类的失败（无 error_category 字段，如普通抓取路径）维持旧行为 failed。
        """
        permanent_categories = frozenset({
            ErrorCategory.NO_SUBTITLE,
            ErrorCategory.REGION_LOCKED,
            ErrorCategory.VIDEO_UNAVAILABLE,
        })
        retryable_categories = frozenset({
            ErrorCategory.IP_BLOCKED,
            ErrorCategory.RATE_LIMIT,
            ErrorCategory.TRANSIENT_NETWORK,
            ErrorCategory.FEISHU_THROTTLE,
            ErrorCategory.UNKNOWN,
        })
        for r in failed_results:
            video_id = r.get("video_id", "")
            if not video_id:
                continue
            record_id = yt_index.get(video_id, "")
            if not record_id:
                continue
            cat = str(r.get("error_category", "") or "")
            if cat in permanent_categories:
                status = YT_SUBTITLE_FAILED
            elif cat in retryable_categories:
                status = YT_SUBTITLE_PENDING
            else:
                status = YT_SUBTITLE_FAILED  # 无法分类: 维持旧行为
            try:
                feishu.update_record(yt_table_id, record_id, {
                    YT_F_SUBTITLE_STATUS: status
                })
            except Exception:
                pass

    def _retry_youtube_smart(self, failed_urls, all_urls, round_num, engine, coordinator, video_attempts=None):
        """使用 SmartRetryEngine 智能重试 YouTube 失败视频 (v5.3.4)

        v5.3.4 优化:
        ① 探测成功直接复用字幕结果 (fetch_and_process_single probe_data)，
           不再"先探测又 fetch 一次"双重请求；探测失败则用错误分类直接决策
        ② 每次请求都 record_result 到引擎，IP 健康度滑动窗口跨轮累积
        ③ attempt_count 改为"单视频已尝试次数"（跨轮累计），而非轮次号
        ⑤ 失败结果附带 error_category，供飞书状态区分 no_subtitle / ip_blocked
        """
        from platforms.video_parser import parse_video_url
        from platforms.youtube_client import (
            _fetch_youtube_subtitle_via_transcript_api,
            _on_youtube_success,
        )
        from main import fetch_and_process_single, get_preferred_lang_for

        video_attempts = video_attempts if video_attempts is not None else {}
        retry_results = []
        for i, url in enumerate(failed_urls, 1):
            print(f"\n--- [YouTube 重试 {round_num}-{i}/{len(failed_urls)}] ---")

            if engine.is_budget_exceeded():
                print(f"  [STOP] 时间预算超出，剩余任务延后")
                break

            video_id, platform, _ = parse_video_url(url)
            if not video_id:
                continue

            # ③ 单视频已尝试次数（跨轮累计），替代旧的"轮次号"
            attempts = video_attempts.get(url, 0) + 1
            video_attempts[url] = attempts

            # 探测字幕（仅一次请求，内部自带 3 次 list() 重试 + fetch fallback）
            # v5.3.7: 探测即带上声明语言, 命中优先字幕轨, 避免重试路径取错语言轨
            data, err, error_category = _fetch_youtube_subtitle_via_transcript_api(
                video_id, preferred_lang=get_preferred_lang_for(video_id))

            # ① 探测成功：复用字幕结果直接进入评分/AI，不再二次请求
            if data and data.get("has_subtitle") and data.get("subtitle_text", "").strip():
                _on_youtube_success()  # 维持 YouTube 速率限制器状态
                print(f"  [OK] 探测即拿到字幕，复用结果: {url[:50]}...")
                result = fetch_and_process_single(url, probe_data=data)
                retry_results.append(result)
                engine.record_result("success", success=True)
                print(f"  [{'OK' if result.get('success') else 'FAIL'}] {url[:50]}...")
                continue

            # 决策（attempt_count = 单视频已尝试次数）
            category = error_category or ErrorCategory.UNKNOWN
            decision = engine.decide(
                category,
                context={"attempt_count": attempts, "url": url}
            )
            engine.record_result(category, success=False)

            if not decision.should_retry:
                error_msg = err or decision.reason
                print(f"  [SKIP] {decision.reason}")
                retry_results.append({
                    "input": url, "success": False,
                    "error": error_msg, "platform": "youtube",
                    "video_id": video_id,        # v5.4.3: 缺此字段会导致飞书状态标记被静默跳过
                    "error_category": category,  # v5.3.4: 供飞书状态区分
                })
                continue

            if decision.wait_seconds > 0:
                print(f"  [WAIT] {decision.wait_seconds:.0f}秒后重试... ({decision.reason})")
                import time
                time.sleep(decision.wait_seconds)

            # 重试（完整流程：fetch → 评分 → AI）
            result = fetch_and_process_single(url)
            retry_results.append(result)
            if result.get("success"):
                engine.record_result("success", success=True)
            else:
                # 重试结果无 error_category 时用探测分类兜底
                retry_cat = str(result.get("error_category", "") or "") or category
                engine.record_result(retry_cat, success=False)
            print(f"  [{('OK' if result.get('success') else 'FAIL')}] {url[:50]}...")

        return retry_results

    def stage_retry_youtube_failed(self, urls: str = None) -> bool:
        """单独重试 YouTube 飞书中非成功的视频 (v5.2.4)

        从飞书 YouTube 表读取:
          - Human Prescreen = "Selected" (仅 Selected，不含 Priority)
          - Date Created = 今天 (ms 时间戳 >= 今日 00:00:00 UTC+8)
          - AI Review Report 为空 (尚未完成 AI 审核)
          - Subtitle Status != 'success' 且 != '获取成功' (未成功的)
        直接进入多轮重试循环（不重新收集优先级视频）。

        参数:
            urls: 逗号分隔的 URL 列表（备用模式，绕过飞书读取）
        """
        self.log("=" * 60)
        self.log("YouTube 未成功视频单独重试模式 (v5.2.4)")
        self.log("  过滤: Human Prescreen = Selected (仅 Selected)")
        self.log("  过滤: Date Created = 今天")
        self.log("  过滤: AI Review Report 为空")
        self.log("  排除: Subtitle Status ∈ {success, 获取成功}")
        self.log("=" * 60)

        if urls:
            # 备用模式：直接使用命令行传入的 URL
            failed_urls = [u.strip() for u in urls.split(",") if u.strip()]
            yt_index: dict = {}
            self.log(f"[备用模式] 使用命令行传入的 {len(failed_urls)} 个 URL")
            # 仍需飞书实例用于写回结果
            from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID
            feishu = FeishuSync()
            feishu.set_bitable_token(BILI_BITABLE_TOKEN)
            try:
                records = feishu.get_records(YT_TABLE_ID)
                for rec in records:
                    fields = rec.get("fields", {})
                    vid = str(fields.get(YT_F_VIDEO_ID, "")).strip()
                    if vid:
                        yt_index[vid] = rec.get("record_id", "")
            except Exception:
                pass  # 无法获取索引时跳过飞书写回
        else:
            # 正常模式：从飞书读取
            from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID
            feishu = FeishuSync()
            feishu.set_bitable_token(BILI_BITABLE_TOKEN)

            self.log("[1/2] 从飞书读取 YouTube 未成功视频 (Selected + 今天 + AI 报告为空)...")
            records = feishu.get_records(YT_TABLE_ID)

            # v5.2.4: 限定为"本轮需要处理"的视频 (仅 Selected) + 今天创建 + AI 报告为空
            PRIORITY_HUMAN_VALUES = {YT_HUMAN_SELECTED}  # 仅 Selected，不含 Priority
            SUCCESS_STATUS_VALUES = {YT_SUBTITLE_SUCCESS, "获取成功"}

            # 计算今天 00:00:00 的毫秒时间戳 (UTC+8)
            # v5.3.2: 仅补充 timedelta/timezone，datetime 使用模块级导入
            # (函数内 import datetime 会导致 datetime 成为局部变量，
            #  --urls 备用模式跳过此分支时第1354行 datetime.now() 报 UnboundLocalError)
            from datetime import timedelta, timezone
            tz_cn = timezone(timedelta(hours=8))
            now_cn = datetime.now(tz_cn)
            today_start_ms = int(datetime(now_cn.year, now_cn.month, now_cn.day, tzinfo=tz_cn).timestamp() * 1000)
            self.log(f"  今日起点 (UTC+8 00:00:00) ms: {today_start_ms}")

            failed_urls = []
            yt_index = {}
            stats = {
                "total": 0,
                "not_selected": 0,
                "not_today": 0,
                "ai_report_exists": 0,
                "already_success": 0,
                "to_retry": 0,
            }
            for rec in records:
                fields = rec.get("fields", {})
                vid = str(fields.get(YT_F_VIDEO_ID, "")).strip()
                if not vid:
                    continue
                stats["total"] += 1
                yt_index[vid] = rec.get("record_id", "")

                hp = str(fields.get(YT_F_HUMAN_PRESCREEN, "") or "").strip()
                status = str(fields.get(YT_F_SUBTITLE_STATUS, "") or "").strip()

                # v5.2.4: 必须为 Selected (不再含 High/Medium/Low Priority)
                if hp not in PRIORITY_HUMAN_VALUES:
                    stats["not_selected"] += 1
                    continue

                # v5.2.4: Date Created 必须是今天
                created_at_raw = fields.get(YT_F_CREATED_AT, 0)
                try:
                    created_at_ms = int(created_at_raw) if created_at_raw else 0
                except (TypeError, ValueError):
                    created_at_ms = 0
                if created_at_ms < today_start_ms:
                    stats["not_today"] += 1
                    continue

                # v5.2.4: AI Review Report 字段必须为空 (尚未完成 AI 审核)
                ai_report = fields.get(YT_F_AI_REVIEW_REPORT, "")
                if ai_report and str(ai_report).strip():
                    stats["ai_report_exists"] += 1
                    continue

                # 已成功的跳过
                if status in SUCCESS_STATUS_VALUES:
                    stats["already_success"] += 1
                    continue

                # 进入重试列表
                url = f"https://www.youtube.com/watch?v={vid}"
                failed_urls.append(url)
                stats["to_retry"] += 1

            self.log(f"  全表共 {stats['total']} 条")
            self.log(f"  非 Selected 跳过: {stats['not_selected']} 条")
            self.log(f"  非今天跳过: {stats['not_today']} 条")
            self.log(f"  AI 报告已存在跳过: {stats['ai_report_exists']} 条")
            self.log(f"  已成功跳过: {stats['already_success']} 条")
            self.log(f"  待重试: {stats['to_retry']} 条", "OK" if stats["to_retry"] > 0 else "WARN")

            if not failed_urls:
                self.log("没有找到需要重试的 YouTube 视频 (Selected + 今天 + AI 报告为空 已全部成功)", "OK")
                self.log("  提示: 先运行 human-prescreen 阶段才会标记 Selected 状态", "TIP")
                self.log("  备用: 使用 --urls 参数直接传入 URL 列表", "TIP")
                return True

        self.log(f"找到 {len(failed_urls)} 个失败视频:")
        for url in failed_urls:
            print(f"    - {url}")

        # 导入核心函数
        from main import fetch_and_process_single, run_fetch_phase_videos, resolve_ai_futures, sync_results_to_feishu
        from reporter import write_report, print_summary

        report_dir = os.path.join(PARENT_DIR, "data", "reports")
        os.makedirs(report_dir, exist_ok=True)

        # MAX_RETRY_ROUNDS 已在模块级定义 (v5.2.2)
        # v5.3.4: 与完整流程统一终止语义 —— 使用 retry_round（已执行重试轮数）:
        #   if retry_round > MAX_RETRY_ROUNDS: break
        #   本函数 round_num 从 1 开始即代表第 1 轮重试, retry_round = round_num

        round_num = 0
        retry_round = 0

        # v5.3.4: 智能重试引擎提升到 while 循环外创建、跨轮复用，
        # IP 健康度滑动窗口跨轮累积（原实现每轮重建引擎导致窗口重置）
        engine = None
        video_attempts: Dict[str, int] = {}  # v5.3.4: url -> 单视频已尝试次数（跨轮累计）
        if _SMART_RETRY_AVAILABLE:
            engine = SmartRetryEngine(budget_seconds=1800.0)

        while failed_urls:
            round_num += 1
            retry_round = round_num  # v5.3.4: 本函数 round_num 即重试轮数

            # v5.3.4: 统一终止条件（与完整流程一致）: retry_round > MAX_RETRY_ROUNDS
            if retry_round > MAX_RETRY_ROUNDS:
                self.log(f"已达最大重试轮数 ({MAX_RETRY_ROUNDS} 轮重试)，剩余 {len(failed_urls)} 个视频终止重试", "WARN")
                self.log(f"  这些视频很可能是视频级问题 (no_subtitle/region_locked/video_unavailable/AI失败)，无法通过重试解决", "TIP")
                self.log(f"  建议: 人工检查并标记为 Rejected", "TIP")
                break

            print(f"\n{'='*60}")
            print(f"  [重试] 第 {round_num}/{MAX_RETRY_ROUNDS} 轮 — {len(failed_urls)} 个失败视频")
            print(f"{'='*60}")

            # v5.2.2: 自动模式下跳过交互输入
            if self._auto_mode:
                self.log(f"  [AUTO] 自动模式: 直接进入第 {round_num}/{MAX_RETRY_ROUNDS} 轮重试", "INFO")
            else:
                print(f"\n  [重要提醒] 请在继续前确认:")
                print(f"    1. YouTube Cookies 是否是最新的? (youtube_cookies.json)")
                print(f"    2. 代理节点是否已更换?")
                print(f"")
                try:
                    user_input = input(f"  按 Enter 开始第 {round_num}/{MAX_RETRY_ROUNDS} 轮重试 (输入 q 退出): ").strip().lower()
                except EOFError:
                    user_input = ""
                if user_input == "q":
                    self.log(f"用户选择退出，剩余 {len(failed_urls)} 个失败视频", "WARN")
                    break

            # 智能等待（v5.3.4: engine 已在循环外创建并跨轮复用）
            if engine and engine.should_pause_batch():
                print(f"\n  [PAUSE] IP健康度过低，建议立即切换代理节点")
                try:
                    input("  切换后按 Enter 继续...")
                except EOFError:
                    pass

            if engine:
                coordinator = TwoPhaseRetryCoordinator(engine=engine)
                retry_results = self._retry_youtube_smart(failed_urls, failed_urls, round_num, engine, coordinator, video_attempts)
            else:
                retry_results = run_fetch_phase_videos(failed_urls, f"YouTube失败重试第{round_num}轮", interval=0.8)

            # 解析 AI
            resolve_ai_futures(retry_results)

            new_success = [r for r in retry_results if r.get("success")]
            new_failed = [r for r in retry_results if not r.get("success")]

            self.log(f"第 {round_num} 轮: 成功 {len(new_success)} | 仍失败 {len(new_failed)}")

            # 报告
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report_path = os.path.join(report_dir, f"yt_retry_round{round_num}_{timestamp}.md")
            write_report(retry_results, report_path)
            self.log(f"报告已保存: {report_path}", "OK")

            # 写飞书
            if new_success:
                synced = sync_results_to_feishu(new_success)
                self.log(f"飞书同步: {synced} 条", "OK")

            # 标记仍然失败的
            self._sync_youtube_failed_status(feishu, new_failed, yt_index, YT_TABLE_ID)

            failed_urls = [r["input"] for r in new_failed]

            if not failed_urls:
                self.log("全部重试成功!", "OK")
                break

        return True

    def show_status(self):
        """显示工作流状态"""
        print("\n" + "=" * 50)
        print("工作流状态")
        print("=" * 50)

        for stage, result in self.results.items():
            status = "OK" if result.get("success") else "FAIL"
            print(f"  {stage}: {status}")
            if result.get("passed") is not None:
                print(f"    通过: {result.get('passed')}/{result.get('total')}")

        if not self.results:
            print("  (无执行记录)")

        print("=" * 50)


def main():
    parser = argparse.ArgumentParser(
        description="工作流调度器 v4.0 (支持平台/阶段选择)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 全流程
  python workflow_orchestrator.py --stage human-prescreen
  python workflow_orchestrator.py --stage human-prescreen --platform all

  # 单平台
  python workflow_orchestrator.py --stage human-prescreen --platform youtube
  python workflow_orchestrator.py --stage human-prescreen --platform bilibili

  # 单阶段
  python workflow_orchestrator.py --stage fetch --platform all
  python workflow_orchestrator.py --stage feishu --platform all

  # v5.3.5 标记阶段 (可加 --dry-run 只看不写)
  python workflow_orchestrator.py --stage discord-video-mark --dry-run
  python workflow_orchestrator.py --stage curated-cn-mark --dry-run

  # 其他
  python workflow_orchestrator.py --status
        """
    )

    parser.add_argument("--full", action="store_true", help="运行完整工作流（含交互菜单）")
    parser.add_argument(
        "--stage",
        choices=["fetch", "prescreen", "subtitle", "feishu", "human-prescreen", "retry-youtube-failed",
                 "discord-video-mark", "curated-cn-mark"],
        help="仅运行指定阶段"
    )
    parser.add_argument("--status", action="store_true", help="显示状态")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="试运行: 只打印发现内容不写飞书 (配合 --stage discord-video-mark / curated-cn-mark)"
    )
    parser.add_argument(
        "--platform",
        default="all",
        choices=["all", "bilibili", "youtube"],
        help="指定平台 (默认: all)"
    )
    parser.add_argument("--auto", action="store_true", help="自动继续（跳过交互菜单，适合定时任务）")
    parser.add_argument(
        "--days", type=int, default=7,
        help="时间过滤天数: 1=今天, 7=本周(默认), 0=全部"
    )
    parser.add_argument(
        "--urls", type=str, default="",
        help="重试模式: 逗号分隔的 YouTube URL 列表 (配合 --stage retry-youtube-failed)"
    )

    args = parser.parse_args()

    orchestrator = WorkflowOrchestrator()
    orchestrator._auto_mode = args.auto
    orchestrator._target_platform = args.platform  # v4.0: 记录目标平台
    orchestrator._days_filter = args.days  # v4.2: 时间过滤天数

    if args.status:
        orchestrator.show_status()
        return

    if args.full:
        orchestrator.run_full()
        return

    if args.stage == "human-prescreen":
        orchestrator.run_from_human_prescreen()
        return

    if args.stage == "retry-youtube-failed":
        orchestrator.stage_retry_youtube_failed(urls=args.urls or None)
        return

    if args.stage == "script-prescreen":
        orchestrator.stage_script_prescreen(platform=args.platform)
        orchestrator.show_status()
        return

    # v5.3.5: 标记类阶段 (Discord 自荐 / 中文精选高优)
    if args.stage == "discord-video-mark":
        orchestrator.stage_discord_video_mark(dry_run=args.dry_run)
        orchestrator.show_status()
        return

    if args.stage == "curated-cn-mark":
        orchestrator.stage_curated_cn_mark(dry_run=args.dry_run)
        orchestrator.show_status()
        return

    if args.stage:
        stage_map = {
            "fetch": lambda: orchestrator.stage_fetch(platform=args.platform),
            "prescreen": lambda: orchestrator.stage_prescreen(args.platform),
            "subtitle": lambda: orchestrator.stage_subtitle(platform=args.platform),
            "feishu": lambda: orchestrator.stage_feishu_sync(platform=args.platform),
        }

        stage_func = stage_map.get(args.stage)
        if stage_func:
            stage_func()
            orchestrator.show_status()
            return

    parser.print_help()
    print("\n示例:")
    print("  python workflow_orchestrator.py --full                    # 运行完整流程（含交互菜单）")
    print("  python workflow_orchestrator.py --full --auto             # 自动模式（跳过菜单）")
    print("  python workflow_orchestrator.py --stage human-prescreen   # 仅从人工初筛开始")
    print("  python workflow_orchestrator.py --stage prescreen         # 仅运行初筛")
    print("  python workflow_orchestrator.py --status                   # 显示状态")


if __name__ == "__main__":
    main()
