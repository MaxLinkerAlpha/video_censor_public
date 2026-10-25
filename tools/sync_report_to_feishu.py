#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_report_to_feishu.py - 从Total_Review_Report.md同步结果到飞书 (v1.0)

功能:
1. 解析 Total_Review_Report.md 提取所有视频的评分和审核结果
2. 使用修复后的真实字段名写入飞书表格
3. 支持断点续传 (记录已同步的视频ID)
4. 降低并发避免触发频率限制

使用方法:
    python sync_report_to_feishu.py              # 同步全部
    python sync_report_to_feishu.py --dry-run     # 仅预览不同步
    python sync_report_to_feishu.py --resume      # 断点续传(跳过已成功)
"""

import os
import sys
import re
import json
import time
import threading
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 路径配置：tools/ 目录下的脚本需要访问 core/, platforms/ 等
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)  # 项目根目录
sys.path.insert(0, PROJECT_ROOT)            # 添加根目录以找到 core/, platforms/
sys.path.insert(0, os.path.join(PROJECT_ROOT, "core"))  # 添加core目录

from feishu_sync import (
    FeishuSync,
    BILI_BITABLE_TOKEN,
    BILI_TABLE_ID,
    YT_TABLE_ID
)
from feishu_fields import (
    BILI_F_SUBTITLE_STATUS, BILI_F_D1, BILI_F_D2, BILI_F_D3, BILI_F_D4,
    BILI_F_TOTAL_SCORE, BILI_F_RATING, BILI_F_AI_REVIEW, BILI_F_AI_REVIEW_REPORT,
    YT_F_SUBTITLE_STATUS, YT_F_D1, YT_F_D2, YT_F_D3, YT_F_D4,
    YT_F_TOTAL_SCORE, YT_F_RATING, YT_F_AI_REVIEW, YT_F_AI_REVIEW_REPORT,
    BILI_SUBTITLE_SUCCESS, BILI_SUBTITLE_FAILED,
    BILI_AI_RESULT_PASS, BILI_AI_RESULT_FAIL,
    BILI_PRESCREEN_PENDING,
    YT_SUBTITLE_SUCCESS, YT_SUBTITLE_FAILED,
    YT_AI_RESULT_SAFE, YT_AI_RESULT_UNSAFE,
    YT_SUBTITLE_PENDING,
)


class ReportParser:
    """Markdown 报告解析器"""

    @staticmethod
    def parse_report(report_path: str) -> List[Dict]:
        """
        解析 Total_Review_Report.md

        返回:
            视频结果列表，每项包含:
            {
                'video_id': str,
                'platform': str ('bilibili'/'youtube'),
                'title': str,
                'url': str,
                'total_score': float,
                'grade': str,
                'd1_score': float,
                'd2_score': float,
                'censor_result': str,
                'success': bool,
            }
        """
        with open(report_path, 'r', encoding='utf-8') as f:
            content = f.read()

        results = []
        
        # 按视频块分割 (## 数字. 标题)
        blocks = re.split(r'\n## \d+\. ', content)
        
        for block in blocks:
            if not block.strip():
                continue
            
            result = ReportParser._parse_video_block(block)
            if result and result.get('video_id'):
                results.append(result)

        return results

    @staticmethod
    def _parse_video_block(block: str) -> Optional[Dict]:
        """解析单个视频块"""
        lines = block.split('\n')

        # 初始化结果字典
        result = {
            'success': False,
            'has_subtitle': False,
            'scores': {},
            'censor_result': '',
        }

        # 提取标题 (第一行)
        title_line = lines[0].strip() if lines else ''
        result['title'] = title_line[:50]

        # 提取URL
        url_match = re.search(r'https?://[^\s\)]+', block)
        url = url_match.group(0) if url_match else ''
        result['url'] = url

        # 提取平台 (从标题或明确标记)
        platform_match = re.search(r'\((BILIBILI|YOUTUBE)\)', title_line)
        if not platform_match:
            platform_match = re.search(r'平台:\s*(BILIBILI|YOUTUBE)', block, re.IGNORECASE)

        if platform_match:
            plat_str = platform_match.group(1).upper()
            result['platform'] = 'bilibili' if plat_str == 'BILIBILI' else 'youtube'
        elif 'bilibili.com' in url.lower():
            result['platform'] = 'bilibili'
        elif 'youtube.com' in url.lower() or 'youtu.be' in url.lower():
            result['platform'] = 'youtube'
        else:
            return None  # 无法识别平台的跳过

        # 提取 video_id
        vid_match = re.search(r'视频ID:\s*([\w\-]+)', block)
        if vid_match:
            result['video_id'] = vid_match.group(1)
        elif result['platform'] == 'bilibili':
            bv_match = re.search(r'BV[\w]{10}', url)
            result['video_id'] = bv_match.group(0) if bv_match else ''
        else:
            yt_match = re.search(r'[?&]v=([\w\-]{11})', url)
            if yt_match:
                result['video_id'] = yt_match.group(1)
            else:
                # 从 Video ID 行提取
                vid_line_match = re.search(r'Video ([\w\-]{11})', title_line)
                result['video_id'] = vid_line_match.group(1) if vid_line_match else ''

        # 检查是否失败
        status_match = re.search(r'状态:\s*(ERROR|OK)', block)
        if status_match and status_match.group(1) == 'ERROR':
            error_match = re.search(r'错误:\s*(.+)', block)
            result['error'] = error_match.group(1).strip() if error_match else '未知错误'
            return result  # 失败记录也返回，但 success=False

        # 标记为成功
        result['success'] = True
        result['has_subtitle'] = True

        # 提取综合评分 (格式: **综合评分**: 7.98分 B 良好)
        score_match = re.search(r'\*\*综合评分\*\*:\s*([\d.]+)\s*分\s*([A-E])', block)
        if score_match:
            total_score = float(score_match.group(1))
            grade = score_match.group(2)
            result['scores'] = {
                'total': total_score,
                'final_score': total_score,
                'grade': grade,
            }

            # 提取 D1 D2 分数 (从表格行)
            d1_match = re.search(r'D1.*?\|\s*([\d.]+)分?', block)
            d2_match = re.search(r'D2.*?\|\s*([\d.]+)分?', block)

            result['scores']['d1'] = {'score': float(d1_match.group(1)) if d1_match else 0}
            result['scores']['d2'] = {'score': float(d2_match.group(1)) if d2_match else 0}

        # 提取审核结果
        if '[OK 全文安全]' in block or '未发现核心违规' in block:
            result['censor_result'] = '[OK] safe'
        elif '违规' in block or '[FAIL' in block or 'unsafe' in block:
            result['censor_result'] = '[FAIL] unsafe'
        elif 'WARN 该视频无字幕' in block or '无法进行内容审核' in block:
            result['censor_result'] = '[FAIL] unsafe'
        else:
            result['censor_result'] = '[OK] safe'

        return result


class FeishuSyncFromReport:
    """基于报告的飞书同步器"""

    def __init__(self):
        self.feishu = FeishuSync()
        self.feishu.set_bitable_token(BILI_BITABLE_TOKEN)
        
        self.sync_log_file = os.path.join(SCRIPT_DIR, "data", ".feishu_sync_log.json")
        self._load_sync_log()
        
        self.stats = {"total": 0, "success": 0, "failed": 0, "skipped": 0}
        self._lock = threading.Lock()

    def _load_sync_log(self) -> None:
        """加载同步日志（用于断点续传）"""
        self.synced_ids = set()
        
        if os.path.exists(self.sync_log_file):
            try:
                with open(self.sync_log_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.synced_ids = set(data.get("synced_ids", []))
                    print(f"[INFO] 已加载同步日志: {len(self.synced_ids)} 条历史记录")
            except Exception as e:
                print(f"[WARN] 加载同步日志失败: {e}")

    def _save_sync_log(self, new_id: str = None) -> None:
        """保存同步日志"""
        if new_id:
            self.synced_ids.add(new_id)
        
        try:
            with open(self.sync_log_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "last_updated": datetime.now().isoformat(),
                    "synced_ids": list(self.synced_ids),
                    "stats": self.stats,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] 保存同步日志失败: {e}")

    def build_updates(self, r: Dict) -> Optional[Dict[str, Any]]:
        """构建更新数据（使用修复后的真实字段名）"""
        platform = r.get('platform', '').lower()
        
        if platform == "bilibili":
            updates = {
                BILI_F_SUBTITLE_STATUS: BILI_SUBTITLE_SUCCESS if r.get('has_subtitle') else BILI_SUBTITLE_FAILED,
            }

            scores = r.get('scores', {})
            if scores:
                d1_data = scores.get('d1', {})
                d2_data = scores.get('d2', {})
                d3_data = scores.get('d3', {})
                d4_data = scores.get('d4', {})
                updates[BILI_F_D1] = round(d1_data.get('score', 0), 2)
                updates[BILI_F_D2] = round(d2_data.get('score', 0), 2)
                updates[BILI_F_D3] = round(d3_data.get('score', 0), 2)
                updates[BILI_F_D4] = round(d4_data.get('score', 0), 2)
                updates[BILI_F_TOTAL_SCORE] = round(scores.get('total', scores.get('final_score', 0)), 2)

                grade = scores.get('grade', 'D')
                if grade:
                    for g in ['A', 'B', 'C', 'D', 'E']:
                        if g in str(grade):
                            updates[BILI_F_RATING] = g
                            break

            censor = r.get('censor_result', '')
            if '[OK' in censor or 'safe' in censor:
                updates[BILI_F_AI_REVIEW] = BILI_AI_RESULT_PASS
            elif 'unsafe' in censor or 'FAIL' in censor:
                updates[BILI_F_AI_REVIEW] = BILI_AI_RESULT_FAIL
            else:
                updates[BILI_F_AI_REVIEW] = BILI_PRESCREEN_PENDING
            # 写入完整AI审核报告（文本类型）
            if censor:
                updates[BILI_F_AI_REVIEW_REPORT] = censor

        elif platform == "youtube":
            updates = {
                YT_F_SUBTITLE_STATUS: YT_SUBTITLE_SUCCESS if r.get('has_subtitle') else YT_SUBTITLE_FAILED,
            }
            
            scores = r.get('scores', {})
            if scores:
                d1_data = scores.get('d1', {})
                d2_data = scores.get('d2', {})
                d3_data = scores.get('d3', {})
                d4_data = scores.get('d4', {})

                updates[YT_F_D1] = round(d1_data.get('score', 0), 2)
                updates[YT_F_D2] = round(d2_data.get('score', 0), 2)
                updates[YT_F_D3] = round(d3_data.get('score', 0), 2)
                updates[YT_F_D4] = round(d4_data.get('score', 0), 2)
                updates[YT_F_TOTAL_SCORE] = round(scores.get('final_score', scores.get('total', 0)), 2)
                
                grade = scores.get('grade', 'D')
                if grade:
                    for g in ['A', 'B', 'C', 'D', 'E']:
                        if g in str(grade):
                            updates[YT_F_RATING] = g
                            break
            
            censor = r.get('censor_result', '')
            if '[OK' in censor or 'safe' in censor:
                updates[YT_F_AI_REVIEW] = YT_AI_RESULT_SAFE
            elif 'unsafe' in censor or 'FAIL' in censor:
                updates[YT_F_AI_REVIEW] = YT_AI_RESULT_UNSAFE
            else:
                updates[YT_F_AI_REVIEW] = YT_SUBTITLE_PENDING
            # 写入完整AI审核报告（文本类型）
            if censor:
                updates[YT_F_AI_REVIEW_REPORT] = censor
        else:
            return None

        return updates if updates else None

    def sync_one(self, r: Dict, skip_existing: bool = False) -> Tuple[bool, str, str]:
        """同步单条记录"""
        video_id = r.get('video_id', '')
        platform = r.get('platform', '')
        title = r.get('title', '')[:30]

        if not video_id or not platform:
            return False, video_id, platform

        # 断点续传: 跳过已同步的
        if skip_existing and video_id in self.synced_ids:
            with self._lock:
                self.stats["skipped"] += 1
            return True, video_id, "skipped"

        # 只同步成功的记录
        if not r.get('success'):
            return False, video_id, platform

        updates = self.build_updates(r)
        if not updates:
            return False, video_id, platform

        # 执行更新
        success = self.feishu.update_video_record(video_id, platform, updates)

        with self._lock:
            self.stats["total"] += 1
            if success:
                self.stats["success"] += 1
                self._save_sync_log(video_id)
                status_str = "[OK]"
            else:
                self.stats["failed"] += 1
                status_str = "[FAIL]"

        print(f"  {status_str} [{platform.upper():6s}] {title} [{video_id}]")

        return success, video_id, platform

    def run_sync(
        self,
        report_path: str,
        max_concurrent: int = 5,
        delay: float = 0.5,
        dry_run: bool = False,
        resume: bool = False
    ) -> bool:
        """
        执行同步

        参数:
            report_path: 报告文件路径
            max_concurrent: 并发数
            delay: 请求间隔
            dry_run: 仅预览不实际执行
            resume: 断点续传模式
        """
        print("\n" + "=" * 70)
        print("  从 Total_Review_Report.md 同步结果到飞书 v1.0")
        print("=" * 70 + "\n")

        # 解析报告
        print(f"[*] 解析报告: {report_path}")
        records = ReportParser.parse_report(report_path)
        
        success_records = [r for r in records if r.get('success')]
        failed_records = [r for r in records if not r.get('success')]
        
        print(f"[*] 总记录: {len(records)} | 成功: {len(success_records)} | 失败: {len(failed_records)}")
        
        if dry_run:
            print("\n*** DRY RUN 模式 - 不实际写入 ***\n")
            for r in success_records[:5]:
                print(f"  [PREVIEW] {r.get('title', '')[:40]}... [{r.get('video_id')}]")
            if len(success_records) > 5:
                print(f"  ... 还有 {len(success_records)-5} 条")
            return True

        if resume:
            print(f"[*] 断点续传模式 - 已跳过: {len(self.synced_ids)} 条历史记录\n")

        start_time = time.time()

        # 执行并发同步
        print(f"[*] 开始同步 {len(success_records)} 条记录 (并发:{max_concurrent}, 间隔:{delay}s)...\n")

        with ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="report_sync") as executor:
            futures = {}
            for i, r in enumerate(success_records, 1):
                if i > 1:
                    time.sleep(delay)
                
                future = executor.submit(self.sync_one, r, skip_existing=resume)
                futures[future] = r

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"  [ERROR] 异常: {str(e)[:50]}")

        elapsed = time.time() - start_time

        # 打印统计
        stats = self.stats
        print(f"\n{'=' * 70}")
        print(f"  同步完成统计")
        print(f"{'=' * 70}")
        print(f"\n  总计: {stats['total']} 条")
        print(f"  成功: {stats['success']} 条")
        print(f"  失败: {stats['failed']} 条")
        if resume:
            print(f"  跳过: {stats['skipped']} 条 (已同步)")
        print(f"\n  耗时: {elapsed:.1f}秒 ({elapsed/60:.1f}分钟)")
        print(f"\n{'=' * 70}\n")

        return stats["failed"] == 0


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="从报告同步结果到飞书 v1.0")
    parser.add_argument("--report", type=str,
                        default=os.path.join(SCRIPT_DIR, "core", "Total_Review_Report.md"),
                        help="报告文件路径")
    parser.add_argument("--concurrent", type=int, default=5, help="并发数")
    parser.add_argument("--delay", type=float, default=0.5, help="请求间隔")
    parser.add_argument("--dry-run", action="store_true", help="仅预览不执行")
    parser.add_argument("--resume", action="store_true", help="断点续传模式")
    
    args = parser.parse_args()

    syncer = FeishuSyncFromReport()
    
    success = syncer.run_sync(
        report_path=args.report,
        max_concurrent=args.concurrent,
        delay=args.delay,
        dry_run=args.dry_run,
        resume=args.resume
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
