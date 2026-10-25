#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prescreen_youtube.py - YouTube 专用初筛脚本 (v4.3.0)

功能：
1. 仅处理 YouTube 表格
2. 支持指定日期范围
3. 回写 Prescreen Status 字段（并发写入）
4. v3.1.1: 本地计算 + 批量并发写入，效率提升 5-10 倍

使用方法：
    # 处理所有 YouTube 视频（全量）
    python prescreen_youtube.py --full

    # 处理指定日期范围的视频
    python prescreen_youtube.py --start 2026-03-29 --end 2026-04-07

    # 仅处理 20260329-20260407
    python prescreen_youtube.py --preset recent
"""

import os
import sys
import json
import argparse
import time
from datetime import datetime
from typing import Tuple, List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))

from config import CONFIG
from feishu_sync import FeishuSync, YT_TABLE_ID, BILI_BITABLE_TOKEN
from scoring import calc_d1_duration_score, calc_d2_freshness_score
from feishu_fields import (
    YT_F_PRESCREEN, YT_F_VIDEO_ID, YT_F_TITLE, YT_F_LINK,
    YT_F_AUTO_NUMBER, YT_F_PUBLISH_DATE, YT_F_DURATION, YT_F_DURATION_MIN,
    YT_PRESCREEN_PASS, YT_PRESCREEN_REJECT,
)

MAX_CONCURRENCY = 40
RATE_LIMIT_DELAY = 0.05
MAX_RETRIES = 3


class YouTubePrescreen:
    """YouTube 专用初筛模块 (v3.1.1 并发版)"""

    def __init__(self):
        self.feishu = FeishuSync()
        self.feishu.set_bitable_token(BILI_BITABLE_TOKEN)
        self.results = []

    def _update_record(self, record_id: str, status: str) -> bool:
        """单条更新飞书记录（使用 lark-cli）"""
        for attempt in range(MAX_RETRIES):
            try:
                if self.feishu.update_record(YT_TABLE_ID, record_id, {YT_F_PRESCREEN: status}):
                    return True
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
        return False

    def _batch_update_concurrent(self, updates: List[Dict], concurrency: int = None):
        """并发批量更新记录"""
        if not updates:
            return

        if concurrency is None:
            concurrency = MAX_CONCURRENCY

        success = 0
        fail = 0
        total = len(updates)

        print(f"[*] 开始并发写入 {total} 条记录 (并发数: {concurrency})...")

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = []
            for item in updates:
                futures.append(executor.submit(self._update_record, item["record_id"], item["status"]))

            for i, future in enumerate(as_completed(futures), 1):
                if future.result():
                    success += 1
                else:
                    fail += 1

                if i % 20 == 0 or i == total:
                    print(f"    进度: {i}/{total} (成功 {success}, 失败 {fail})")

                time.sleep(RATE_LIMIT_DELAY)

        print(f"[*] 写入完成: 成功 {success}, 失败 {fail}")
        return success, fail

    def _detect_concurrency(self) -> int:
        """动态探测最大并发数"""
        print("[*] 动态探测飞书最大并发限制...")

        test_results = self.results[:min(30, len(self.results))]
        if not test_results:
            return MAX_CONCURRENCY

        current_concurrency = 10
        max_success = 0

        while current_concurrency <= MAX_CONCURRENCY:
            print(f"[*] 测试批量 {current_concurrency}...", end=" ")

            start = time.time()
            success = 0

            with ThreadPoolExecutor(max_workers=current_concurrency) as executor:
                futures = [executor.submit(self._update_record, r["record_id"], r["status"]) for r in test_results]
                results = [f.result() for f in as_completed(futures)]
                success = sum(results)

            elapsed = time.time() - start
            success_rate = success / len(results)

            if success_rate >= 0.95:
                print(f"成功 {success_rate*100:.0f}%, 耗时 {elapsed:.1f}s")
                max_success = current_concurrency
                current_concurrency += 5
            else:
                print(f"成功率 {success_rate*100:.0f}% 下降，探测结束")
                break

            time.sleep(0.5)

        if max_success == 0:
            max_success = 10

        print(f"[*] 最大安全并发数: {max_success}")
        return max_success

    def _parse_duration(self, duration_str: str) -> int:
        """解析时长字符串为秒数"""
        if not duration_str:
            return 0
        try:
            if isinstance(duration_str, (int, float)):
                return int(duration_str * 60)
            if ":" in duration_str:
                parts = duration_str.split(":")
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            return int(float(duration_str))
        except (ValueError, TypeError):
            return 0

    def _parse_date(self, date_value) -> datetime:
        """解析日期"""
        if not date_value:
            return None
        if isinstance(date_value, datetime):
            return date_value
        if isinstance(date_value, (int, float)):
            timestamp = date_value
            if timestamp > 1e12:
                timestamp = timestamp / 1000
            try:
                return datetime.fromtimestamp(timestamp)
            except (ValueError, OSError):
                return None
        if isinstance(date_value, str):
            for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"]:
                try:
                    return datetime.strptime(date_value, fmt)
                except ValueError:
                    continue
        return None

    def _check_duration(self, duration_str: str) -> Tuple[bool, str]:
        """检查时长是否符合初筛条件（3-45分钟，与 prescreen.py 一致）"""
        seconds = self._parse_duration(duration_str)
        if seconds == 0:
            return False, "无法解析时长"

        minutes = seconds / 60

        if minutes > 45:
            return False, f"时长{minutes:.0f}分钟超长(>45分钟拒绝)"
        if minutes < 3:
            return False, f"时长{minutes:.1f}分钟太短(<3分钟)"

        return True, f"时长{minutes:.1f}分钟合适"

    def _check_freshness(self, date_value) -> Tuple[bool, str]:
        """检查时效是否符合初筛条件（21天内，与 prescreen.py 一致）"""
        current_date = self._parse_date(date_value)
        if not current_date:
            return False, "无法解析日期"

        from datetime import datetime as dt
        days_ago = (dt.now() - current_date).days
        if days_ago > 21:
            return False, f"发布时间{days_ago}天前(>21天拒绝)"

        return True, f"发布时间{days_ago}天前(21天内)"

    def evaluate_video(self, fields: dict) -> dict:
        """评估单个视频（v4.2.0: 增加时长+时效硬性否决层）"""
        duration_str = fields.get(YT_F_DURATION, "") or fields.get(YT_F_DURATION_MIN, "")
        duration_seconds = self._parse_duration(duration_str)
        date_value = fields.get(YT_F_PUBLISH_DATE, "")

        # 硬性否决：时长检查（3~45分钟）
        duration_ok, duration_reason = self._check_duration(duration_str)
        # 硬性否决：时效检查（21天内）
        freshness_ok, freshness_reason = self._check_freshness(date_value)

        # 计算 D1 分数
        if duration_seconds > 0:
            d1_score, d1_desc = calc_d1_duration_score(duration_seconds)
        else:
            d1_score, d1_desc = 0, "无法解析时长"

        # 计算 D2 分数
        current_date = self._parse_date(date_value)
        if current_date:
            timestamp = int(current_date.timestamp())
            d2_score, d2_desc = calc_d2_freshness_score(timestamp)
        else:
            d2_score, d2_desc = 0, "无法解析日期"

        title = fields.get(YT_F_TITLE) or ""
        if not isinstance(title, str):
            title = str(title) if title else ""

        # 最终决策：硬性条件 AND 分数排序
        passed = duration_ok and freshness_ok and ((d1_score + d2_score) / 2 >= 5.0)

        return {
            "title": title[:50],
            "d1_score": d1_score,
            "d1_desc": d1_desc,
            "d2_score": d2_score,
            "d2_desc": d2_desc,
            "duration_ok": duration_ok,
            "duration_reason": duration_reason,
            "freshness_ok": freshness_ok,
            "freshness_reason": freshness_reason,
            "pre_score": (d1_score + d2_score) / 2,
            "passed": passed,
            "status": YT_PRESCREEN_PASS if passed else YT_PRESCREEN_REJECT
        }

    def run(self, start_date: str = None, end_date: str = None, incremental: bool = True) -> list:
        """执行 YouTube 初筛（本地计算 + 批量并发写入）
        
        参数:
            start_date: 开始日期 (YYYY-MM-DD)，None 表示自动检测
            end_date: 结束日期 (YYYY-MM-DD)
            incremental: 是否启用增量模式（默认 True，只处理新记录）
        """
        print("=" * 50)
        print("YouTube 专用初筛 (v3.2.0 增量模式)")
        print("=" * 50)

        records = self.feishu.get_records(YT_TABLE_ID)
        print(f"[*] 获取到 {len(records)} 条记录")

        # 增量模式：加载上次处理进度
        last_info = {"last_date": "", "last_seq": 0}
        state_file = os.path.join(os.path.dirname(__file__), "..", ".yt_prescreen_state.json")
        
        if incremental and os.path.exists(state_file):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    last_info = json.load(f)
                print(f"[*] 增量模式: 上次已处理到日期 {last_info.get('last_date', 'N/A')} 序号 {last_info.get('last_seq', 0)}")
            except Exception:
                pass
        
        if not incremental:
            print("[*] 全量模式: 处理所有未通过初筛的记录")
        elif start_date:
            print(f"[*] 日期范围: {start_date} ~ {end_date or '现在'}")
        else:
            print("[*] 增量模式: 仅处理新增记录")

        results = []
        skipped_count = 0
        filtered_by_increment = 0
        max_date_this_run = last_info.get("last_date", "")
        max_seq_this_run = last_info.get("last_seq", 0)

        for record in records:
            fields = record.get("fields", {})
            title = fields.get(YT_F_TITLE, "Unknown")
            video_id = fields.get(YT_F_VIDEO_ID, "")
            link = fields.get(YT_F_LINK, "")
            
            # 序号字段
            number_raw = fields.get("Number", 0)
            try:
                current_seq = int(number_raw) if number_raw else 0
            except (ValueError, TypeError):
                current_seq = 0

            current_prescreen = fields.get(YT_F_PRESCREEN, "")
            
            # 跳过已通过初筛的记录
            if current_prescreen == YT_PRESCREEN_PASS:
                skipped_count += 1
                continue
            
            current_date = self._parse_date(fields.get(YT_F_PUBLISH_DATE, ""))
            current_date_str = current_date.strftime("%Y-%m-%d") if current_date else None

            # 增量过滤：只处理日期更新 或 序号更大的记录
            if incremental and not start_date:
                if current_date_str:
                    if current_date_str <= (last_info.get("last_date") or ""):
                        if current_date_str == last_info.get("last_date"):
                            if current_seq <= last_info.get("last_seq", 0):
                                filtered_by_increment += 1
                                continue
                        else:
                            filtered_by_increment += 1
                            continue
                else:
                    if current_seq <= last_info.get("last_seq", 0):
                        filtered_by_increment += 1
                        continue

            # 日期范围过滤（如果指定了）
            if start_date:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else datetime.now()

                if current_date:
                    if current_date.date() < start_dt.date() or current_date.date() > end_dt.date():
                        skipped_count += 1
                        continue
                else:
                    skipped_count += 1
                    continue

            result = self.evaluate_video(fields)
            results.append({
                "record_id": record.get("record_id"),
                "video_id": video_id,
                "title": title,
                "link": link,
                "number": current_seq,
                "date": current_date_str or "Unknown",
                **result
            })

            status_icon = "[OK]" if result["passed"] else "[X]"
            reject_reason = ""
            if not result["duration_ok"]:
                reject_reason = f" 时长否决({result['duration_reason']})"
            elif not result["freshness_ok"]:
                reject_reason = f" 时效否决({result['freshness_reason']})"
            elif not result["passed"]:
                reject_reason = f" 分数不足({result['pre_score']:.1f}<5.0)"
            print(f"  {status_icon} [{current_seq}] {(title or 'N/A')[:40]}...{reject_reason}")
            print(f"      D1:{result['d1_score']:.1f} {result['d1_desc']} | D2:{result['d2_score']:.1f} {result['d2_desc']}")

            # 更新本次运行的最大日期和序号
            if current_date_str:
                if (not max_date_this_run) or (current_date_str > max_date_this_run):
                    max_date_this_run = current_date_str
                    max_seq_this_run = current_seq
                elif current_date_str == max_date_this_run and current_seq > max_seq_this_run:
                    max_seq_this_run = current_seq
            elif current_seq > max_seq_this_run:
                max_seq_this_run = current_seq

        print(f"\n[*] 跳过已通过初筛: {skipped_count} 条")
        if incremental and filtered_by_increment > 0:
            print(f"[*] 增量过滤: {filtered_by_increment} 条 (在已处理范围内)")

        passed_count = sum(1 for r in results if r["passed"])
        rejected_count = len(results) - passed_count

        if results:
            self.results = results
            self._batch_update_concurrent(results)

            # 保存进度（仅增量模式）
            if incremental:
                try:
                    with open(state_file, "w", encoding="utf-8") as f:
                        json.dump({"last_date": max_date_this_run, "last_seq": max_seq_this_run}, f)
                    print(f"[*] 已保存进度: 日期 {max_date_this_run} 序号 {max_seq_this_run}")
                except Exception as e:
                    print(f"[WARN] 保存进度失败: {e}")

        print("=" * 50)
        print(f"[*] 初筛完成:")
        print(f"    通过: {passed_count}")
        print(f"    拒绝: {rejected_count}")
        print(f"    总计: {len(results)}")
        print("=" * 50)

        return results


def main():
    parser = argparse.ArgumentParser(description="YouTube 专用初筛脚本 v3.1.1")
    parser.add_argument("--start", help="开始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--preset", choices=["recent"], help="预设: recent (2026-03-29 ~ 2026-04-07)")
    parser.add_argument("--full", action="store_true", help="全量模式（处理所有记录）")

    args = parser.parse_args()

    start_date = None
    end_date = None

    if args.preset == "recent":
        start_date = "2026-03-29"
        end_date = "2026-04-07"
    elif args.start:
        start_date = args.start
        end_date = args.end

    if not start_date and not args.full:
        print("[*] 未指定日期范围，使用全量模式（处理所有未通过初筛的记录）")
        print("    如需指定日期范围: python prescreen_youtube.py --start 2026-03-29 --end 2026-04-07")
        print("    或使用预设范围: python prescreen_youtube.py --preset recent")

    ps = YouTubePrescreen()
    ps.run(start_date=start_date, end_date=end_date)


if __name__ == "__main__":
    main()
