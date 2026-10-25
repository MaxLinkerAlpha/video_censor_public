#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reporter.py - 报告生成与输出模块 (v4.3.0)

功能：
- 生成 Markdown 格式的审查报告
- 打印终端汇总信息
- 打印单个视频评分结果

使用方法：
    from reporter import print_summary, write_report

    write_report(results, "report.md")
    print_summary(results)
"""

import os
import datetime
from typing import List, Dict

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scoring import get_score_grade as scoring_get_grade
from feishu_fields import (
    BILI_GRADE_A, BILI_GRADE_B, BILI_GRADE_C, BILI_GRADE_D, BILI_GRADE_E,
)


def print_video_result(result_info: Dict) -> None:
    """打印视频评分结果"""
    scores = result_info.get('scores', {})

    print(f"\n{'='*60}")
    print(f"评分结果 (v3.4.0 新评分系统):")
    print(f"   D1 时长: {scores.get('d1', {}).get('score', 0):.1f}分 ({scores.get('d1', {}).get('desc', '')})")
    print(f"   D2 时效: {scores.get('d2', {}).get('score', 0):.1f}分 ({scores.get('d2', {}).get('desc', '')})")
    print(f"   D3 互动: {scores.get('d3', {}).get('score', 0):.1f}分 ({scores.get('d3', {}).get('desc', '')})")
    d4 = scores.get('d4', {})
    if d4:
        print(f"   D4 吸引: {d4.get('score', 0):.1f}分 ({d4.get('desc', '')})")
    print(f"   M1 新人: {scores.get('m1', {}).get('desc', '')}")
    print(f"   ----------------------")
    print(f"   内容分: {scores.get('content_score', 0):.2f}分")
    print(f"   最终分: {scores.get('total', 0):.2f}分 {scores.get('grade', '')}")
    print(f"   建议: {scores.get('recommendation', '')}")

    ai_appeal = scores.get('ai_appeal')
    if ai_appeal:
        print(f"\nAI吸引力评分: {ai_appeal.get('score', 0)}分 ({ai_appeal.get('desc', '')})")
        appeal_details = ai_appeal.get('details', {})
        if appeal_details:
            print(f"   游戏相关性: {appeal_details.get('game_relevance', '-')}")
            print(f"   信息价值: {appeal_details.get('info_value', '-')}")
            print(f"   目标受众: {appeal_details.get('target_audience', '-')}")
            print(f"   创新性: {appeal_details.get('innovation', '-')}")
            print(f"   可理解性: {appeal_details.get('clarity', '-')}")

    if result_info.get('has_subtitle') and result_info.get('censor_result'):
        print(f"\n安全审查:")
        censor_text = result_info['censor_result']
        print(censor_text[:300])
        if len(censor_text) > 300:
            print("...")
    print(f"{'='*60}")


def print_summary(results: List[Dict]) -> None:
    """打印汇总报告"""
    total = len(results)
    success = sum(1 for r in results if r.get('success'))
    failed = total - success

    valid_scores = []
    for r in results:
        if r.get('success') and r.get('scores') and 'total' in r['scores']:
            valid_scores.append(r['scores']['total'])

    no_subtitle = sum(1 for r in results if r.get('success') and not r.get('has_subtitle'))

    print(f"\n{'='*60}")
    print(f"批量处理完成 - 汇总报告 (v3.0.0)")
    print(f"{'='*60}")
    print(f"  总计: {total} 个视频")
    print(f"  成功: {success} 个")
    print(f"  失败: {failed} 个")

    if no_subtitle > 0:
        print(f"  WARN 无字幕: {no_subtitle} 个")

    if valid_scores:
        avg_score = sum(valid_scores) / len(valid_scores)
        grade, _ = scoring_get_grade(avg_score)
        print(f"\n评分统计:")
        print(f"  平均分: {avg_score:.2f}分 {grade}")

        excellent = sum(1 for s in valid_scores if s >= 10.0)
        good = sum(1 for s in valid_scores if 8.0 <= s < 10.0)
        normal = sum(1 for s in valid_scores if 6.0 <= s < 8.0)
        poor = sum(1 for s in valid_scores if s < 6.0)

        print(f"  {BILI_GRADE_A} 优质(>=10): {excellent}个")
        print(f"  {BILI_GRADE_B} 良好(8-10): {good}个")
        print(f"  {BILI_GRADE_C} 一般(6-8): {normal}个")
        print(f"  {BILI_GRADE_D} 较差(<6): {poor}个")

    if failed > 0:
        print(f"\n失败列表:")
        for r in results:
            if not r.get('success'):
                print(f"   - {r.get('input')}: {r.get('error', '未知错误')}")

    print(f"{'='*60}")


def write_report(results: List[Dict], report_path: str) -> None:
    """生成审查报告"""
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("# 视频内容审查报告 (v3.0.0)\n\n")
        f.write(f"**生成时间**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"**处理数量**: {len(results)} 个视频\n\n")

        valid_scores = [r['scores'].get('total', 0) for r in results if r.get('success') and r.get('scores') and 'total' in r['scores']]
        if valid_scores:
            avg_score = sum(valid_scores) / len(valid_scores)
            f.write(f"**平均评分**: {avg_score:.2f}分\n\n")

        f.write("---\n\n")

        for i, r in enumerate(results, 1):
            title = r.get('title', r.get('input', '未知'))
            scores = r.get('scores', {})

            f.write(f"## {i}. {title}\n\n")

            if scores:
                grade = scores.get('grade', '')
                total = scores.get('total', 0)
                f.write(f"**综合评分**: {total:.2f}分 {grade}\n\n")

            f.write(f"| 维度 | 分数 | 说明 |\n")
            f.write(f"|------|------|\n")

            if scores:
                f.write(f"| D1 时长 | {scores.get('d1', {}).get('score', 0):.1f} | {scores.get('d1', {}).get('desc', '')} |\n")
                f.write(f"| D2 时效 | {scores.get('d2', {}).get('score', 0):.1f} | {scores.get('d2', {}).get('desc', '')} |\n")
                f.write(f"| D3 互动 | {scores.get('d3', {}).get('score', 0):.1f} | {scores.get('d3', {}).get('desc', '')} |\n")
                f.write(f"| M1 新人 | - | {scores.get('m1', {}).get('desc', '')} |\n")
                f.write(f"| 内容分 | {scores.get('content_score', 0):.2f} | |\n")
                if r.get('_is_split'):
                    f.write(f"| 拆分 | - | 字幕过长已拆分为多段审查 |\n")

            ai_appeal = scores.get('ai_appeal')
            if ai_appeal:
                f.write(f"\n**AI吸引力评分**: {ai_appeal.get('score', 0)}分 ({ai_appeal.get('desc', '')})\n")
                appeal_details = ai_appeal.get('details', {})
                if appeal_details:
                    f.write(f"\n**吸引力详情**: 游戏相关性-{appeal_details.get('game_relevance', '-')} | ")
                    f.write(f"信息价值-{appeal_details.get('info_value', '-')} | ")
                    f.write(f"目标受众-{appeal_details.get('target_audience', '-')} | ")
                    f.write(f"创新性-{appeal_details.get('innovation', '-')} | ")
                    f.write(f"可理解性-{appeal_details.get('clarity', '-')}\n")

            f.write(f"\n- **原始输入**: `{r.get('input', '')}`\n")
            f.write(f"- **平台**: {r.get('platform', '未知')}\n")
            f.write(f"- **状态**: {'OK 成功' if r.get('success') else 'ERROR 失败'}\n")

            if r.get('error'):
                f.write(f"- **错误**: {r.get('error')}\n")

            if r.get('censor_result'):
                f.write(f"\n**安全审查结果**:\n\n{r.get('censor_result')}\n")

            f.write("\n---\n\n")
