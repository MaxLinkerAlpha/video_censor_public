#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prescreen.py - 初筛模块 (v5.3.1)

功能：
1. 从飞书多维表格读取视频数据
2. 基于 D1（时长）和 D2（时效）进行初筛
3. 回写初筛状态到飞书（并发写入）
4. 支持基于序号的增量筛选，避免重复处理
5. v5.2.0 ScriptPrescreen 脚本自动去重（替代人工初筛）
   - ⚠️ 只处理【当周写入飞书】的视频 (按 CREATED_AT 过滤, 非全表)
   - 同 up/youtuber 多视频 → 只保留最新
   - 语言总视频 ≤ 8 → 允许同 up 多个
   - 自动写入 Human Prescreen + Notes 字段

v3.1.1 升级：
- 本地计算 + 批量并发写入
- 动态探测飞书最大并发限制
- SSL 重试机制
- 效率提升 5-10 倍

v5.0.0 升级：
- 新增 ScriptPrescreen 类（脚本去重）
- 取消人工初筛等待环节
- 人工只介入最后 AI 审核结果

v5.1.0 升级：
- _language_of 改为先调用 normalize_language() 归一化 BCP-47 -> ISO 639-1
- 豁免判断基于归一化后的语言进行 (en-US 和 en-GB 现在算同一语言)

v5.2.0 升级：
- ScriptPrescreen 新增 days_filter: 只处理当周写入的 Passed 视频 (非全表)
- _fetch_passed_unmarked 新增 Human Prescreen 空字段检查
- 避免 "同 UP 去重" 砍掉历史视频时波及当周新视频

v5.3.1 升级 (bug 修复):
- process_table 增量过滤: 按【序号】而非【发布日期】判断已处理范围
  修复: 7/27~8/1 发布的新推送视频因日期早于 last_date 被误判为已处理而跳过
- ScriptPrescreen 去重: 改为两轮逻辑, 最终入选 < 8 时从丢弃中按日期最新优先补充
  修复: 10 条视频因 "去重前总数 > 8" 禁止同 up, 结果只入选 4 条 (< 8) 的问题
- run() 空结果时返回 stats 补充 failed=0 键, 避免 orchestrator 误报 FAIL

使用方法：
    from prescreen import Prescreen, ScriptPrescreen

    ps = Prescreen()
    ps.run()  # 增量模式 (机器初筛)

    sp = ScriptPrescreen()
    sp.run(platform="bilibili", days_filter=7)  # 脚本去重 (仅当周)
"""

import time
import os
from typing import Dict, List, Any, Tuple, Optional
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CONFIG
from scoring import calc_d1_duration_score, calc_d2_freshness_score
from feishu_sync import FeishuSync, BILI_TABLE_ID, YT_TABLE_ID, BILI_BITABLE_TOKEN
from feishu_fields import (
    BILI_F_PRESCREEN, YT_F_PRESCREEN, BILI_F_AUTO_NUMBER, YT_F_AUTO_NUMBER,
    BILI_F_PUBLISH_DATE, YT_F_PUBLISH_DATE, BILI_F_TITLE, YT_F_TITLE,
    BILI_F_BVID, YT_F_VIDEO_ID, BILI_F_DURATION, YT_F_DURATION,
    BILI_F_HUMAN_PRESCREEN, YT_F_HUMAN_PRESCREEN,
    BILI_F_NOTES, YT_F_NOTES,
    BILI_F_UPLOADER, YT_F_CHANNEL_ID, YT_F_LANGUAGE,
    BILI_F_AI_REVIEW_REPORT, YT_F_AI_REVIEW_REPORT,  # v5.4.4: 兜底校验统计口径
    BILI_F_TOTAL_SCORE, YT_F_TOTAL_SCORE,            # v5.4.4: 兜底校验保留最优
    BILI_PRESCREEN_PASS, YT_PRESCREEN_PASS,
    BILI_PRESCREEN_REJECT, YT_PRESCREEN_REJECT,
    BILI_HUMAN_SELECTED, BILI_HUMAN_REJECTED,
        BILI_HUMAN_HIGH, BILI_HUMAN_MEDIUM, BILI_HUMAN_LOW,  # v5.3.7: 正向标记集合
        YT_HUMAN_SELECTED, YT_HUMAN_REJECTED,
        YT_HUMAN_HIGH, YT_HUMAN_MEDIUM, YT_HUMAN_LOW,  # v5.3.7: 正向标记集合
    BILI_F_CREATED_AT, YT_F_CREATED_AT,  # v5.2.0: 当周过滤
    LANG_FIELD,  # v5.4.4: 兜底校验按 AI 判定语言分组
)
from language_utils import normalize_language  # v5.1.0: BCP-47 → ISO 639-1

MAX_CONCURRENCY = 40
RATE_LIMIT_DELAY = 0.05
MAX_RETRIES = 3


class Prescreen:
    """初筛模块：基于 D1+D2 进行快速筛选 (v3.1.1 并发版)"""

    def __init__(self, feishu_sync: FeishuSync = None):
        self.feishu = feishu_sync or FeishuSync()
        self.results = []
        self._state_file = os.path.join(os.path.dirname(__file__), "..", "data", ".prescreen_state.json")

    def _update_record(self, table_id: str, record_id: str, status: str, is_bilibili: bool = True) -> bool:
        """单条更新飞书记录（使用 lark-cli）"""
        field_name = BILI_F_PRESCREEN if is_bilibili else YT_F_PRESCREEN
        for attempt in range(MAX_RETRIES):
            try:
                if self.feishu.update_record(table_id, record_id, {field_name: status}):
                    return True
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                print(f"    [DEBUG] 更新失败 record_id={record_id}, {e}")
        return False

    def _batch_update_concurrent(self, table_id: str, updates: List[Dict], concurrency: int = None):
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
                futures.append(executor.submit(self._update_record, table_id, item["record_id"], item["status"], item.get("is_bilibili", True)))

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

    def _detect_concurrency(self, test_count: int = 10) -> int:
        """动态探测最大并发数"""
        print("[*] 动态探测飞书最大并发限制...")

        test_results = self.results[:min(test_count, len(self.results))]
        if not test_results:
            return MAX_CONCURRENCY

        current_concurrency = 10
        max_success = 0
        consecutive_success = 0

        while current_concurrency <= MAX_CONCURRENCY:
            print(f"[*] 测试批量 {current_concurrency}...", end=" ")

            start = time.time()
            success = 0

            with ThreadPoolExecutor(max_workers=current_concurrency) as executor:
                futures = [executor.submit(self._update_record, test_results[0]["table_id"], r["record_id"], r["status"]) for r in test_results]
                results = [f.result() for f in as_completed(futures)]
                success = sum(results)

            elapsed = time.time() - start
            success_rate = success / len(results)

            if success_rate >= 0.95:
                print(f"成功 {success_rate*100:.0f}%, 耗时 {elapsed:.1f}s")
                max_success = current_concurrency
                consecutive_success += 1
                current_concurrency += 5
            else:
                print(f"成功率 {success_rate*100:.0f}% 下降，探测结束")
                break

            time.sleep(0.5)

        if max_success == 0:
            max_success = 10

        print(f"[*] 最大安全并发数: {max_success}")
        return max_success

    def _load_last_processed(self, platform: str) -> dict:
        """加载上次处理的进度（日期和序号）"""
        try:
            if os.path.exists(self._state_file):
                import json
                with open(self._state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                return state.get(platform, {"last_date": None, "last_seq": 0})
        except (json.JSONDecodeError, IOError, OSError):
            pass
        return {"last_date": None, "last_seq": 0}

    def _save_last_processed(self, platform: str, last_date: str, last_seq: int) -> None:
        """保存当前进度"""
        try:
            import json
            state = {}
            if os.path.exists(self._state_file):
                with open(self._state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
            state[platform] = {"last_date": last_date, "last_seq": last_seq}
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except (json.JSONDecodeError, IOError, OSError):
            pass

    def _parse_duration(self, duration_str: str) -> int:
        """解析时长字符串为秒数"""
        if not duration_str:
            return 0

        try:
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
        """解析日期，支持时间戳(毫秒/秒)或字符串"""
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
            formats = [
                "%Y-%m-%d",
                "%Y/%m/%d",
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S",
            ]
            for fmt in formats:
                try:
                    return datetime.strptime(date_value, fmt)
                except ValueError:
                    continue

        return None

    def _check_duration(self, duration_str: str) -> Tuple[bool, str]:
        """检查时长是否符合初筛条件（3-45分钟，v3.1.1）"""
        seconds = self._parse_duration(duration_str)
        if seconds == 0:
            return False, "无法解析时长"

        minutes = seconds / 60

        if minutes > 45:
            return False, f"时长{minutes:.0f}分钟超长(>45分钟拒绝)"
        if minutes < 3:
            return False, f"时长{minutes:.1f}分钟太短(<3分钟)"

        return True, f"时长{minutes:.1f}分钟合适"

    def _check_freshness(self, date_str: str) -> Tuple[bool, str]:
        """检查时效性是否符合初筛条件（≤21天）"""
        date = self._parse_date(date_str)
        if not date:
            return False, "无法解析日期"

        now = datetime.now()
        days_ago = (now - date).days

        if days_ago > 21:
            return False, f"视频发布于{days_ago}天前，超过21天"
        if days_ago < 0:
            return False, "视频发布日期在将来"

        return True, f"视频发布于{days_ago}天前"

    def _calculate_d1_score(self, duration_str: str) -> Dict[str, Any]:
        """计算 D1 时长得分"""
        seconds = self._parse_duration(duration_str)
        score, desc = calc_d1_duration_score(seconds)
        return {"score": score, "desc": desc}

    def _calculate_d2_score(self, date_str) -> Dict[str, Any]:
        """计算 D2 时效得分"""
        date = self._parse_date(date_str)
        if not date:
            return {"score": 0, "desc": "无法解析日期"}

        timestamp = int(date.timestamp())
        score, desc = calc_d2_freshness_score(timestamp)
        return {"score": score, "desc": desc}

    def evaluate_video(self, fields: Dict, is_bilibili: bool = True) -> Dict[str, Any]:
        """
        评估单个视频 (v3.2.0: 支持按平台区分中/英文状态值)
        
        参数:
            fields: 飞书记录字段
            is_bilibili: True=B站(中文状态), False=YouTube(英文状态)
        """
        video_id = fields.get("视频ID") or fields.get(BILI_F_BVID) or fields.get(YT_F_VIDEO_ID, "")
        title = fields.get(BILI_F_TITLE) or fields.get(YT_F_TITLE, "")
        duration = fields.get("时长") or fields.get(BILI_F_DURATION, "") or fields.get(YT_F_DURATION, "")
        date = fields.get(BILI_F_PUBLISH_DATE) or fields.get("发布时间", "") or fields.get(YT_F_PUBLISH_DATE, "")

        duration_ok, duration_reason = self._check_duration(duration)
        freshness_ok, freshness_reason = self._check_freshness(date)

        d1_result = self._calculate_d1_score(duration)
        d2_result = self._calculate_d2_score(date)

        pre_score = (d1_result["score"] + d2_result["score"]) / 2

        passed = duration_ok and freshness_ok

        # v3.2.0: B站用中文, YouTube用英文
        status_passed = BILI_PRESCREEN_PASS if is_bilibili else YT_PRESCREEN_PASS
        status_rejected = BILI_PRESCREEN_REJECT if is_bilibili else YT_PRESCREEN_REJECT

        return {
            "video_id": str(video_id),
            "title": str(title)[:50],
            "duration": duration,
            "date": date,
            "duration_ok": duration_ok,
            "duration_reason": duration_reason,
            "freshness_ok": freshness_ok,
            "freshness_reason": freshness_reason,
            "d1_score": d1_result["score"],
            "d1_desc": d1_result.get("desc", ""),
            "d2_score": d2_result["score"],
            "d2_desc": d2_result.get("desc", ""),
            "pre_score": pre_score,
            "passed": passed,
            "status": status_passed if passed else status_rejected
        }

    def process_table(self, table_id: str, platform: str = "unknown", incremental: bool = True) -> List[Dict]:
        """处理整个表的记录（本地计算 + 批量并发写入）"""
        print(f"[*] 开始处理表: {platform} ({table_id})")

        is_bilibili = platform.lower() == "bilibili"
        seq_field = BILI_F_AUTO_NUMBER if is_bilibili else YT_F_AUTO_NUMBER
        date_field = BILI_F_PUBLISH_DATE if is_bilibili else YT_F_PUBLISH_DATE
        title_field = BILI_F_TITLE if is_bilibili else YT_F_TITLE
        prescreen_field = BILI_F_PRESCREEN if is_bilibili else YT_F_PRESCREEN
        status_passed = BILI_PRESCREEN_PASS if is_bilibili else YT_PRESCREEN_PASS
        status_rejected = BILI_PRESCREEN_REJECT if is_bilibili else YT_PRESCREEN_REJECT

        last_info = {"last_date": None, "last_seq": 0}
        if incremental:
            last_info = self._load_last_processed(platform)
            print(f"[*] 增量模式: 上次已处理到日期 {last_info.get('last_date', '无')} 序号 {last_info.get('last_seq', 0)}")

        records = self.feishu.get_records(table_id)
        print(f"[*] 获取到 {len(records)} 条记录")

        results = []
        max_date_this_run = last_info.get("last_date")
        max_seq_this_run = last_info.get("last_seq", 0)
        skip_count = 0
        filtered_by_increment = 0

        for record in records:
            fields = record.get("fields", {})
            current_seq = int(fields.get(seq_field, 0) or 0)

            current_date = self._parse_date(fields.get(date_field))
            current_date_str = current_date.strftime("%Y-%m-%d") if current_date else None

            # v5.3.1: 修复增量过滤逻辑
            # 旧逻辑按【发布日期】判断 (current_date <= last_date 就跳过),
            # 导致新推送的早期日期视频 (如 7/27 发布, 晚于 8/2 才入库) 被误判为已处理。
            # 新逻辑按【序号】判断: 序号是飞书自动编号单调递增, 新入库记录序号必然更大。
            if incremental:
                if current_seq and current_seq <= last_info.get("last_seq", 0):
                    filtered_by_increment += 1
                    continue

            if fields.get(prescreen_field) == status_passed:
                skip_count += 1
                continue

            result = self.evaluate_video(fields, is_bilibili=is_bilibili)
            result["record_id"] = record.get("record_id")
            result["platform"] = platform
            result["seq"] = current_seq
            result["table_id"] = table_id
            result["is_bilibili"] = is_bilibili
            results.append(result)

            # v3.1.2: 修复无法解析日期的记录导致进度永不更新
            if current_date_str:
                if not max_date_this_run or current_date_str > (max_date_this_run or ""):
                    max_date_this_run = current_date_str
                    max_seq_this_run = current_seq
                elif current_date_str == max_date_this_run:
                    if current_seq > max_seq_this_run:
                        max_seq_this_run = current_seq
            else:
                # 日期无法解析时，仅用序号跟踪进度，防止同一条记录反复处理
                if current_seq > max_seq_this_run:
                    max_seq_this_run = current_seq

            status_icon = "[OK]" if result["passed"] else "[X]"
            print(f"  {status_icon} [{current_seq}] {result['title'][:30]}...")
            print(f"      D1:{result['d1_score']:.0f} {result['d1_desc']} | D2:{result['d2_score']:.0f} {result['d2_desc']}")

        print(f"[*] 跳过已处理: {skip_count} 条")
        if incremental and filtered_by_increment > 0:
            print(f"[*] 增量过滤: {filtered_by_increment} 条 (日期/序号在已处理范围内)")

        if results:
            self.results = results
            self._batch_update_concurrent(table_id, results)

        if incremental and results:
            self._save_last_processed(platform, max_date_this_run, max_seq_this_run)
            print(f"[*] 已保存进度: 日期 {max_date_this_run} 序号 {max_seq_this_run}")

        return results

    def run(self, platform: str = "bilibili", incremental: bool = True) -> List[Dict]:
        """运行初筛"""
        mode_str = "增量模式" if incremental else "全量模式"
        print("=" * 50)
        print(f"初筛模块 (v3.1.1 {mode_str}) - {platform}")
        print("=" * 50)

        self.feishu.set_bitable_token(BILI_BITABLE_TOKEN)

        if platform.lower() == "bilibili":
            table_id = BILI_TABLE_ID
        else:
            table_id = YT_TABLE_ID

        results = self.process_table(table_id, platform, incremental=incremental)

        passed = sum(1 for r in results if r["passed"])
        rejected = sum(1 for r in results if not r["passed"])

        print(f"\n[*] 初筛完成:")
        print(f"    通过: {passed}")
        print(f"    拒绝: {rejected}")
        print(f"    总计: {len(results)}")

        self.results = results
        return results


# ============================================================
# v5.0.0 脚本自动去重 (ScriptPrescreen)
# ============================================================
# 替代原人工初筛，基于规则:
#   1. 同 up/youtuber 多视频 → 只保留最新
#   2. 语言总视频 ≤ 8 → 允许同 up 多个
# 人工只介入最后 AI 审核结果查看

# v5.0.0: 同 up 允许多个视频的语言总视频数阈值
SCRIPT_MULTI_AUTHOR_THRESHOLD = 8

# v5.4.4: 语言归一化后"未知"的取值 (normalize_language 对空/None/"N/A" 统一返回 "N/A")
UNKNOWN_LANGUAGE_KEY = "N/A"


def _group_by(items: List[Dict], key: str) -> Dict[str, List[Dict]]:
    """按 key 字段分组 (空值归入 'unknown' 分组)"""
    grouped: Dict[str, List[Dict]] = {}
    for item in items:
        val = str(item.get(key, "") or "").strip()
        bucket = val if val else "unknown"
        grouped.setdefault(bucket, []).append(item)
    return grouped


def _parse_date_value(value) -> str:
    """归一化日期字段为可比较字符串, 用于排序"""
    if not value:
        return ""
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, OSError):
            return ""
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return ""
        return s
    return str(value)


class ScriptPrescreen:
    """v5.0.0 脚本自动去重 (替代人工初筛)

    读取 Prescreen Status = "Passed" 且 Human Prescreen 为空 的记录,
    按 up/youtuber + 语言视频数规则去重, 写回 Human Prescreen + Notes.

    使用方法:
        sp = ScriptPrescreen()
        sp.run(platform="bilibili")
    """

    def __init__(self, feishu_sync: FeishuSync = None):
        self.feishu = feishu_sync or FeishuSync()
        self.kept: List[Dict] = []
        self.dropped: List[Dict] = []

    def _platform_config(self, platform: str) -> Dict[str, Any]:
        """获取平台相关字段配置"""
        is_bili = platform.lower() == "bilibili"
        if is_bili:
            return {
                "is_bilibili": True,
                "table_id": BILI_TABLE_ID,
                "video_id_field": BILI_F_BVID,
                "author_field": BILI_F_UPLOADER,
                "date_field": BILI_F_PUBLISH_DATE,
                "language_field": None,  # B站默认中文, 硬编码
                "default_language": "zh",
                "prescreen_pass": BILI_PRESCREEN_PASS,
                "human_selected": BILI_HUMAN_SELECTED,
                "human_rejected": BILI_HUMAN_REJECTED,
                # v5.3.7: 正向标记集合 (这些标记视为"本周已有代表视频")
                "human_positive_values": {BILI_HUMAN_HIGH, BILI_HUMAN_MEDIUM,
                                          BILI_HUMAN_LOW, BILI_HUMAN_SELECTED},
                "notes_field": BILI_F_NOTES,
                "create_time_field": BILI_F_CREATED_AT,  # v5.2.0: 当周过滤用
                "human_prescreen_field": BILI_F_HUMAN_PRESCREEN,  # v5.2.0: 检查是否已标记
                "number_field": BILI_F_AUTO_NUMBER,   # v5.4.2: 飞书自动编号, 用于批次校验
            }
        return {
            "is_bilibili": False,
            "table_id": YT_TABLE_ID,
            "video_id_field": YT_F_VIDEO_ID,
            "author_field": YT_F_CHANNEL_ID,
            "date_field": YT_F_PUBLISH_DATE,
            "language_field": YT_F_LANGUAGE,
            "default_language": "unknown",
            "prescreen_pass": YT_PRESCREEN_PASS,
            "human_selected": YT_HUMAN_SELECTED,
            "human_rejected": YT_HUMAN_REJECTED,
            # v5.3.7: 正向标记集合 (这些标记视为"本周已有代表视频")
            "human_positive_values": {YT_HUMAN_HIGH, YT_HUMAN_MEDIUM,
                                      YT_HUMAN_LOW, YT_HUMAN_SELECTED},
            "notes_field": YT_F_NOTES,
            "create_time_field": YT_F_CREATED_AT,  # v5.2.0: 当周过滤用
            "human_prescreen_field": YT_F_HUMAN_PRESCREEN,  # v5.2.0: 检查是否已标记
            "number_field": YT_F_AUTO_NUMBER,     # v5.4.2: 飞书自动编号, 用于批次校验
        }

    def _fetch_passed_unmarked(self, cfg: Dict[str, Any], days_filter: int = 7) -> Tuple[List[Dict], Dict[str, set], Dict[str, int]]:
        """拉取 Prescreen=Passed 且 Human Prescreen 为空的记录 (v5.2.0)

        v5.2.0 重大变更:
        - 新增 days_filter: 按 写入时间 (CREATED_AT) 过滤, 只处理当周写入的记录
        - 新增 Human Prescreen 空字段检查 (之前 docstring 写了但实际没做)
        - 不再处理全表历史数据, 避免 "同 UP 去重" 砍掉旧视频时波及新视频

        v5.3.9 变更:
        - 返回 (candidates, marked_authors_by_lang, marked_positive_by_lang):
          * marked_authors_by_lang: 语言 -> 本周已有正向标记 (自荐/精选/优先级/入选)
            的 UP 集合, ScriptDedup 按语言分组去重时, 该语言组内排除这些 UP 的其余候选
          * marked_positive_by_lang: 语言 -> 本周正向标记记录数, 计入该语言组"有效入选"
        - 标记检查先于 Prescreen 检查: 强行入选 (Prescreen=拒绝 + 优先级标记)
          也是本周入选, 其 UP 应记入"已有代表"集合

        v5.4.2 变更 (修复"上次产出被误算作本周已标记"的边界 BUG):
        - 旧实现用单一 7 天滚动窗口同时约束"候选池"与"已标记统计", 若上次运行日
          恰好等于 today-(days_filter-1), 上次产出 (Human=Selected) 会落进窗口,
          被当成"本周已标记 UP", 使其 UP 的整批候选被排除
          (2026-09-20 事故: YouTube 25 个频道 102/145 条被误排除)
        - 现改为运行锚点 (core/run_anchor.py) 提供两个独立下界:
            pool_cutoff_ms   = 上次运行 **开始** 时刻 -> 上次中断未处理的记录也能接住
            marked_cutoff_ms = 上次运行 **结束** 时刻 -> 排除上次运行自身的产出
        - 锚点缺失时回退到原有的固定天数窗口 (两者相同), 并打印 WARN
        """
        records = self.feishu.get_records(cfg["table_id"])

        # v5.2.0: 计算时间窗口; v5.4.2: 改用运行锚点, 不再依赖固定天数
        import datetime as dt_mod
        from run_anchor import get_last_run
        pool_cutoff_ms = 0
        marked_cutoff_ms = 0
        anchor = get_last_run()
        if anchor["start"] or anchor["end"]:
            pool_cutoff_ms = anchor["start"]
            marked_cutoff_ms = anchor["end"]
            print(f"[*] 运行锚点: 候选池下界={self._fmt_ms(pool_cutoff_ms)}, "
                  f"已标记下界={self._fmt_ms(marked_cutoff_ms)}", flush=True)
        elif days_filter > 0:
            # 回退: 无锚点 (首次运行) 时沿用固定天数窗口
            today_start = dt_mod.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            pool_cutoff_ms = marked_cutoff_ms = int(
                (today_start - dt_mod.timedelta(days=days_filter - 1)).timestamp() * 1000)
            print(f"[WARN] 未找到运行锚点, 回退到 {days_filter} 天固定窗口 "
                  f"(下界={self._fmt_ms(pool_cutoff_ms)})", flush=True)

        def _parse_create_time(val):
            """解析 CREATED_AT 为毫秒时间戳 (与 feishu_sync 一致)"""
            if not val:
                return 0
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str):
                s = val.strip()
                if not s:
                    return 0
                try:
                    dt = dt_mod.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                    return int(dt.timestamp() * 1000)
                except ValueError:
                    return 0
            return 0

        result = []
        skipped_by_time = 0
        skipped_by_already_marked = 0
        # v5.4.2: 收集本批候选池的飞书 Number (自动编号), 用于批次边界校验
        pool_numbers: List[int] = []
        # v5.3.9: 按语言维度记录正向标记 (用于按语言分组去重时组内排除)
        marked_authors_by_lang: Dict[str, set] = {}
        marked_positive_by_lang: Dict[str, int] = {}
        for rec in records:
            fields = rec.get("fields", {})

            if str(fields.get(cfg["video_id_field"], "")).strip() == "":
                continue

            create_time = _parse_create_time(fields.get(cfg["create_time_field"], 0))

            # v5.2.0 + v5.4.2: 候选池只取"上次运行开始之后"写入的记录
            in_pool_window = True
            if pool_cutoff_ms > 0 and create_time and create_time < pool_cutoff_ms:
                skipped_by_time += 1
                in_pool_window = False

            # v5.2.0: 跳过已有人工标记的 (避免重复覆盖)
            hp = str(fields.get(cfg["human_prescreen_field"], "") or "").strip()
            if hp:
                skipped_by_already_marked += 1
                # v5.3.9: 当周正向标记按语言记入"已有代表"集合
                #   (不看 Prescreen 状态 — Prescreen=拒绝 + 优先级标记的强行入选也是入选)
                # v5.4.2: 下界改用"上次运行结束时刻", 排除上次运行自身的产出,
                #   否则上次入选的 UP 会在本次被整批排除
                if hp in cfg["human_positive_values"] and (
                        marked_cutoff_ms <= 0 or not create_time or create_time >= marked_cutoff_ms):
                    author = str(fields.get(cfg["author_field"], "") or "").strip()
                    lang = self._language_of({"fields": fields}, cfg)
                    if author:
                        marked_authors_by_lang.setdefault(lang, set()).add(author)
                    marked_positive_by_lang[lang] = marked_positive_by_lang.get(lang, 0) + 1
                continue

            if not in_pool_window:
                continue

            # 初筛必须已通过
            prescreen_field_name = BILI_F_PRESCREEN if cfg["is_bilibili"] else YT_F_PRESCREEN
            if str(fields.get(prescreen_field_name, "")) != cfg["prescreen_pass"]:
                continue

            result.append({
                "record_id": rec.get("record_id"),
                "fields": fields,
            })
            # v5.4.2: 记录候选池的 Number (飞书返回为字符串, 需转 int)
            num_raw = str(fields.get(cfg.get("number_field") or "", "") or "").strip()
            if num_raw.isdigit():
                pool_numbers.append(int(num_raw))

        if skipped_by_time > 0:
            print(f"[*] 时间过滤跳过 {skipped_by_time} 条 (非当周)", flush=True)
        if skipped_by_already_marked > 0:
            print(f"[*] 已标记跳过 {skipped_by_already_marked} 条", flush=True)
        # v5.4.2: 输出本批 Number 区间 — 同一批由 Fetcher 连续写入, 应为一整段连续编号;
        #   若区间不连续或起点异常, 说明批次边界可能有问题 (配合锚点日志排查)
        if pool_numbers:
            nums_sorted = sorted(pool_numbers)
            contiguous = (nums_sorted[-1] - nums_sorted[0] + 1) == len(nums_sorted)
            print(f"[*] 本批 Number 区间: {nums_sorted[0]} ~ {nums_sorted[-1]} "
                  f"(共 {len(nums_sorted)} 条, 连续={'是' if contiguous else '否'})", flush=True)
        return result, marked_authors_by_lang, marked_positive_by_lang

    def _fmt_ms(self, ms: int) -> str:
        """毫秒时间戳 -> 可读字符串 (0 显示为"-", 供日志打印)"""
        if not ms:
            return "-"
        import datetime as dt_mod
        return dt_mod.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")

    def _language_of(self, item: Dict, cfg: Dict[str, Any]) -> str:
        """获取视频语言, v5.1.0: 已归一化为 ISO 639-1 二字符代码.

        旧版直接返回飞书中的 BCP-47 字符串 (en-US, de-DE), 同一语言的不同变体
        会被 ScriptPrescreen 当成不同语言, 豁免判断失效。
        v5.1.0 起统一归一化: de-DE -> de, en-US -> en, ja-JP -> ja, ...
        """
        if cfg["language_field"]:
            val = str(item["fields"].get(cfg["language_field"], "") or "").strip()
            if not val:
                return "unknown"
            return normalize_language(val)
        return cfg["default_language"]

    def _author_of(self, item: Dict, cfg: Dict[str, Any]) -> str:
        return str(item["fields"].get(cfg["author_field"], "") or "").strip()

    def _date_of(self, item: Dict, cfg: Dict[str, Any]) -> str:
        return _parse_date_value(item["fields"].get(cfg["date_field"], ""))

    def _video_label(self, item: Dict, cfg: Dict[str, Any]) -> str:
        """取 video_id 作为展示标签"""
        return str(item["fields"].get(cfg["video_id_field"], "?"))

    def _decide(self, items: List[Dict], cfg: Dict[str, Any]) -> Tuple[List[Tuple[Dict, str]], List[Tuple[Dict, str]]]:
        """对同一作者的视频做去重决策

        返回: (kept, dropped) - 每项是 (item_dict, notes)
        """
        author = self._author_of(items[0], cfg) if items else ""
        labels = [self._video_label(v, cfg) for v in items]
        if not author:
            return [(v, "Kept: no author") for v in items], []
        if len(items) == 1:
            return [(items[0], "Kept: only video from this author")], []
        sorted_items = sorted(items, key=lambda v: self._date_of(v, cfg), reverse=True)
        return [(sorted_items[0], f"Kept: latest of {len(items)} from {author}")], [
            (v, f"Dropped: kept latest of {len(items)} from {author}")
            for v in sorted_items[1:]
        ]

    def run(self, platform: str = "bilibili", days_filter: int = 7) -> Dict[str, Any]:
        """执行脚本去重, 返回统计

        v5.2.0: 新增 days_filter, 只处理当周写入的 Passed 视频
          days_filter=7: 本周 (默认)
          days_filter=1: 今天
          days_filter=0: 全量 (调试用, 一般不用)
        """
        cfg = self._platform_config(platform)
        print("=" * 60, flush=True)
        print(f"脚本自动去重 (ScriptPrescreen v5.3.9) - {platform} | 时间过滤: {days_filter}天", flush=True)
        print("=" * 60, flush=True)

        self.feishu.set_bitable_token(BILI_BITABLE_TOKEN)

        # v5.2.0: 传入 days_filter, 只拉取当周 Prescreen=Passed 且 Human 为空的记录
        # v5.3.9: 同时返回按语言维度的正向标记 UP 集合与数量 (Discord 自荐/中文精选/已入选)
        all_items, marked_authors_by_lang, marked_positive_by_lang = self._fetch_passed_unmarked(cfg, days_filter=days_filter)
        print(f"[*] Prescreen=Passed, Human=空, 当周(days_filter={days_filter}): {len(all_items)} 条", flush=True)
        if marked_authors_by_lang:
            total_marked = sum(marked_positive_by_lang.values())
            total_marked_up = sum(len(s) for s in marked_authors_by_lang.values())
            print(f"[*] 本周已有正向标记: {total_marked} 条 / {total_marked_up} 个 UP "
                  f"(按语言组内排除其候选视频)", flush=True)

        if not all_items:
            print("[*] 无需处理, 退出", flush=True)
            return {"kept": 0, "dropped": 0, "skipped": 0, "failed": 0}

        # v5.3.9: 恢复按语言分组去重 (撤销 v5.3.8 的全局去重)。
        # 用户规则: 去重按语言维度 —— 每个语言组内每频道最多 1 条 (同 UP 跨语言
        # 可各留 1 条, 因不同语言面向不同受众); 该语言组内已标记 UP (自荐/精选) 的
        # 其他候选视频排除 (该 UP 在本语言已有代表视频)。
        #
        # v5.4.4 修正: "声明语言未知(N/A)" 的候选不再自成一组独占配额。
        #   旧实现下同一频道可同时在 en 组与 N/A 组各拿一份配额
        #   (实测: 英文组 18 条 > 8, 但 频道A 3 条 / 频道B 2 条),
        #   表现为"某语言明明 >8, 同一频道却入选多次"。
        #   新规则:
        #     ① 未知语言的候选, 先归入"该频道在本批中出现过的已知语言组"
        #        (同一频道的视频理应算同一语言受众);
        #     ② 若该频道本批全是未知语言, 才进入"未知语言补充位" ——
        #        不占任何语言配额, 只允许"任何语言组都还没有代表"的作者补 1 条。
        by_lang: Dict[str, List[Dict]] = {}
        unknown_items: List[Dict] = []
        channel_lang: Dict[str, str] = {}
        for item in all_items:
            lang = self._language_of(item, cfg)
            if lang == UNKNOWN_LANGUAGE_KEY:
                continue
            by_lang.setdefault(lang, []).append(item)
            author = self._author_of(item, cfg)
            if author and author not in channel_lang:
                channel_lang[author] = lang
        for item in all_items:
            if self._language_of(item, cfg) != UNKNOWN_LANGUAGE_KEY:
                continue
            author = self._author_of(item, cfg)
            lang = channel_lang.get(author, "") if author else ""
            if lang:
                by_lang.setdefault(lang, []).append(item)
            else:
                unknown_items.append(item)
        if unknown_items:
            print(f"[*] 声明语言未知且频道本批无已知语言: {len(unknown_items)} 条 "
                  f"-> 未知语言补充位 (不占语言配额)", flush=True)

        all_kept: List[Tuple[Dict, str]] = []
        all_dropped: List[Tuple[Dict, str]] = []

        for lang, lang_items in sorted(by_lang.items(), key=lambda kv: -len(kv[1])):
            marked_in_lang = marked_authors_by_lang.get(lang, set())
            marked_count_in_lang = marked_positive_by_lang.get(lang, 0)
            print(f"\n[*] 语言={lang}: {len(lang_items)} 条 "
                  f"(已标记 {marked_count_in_lang} 条 / {len(marked_in_lang)} 个 UP)", flush=True)

            by_author: Dict[str, List[Dict]] = {}
            for item in lang_items:
                author = self._author_of(item, cfg)
                by_author.setdefault(author, []).append(item)

            round1_kept: List[Tuple[Dict, str]] = []
            fill_pool: List[Tuple[Dict, str]] = []   # 同频道多余 (可补足)
            excluded: List[Tuple[Dict, str]] = []    # 已标记 UP 的候选 (不参与补足)

            for author, items in by_author.items():
                if author and author in marked_in_lang:
                    # v5.3.9: 该 UP 在本语言已有正向标记视频, 其余候选排除
                    for v in items:
                        excluded.append((v, "Dropped: author already marked this week (same language)"))
                    continue
                kept, dropped = self._decide(items, cfg)
                round1_kept.extend(kept)
                fill_pool.extend(dropped)

            # 补足: 该语言组有效入选 (已标记 + 脚本保留) < 8 时, 从同频道多余视频
            # 按发布日期最新优先补充 (规则: 总数<8 时允许同频道重复)
            effective_lang = marked_count_in_lang + len(round1_kept)
            if effective_lang < SCRIPT_MULTI_AUTHOR_THRESHOLD and fill_pool:
                print(f"    [*] 有效入选 {effective_lang} 条 (< {SCRIPT_MULTI_AUTHOR_THRESHOLD}), "
                      f"从同频道多余 {len(fill_pool)} 条中按日期最新优先补充", flush=True)
                fill_pool.sort(key=lambda kv: self._date_of(kv[0], cfg), reverse=True)
                need = SCRIPT_MULTI_AUTHOR_THRESHOLD - effective_lang
                for item, notes in fill_pool:
                    if need <= 0:
                        break
                    round1_kept.append((item, f"Kept: fill to {SCRIPT_MULTI_AUTHOR_THRESHOLD} (from {notes})"))
                    need -= 1
                kept_ids = {id(v) for v, _ in round1_kept}
                fill_pool = [(v, n) for v, n in fill_pool if id(v) not in kept_ids]
            elif effective_lang >= SCRIPT_MULTI_AUTHOR_THRESHOLD:
                print(f"    [*] 有效入选 {effective_lang} 条 (>= {SCRIPT_MULTI_AUTHOR_THRESHOLD}), 每频道 1 条", flush=True)

            all_kept.extend(round1_kept)
            all_dropped.extend(fill_pool)
            all_dropped.extend(excluded)

        # v5.4.4: 未知语言补充位 —— 不占语言配额, 只补"任何语言组都还没有代表"的作者 1 条
        if unknown_items:
            represented: set = set()
            for s in marked_authors_by_lang.values():
                represented |= s
            for v, _ in all_kept:
                a = self._author_of(v, cfg)
                if a:
                    represented.add(a)
            by_author_unknown: Dict[str, List[Dict]] = {}
            for item in unknown_items:
                by_author_unknown.setdefault(self._author_of(item, cfg), []).append(item)
            for author, items in by_author_unknown.items():
                if author and author in represented:
                    for v in items:
                        all_dropped.append(
                            (v, "Dropped: unknown language & author already has a representative"))
                    continue
                kept, dropped = self._decide(items, cfg)
                all_kept.extend(kept)
                all_dropped.extend(dropped)
                for v, _ in kept:
                    a = self._author_of(v, cfg)
                    if a:
                        represented.add(a)

        total_marked = sum(marked_positive_by_lang.values())
        print(f"\n[*] 去重结果: 保留 {len(all_kept)} | 丢弃 {len(all_dropped)} | "
              f"本周已标记(正向) {total_marked} | 有效入选 {total_marked + len(all_kept)}", flush=True)

        # 写回飞书: Human Prescreen + Notes
        # v5.4.0: Notes 合并写入 (保留已有内容, 如 "来源: Discord videos-n-streams 自荐"
        # / "来源: 中文精选表" 来源标注行), 去重备注追加在原内容之后 —— 不再整体覆盖
        def _merge_notes(item: Dict, dedup_note: str) -> str:
            existing = str(item["fields"].get(cfg["notes_field"], "") or "").strip()
            return f"{existing}\n{dedup_note}" if existing else dedup_note

        updates = []
        for item, notes in all_kept:
            updates.append({
                "record_id": item["record_id"],
                "fields": {
                    cfg["video_id_field"]: str(item["fields"].get(cfg["video_id_field"], "")),
                    cfg["notes_field"]: _merge_notes(item, notes),
                },
                "human_value": cfg["human_selected"],
            })
        for item, notes in all_dropped:
            updates.append({
                "record_id": item["record_id"],
                "fields": {
                    cfg["video_id_field"]: str(item["fields"].get(cfg["video_id_field"], "")),
                    cfg["notes_field"]: _merge_notes(item, notes),
                },
                "human_value": cfg["human_rejected"],
            })

        success = 0
        fail = 0
        if updates:
            print(f"[*] 写入飞书 {len(updates)} 条 (并发=10)...", flush=True)

            def _write_one(u: Dict) -> bool:
                notes_value = u["fields"].get(cfg["notes_field"], "") if isinstance(u.get("fields"), dict) else ""
                payload = {
                    BILI_F_HUMAN_PRESCREEN if cfg["is_bilibili"] else YT_F_HUMAN_PRESCREEN: u["human_value"],
                    cfg["notes_field"]: notes_value,
                }
                for attempt in range(MAX_RETRIES):
                    try:
                        if self.feishu.update_record(cfg["table_id"], u["record_id"], payload):
                            return True
                    except Exception as e:
                        if attempt < MAX_RETRIES - 1:
                            time.sleep(0.5 * (attempt + 1))
                            continue
                        print(f"    [DEBUG] 写入失败 record_id={u['record_id']}, {e}", flush=True)
                return False

            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {pool.submit(_write_one, u): i for i, u in enumerate(updates)}
                done_count = 0
                for f in as_completed(futures):
                    if f.result():
                        success += 1
                    else:
                        fail += 1
                    done_count += 1
                    if done_count % 10 == 0:
                        print(f"    写入进度: {done_count}/{len(updates)} (成功 {success}, 失败 {fail})", flush=True)

        print(f"\n[*] 写入完成: 成功 {success}, 失败 {fail}", flush=True)
        print(f"    保留 (Human Prescreen={cfg['human_selected']}): {len(all_kept)}", flush=True)
        print(f"    丢弃 (Human Prescreen={cfg['human_rejected']}): {len(all_dropped)}", flush=True)

        self.kept = all_kept
        self.dropped = all_dropped
        return {"kept": len(all_kept), "dropped": len(all_dropped), "failed": fail}


# ============================================================
# v5.4.4: 最终一致性收敛 (兜底校验)
# ============================================================

def _norm_select(val: Any) -> str:
    """单选字段取值 (API 可能返回列表), 统一为去空白字符串"""
    if isinstance(val, list):
        return str(val[0]).strip() if val else ""
    return "" if val is None else str(val).strip()


def _num(val: Any) -> float:
    """数值字段安全转换 (失败返回 0.0)"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _append_note(fields: Dict[str, Any], notes_field: str, note: str) -> str:
    """把说明追加到已有 Notes 之后 (不覆盖历史备注)"""
    existing = str(fields.get(notes_field, "") or "").strip()
    return f"{existing}\n{note}" if existing else note


def _ms_of(val: Any) -> int:
    """把飞书日期字段(毫秒时间戳 / 字符串)解析为毫秒时间戳, 失败返回 0"""
    if not val:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0
        try:
            return int(float(s))
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return int(datetime.strptime(s, fmt).timestamp() * 1000)
            except ValueError:
                continue
    return 0


def resolve_batch_cutoff(days_filter: int = 7, cutoff_ms: Optional[int] = None) -> int:
    """解析"本批"下界(毫秒): 优先运行锚点(上次运行结束), 无锚点回退固定天数; 0 表示不限"""
    if cutoff_ms is not None:
        return cutoff_ms
    try:
        from run_anchor import get_last_run
        anchor = get_last_run()
        cut = int(anchor.get("end") or anchor.get("start") or 0)
        if cut > 0:
            return cut
    except ImportError:
        pass
    if days_filter > 0:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return int((today_start - timedelta(days=days_filter - 1)).timestamp() * 1000)
    return 0


def enforce_final_language_dedup(feishu: FeishuSync = None,
                                 platforms: Tuple[str, ...] = ("bilibili", "youtube"),
                                 threshold: int = SCRIPT_MULTI_AUTHOR_THRESHOLD,
                                 days_filter: int = 7,
                                 cutoff_ms: Optional[int] = None,
                                 dry_run: bool = False) -> Dict[str, Any]:
    """v5.4.4 兜底校验: 按「最终语言(AI 判定)」+「频道」收敛重复入选

    用户口径: 某语言下**通过 AI 审核**的视频总数 > 8 时, 该语言内每个频道只保留 1 条。

    为什么必须单独做这一步(而不是只靠 ScriptDedup):
      ① ScriptDedup 跑在 AI 审核**之前**, 只能读到"视频声明语言"(Language 字段),
         而决定语言归属的是 AI 判定的「语言_Language」—— 两者可能不一致;
      ② 已被标记过的记录不会再被 ScriptDedup 复查(它只处理 Human 为空的候选),
         一旦某轮判断偏差, 后续轮次无法自我纠正;
      ③ 来源视频的优先级翻转 / <8 复活补跑发生在 ScriptDedup **之后**,
         可能把一条已被淘汰的视频又翻回入选。
      三者叠加就会出现"某语言明明 >8, 同一频道却入选多次"。

    统计口径: 该语言下 Human Prescreen ∈ 正向集合 **且** AI 审核报告非空 的记录;
              且必须落在"本批窗口"内(默认=上次运行结束时刻, 无锚点回退 days_filter 天,
              days_filter=0 且无锚点则不限窗口) —— 规则是"每周批内"的约束,
              不能拿它去回改历史各周的产出。
    收敛动作: 同频道按 Total Score 降序(同分按自动编号降序)保留第 1 条,
              其余置为"拒绝"并追加 Notes 说明。

    参数:
        days_filter: 无锚点时的回退窗口天数 (0=不限)
        cutoff_ms:   显式指定本批下界 (毫秒), 优先级最高
        dry_run:     True 时只统计与打印, 不写飞书
    返回:
        {"checked": 纳入统计的记录数, "fixed": 被收敛的记录数, "detail": {平台:语言: 条数}}
    """
    sync = feishu or FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)
    helper = ScriptPrescreen(feishu_sync=sync)   # 复用 _platform_config / _author_of

    cutoff = resolve_batch_cutoff(days_filter=days_filter, cutoff_ms=cutoff_ms)
    if cutoff > 0:
        print(f"[*] 兜底校验窗口: 本批下界={datetime.fromtimestamp(cutoff / 1000):%Y-%m-%d %H:%M}"
              f" (仅校验该时刻之后写入的记录)", flush=True)
    else:
        print("[*] 兜底校验窗口: 不限 (将扫描全表历史)", flush=True)

    checked = 0
    fixed = 0
    detail: Dict[str, int] = {}

    for platform in platforms:
        cfg = helper._platform_config(platform)
        is_bili = cfg["is_bilibili"]
        table_id = cfg["table_id"]
        human_field = BILI_F_HUMAN_PRESCREEN if is_bili else YT_F_HUMAN_PRESCREEN
        report_field = BILI_F_AI_REVIEW_REPORT if is_bili else YT_F_AI_REVIEW_REPORT
        score_field = BILI_F_TOTAL_SCORE if is_bili else YT_F_TOTAL_SCORE
        number_field = BILI_F_AUTO_NUMBER if is_bili else YT_F_AUTO_NUMBER

        print(f"\n[*] [{platform}] 兜底校验: 按「{LANG_FIELD}」+ 频道 收敛 (> {threshold})", flush=True)
        # 写入前置校验: 重新获取字段类型, 确认目标字段存在 (避免写错列)
        types = sync.get_fields(table_id)
        fields_ok = True
        for f_name, f_expect in ((human_field, 3), (cfg["notes_field"], 1)):
            t = types.get(f_name)
            if t is None:
                print(f"    [ABORT] 字段 {f_name!r} 不存在, 跳过 {platform}", flush=True)
                fields_ok = False
                break
            if t != f_expect:
                print(f"    [WARN] 字段 {f_name!r} 类型={t} (期望 {f_expect}), 仍按字符串写入", flush=True)
        if not fields_ok:
            continue
        records = sync.get_records(table_id)

        groups: Dict[str, List[Dict]] = {}
        skipped_by_window = 0
        for rec in records:
            fields = rec.get("fields", {})
            if cutoff > 0 and _ms_of(fields.get(cfg["create_time_field"], 0)) < cutoff:
                skipped_by_window += 1      # 非本批(历史各周)记录 -> 不回改
                continue
            if _norm_select(fields.get(human_field, "")) not in cfg["human_positive_values"]:
                continue
            if not str(fields.get(report_field, "") or "").strip():
                continue          # AI 审核未完成/未产出报告 -> 不纳入统计
            lang = _norm_select(fields.get(LANG_FIELD, ""))
            if not lang and is_bili:
                lang = "中文"      # B站视频语言固定为中文(同 sync 的写入约定)
            if not lang:
                continue          # 语言未判定(如无字幕) -> 无法归属语言组, 不参与
            groups.setdefault(lang, []).append({"record_id": rec.get("record_id"), "fields": fields})
        if skipped_by_window:
            print(f"    (窗口外跳过 {skipped_by_window} 条历史记录)", flush=True)

        for lang, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            checked += len(items)
            if len(items) <= threshold:
                print(f"    {lang}: {len(items)} 条 (<= {threshold}, 不收敛)", flush=True)
                continue

            by_author: Dict[str, List[Dict]] = {}
            for it in items:
                by_author.setdefault(helper._author_of(it, cfg), []).append(it)
            dup = {a: v for a, v in by_author.items() if len(v) > 1}
            print(f"    {lang}: {len(items)} 条 (> {threshold}) | 重复频道 {len(dup)} 个", flush=True)
            if not dup:
                continue

            for author, vids in dup.items():
                vids.sort(key=lambda it: (_num(it["fields"].get(score_field, 0)),
                                          _num(it["fields"].get(number_field, 0))),
                          reverse=True)
                keep = vids[0]
                print(f"      - {author or '(无作者字段)'}: 保留 1 条 "
                      f"({_num(keep['fields'].get(score_field, 0)):.2f} 分, "
                      f"Number={keep['fields'].get(number_field)}), "
                      f"收敛 {len(vids) - 1} 条", flush=True)
                for it in vids[1:]:
                    key = f"{platform}:{lang}"
                    detail[key] = detail.get(key, 0) + 1
                    if dry_run:
                        fixed += 1
                        continue
                    note = (f"Dropped: language group >{threshold} & same channel kept "
                            f"top-score (v5.4.4)")
                    ok = sync.update_record(table_id, it["record_id"], {
                        human_field: cfg["human_rejected"],
                        cfg["notes_field"]: _append_note(it["fields"], cfg["notes_field"], note),
                    })
                    if ok:
                        fixed += 1
                    else:
                        print(f"        [WARN] 写入失败 record_id={it['record_id']}", flush=True)

    mode = "DRY-RUN" if dry_run else "已写入"
    print(f"\n[*] 兜底校验完成 ({mode}): 纳入统计 {checked} 条, 收敛 {fixed} 条, 明细 {detail}", flush=True)
    return {"checked": checked, "fixed": fixed, "detail": detail, "dry_run": dry_run}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="初筛模块 v3.1.1 / 脚本去重 v5.0.0")
    parser.add_argument("--platform", default="bilibili", choices=["bilibili", "youtube"])
    parser.add_argument("--full", action="store_true", help="全量模式（忽略进度）")
    parser.add_argument("--script", action="store_true", help="运行脚本去重 (ScriptPrescreen, v5.0.0)")
    # v5.4.4: 兜底校验 (按最终语言+频道收敛重复入选); --dry-run 只报告不写入
    parser.add_argument("--enforce-lang-dedup", action="store_true",
                        help="运行最终一致性收敛 (v5.4.4): 某语言通过 AI 数 >8 时同频道只留 1 条")
    parser.add_argument("--dry-run", action="store_true", help="配合 --enforce-lang-dedup: 只报告不写入")
    parser.add_argument("--platforms", default="bilibili,youtube",
                        help="配合 --enforce-lang-dedup: 逗号分隔的平台列表")
    parser.add_argument("--days", type=int, default=7,
                        help="配合 --enforce-lang-dedup: 无运行锚点时的回退窗口天数")
    parser.add_argument("--no-window", action="store_true",
                        help="配合 --enforce-lang-dedup: 不限定窗口(扫描全表历史, 慎用)")
    args = parser.parse_args()

    if args.enforce_lang_dedup:
        plats = tuple(p.strip() for p in args.platforms.split(",") if p.strip())
        enforce_final_language_dedup(
            platforms=plats,
            days_filter=0 if args.no_window else args.days,
            cutoff_ms=0 if args.no_window else None,
            dry_run=args.dry_run,
        )
    elif args.script:
        sp = ScriptPrescreen()
        sp.run(platform=args.platform)
    else:
        ps = Prescreen()
        ps.run(platform=args.platform, incremental=not args.full)
