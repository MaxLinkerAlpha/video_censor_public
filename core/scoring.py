#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scoring.py - 智能评分算法模块 (v4.3.0)

v3.9.0 更新：
- YouTube视频启用D2时效分和D3互动分（数据从飞书回填）
- YouTube采用动态维度平均：有数据的维度参与平均，无数据的自动排除
- 两平台评分公式统一为四维（数据完整时）

v3.4.0 更新：
- 新增D4维度：AI吸引力评分
- 调整D3互动得分阈值，增加区分度
- 降低M1新人乘数，避免分数膨胀
- 优化等级划分，提升区分度

评分维度：
- D1: 时长得分（超长惩罚）
- D2: 时效得分（21天外不考虑）
- D3: 互动得分（弹幕+评论+点赞/播放量）
- D4: AI吸引力评分（0-100分映射到0-10分）
- M1: 新人修正（分平台统计）

评分公式：
    B站视频: 内容分 = (D1 + D2 + D3 + D4) / 4
    YouTube视频: 内容分 = 动态维度平均（完整数据时与B站一致，缺失时自动降维）
    最终评分 = 内容分 × M1
"""

import os
import json
import time
import datetime
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from feishu_fields import PRIORITY_WEIGHT


@dataclass
class ScoreResult:
    """评分结果数据类"""
    d1_score: float
    d1_desc: str
    d2_score: float
    d2_desc: str
    d3_score: float
    d3_desc: str
    d4_score: float = 0.0
    d4_desc: str = ""
    m1_multiplier: float = 1.0
    m1_desc: str = "标准评分 (1.0x)"
    content_score: float = 0.0
    final_score: float = 0.0
    grade: str = ""
    recommendation: str = ""
    is_creator: bool = False
    human_priority: str = "normal"
    platform: str = ""


def calc_d1_duration_score(duration_seconds: int) -> Tuple[float, str]:
    """
    D1: 时长得分 (v3.4.0 保持不变)
    
    最佳区间：10-20分钟
    """
    if duration_seconds <= 0:
        return 0.0, "未知时长"

    minutes = duration_seconds / 60

    if minutes < 0.5:
        return 0.0, f"{minutes*60:.0f}秒 (拒绝:过短)"
    if minutes > 45:
        return 0.0, f"{minutes:.0f}分钟 (拒绝:超长)"

    if minutes < 3:
        return 4.0, f"{minutes*60:.0f}秒 (过短)"

    if 10 <= minutes <= 20:
        return 10.0, f"{minutes:.1f}分钟 (最佳)"

    if 5 <= minutes < 10:
        score = 10.0 * (0.92 ** (10 - minutes))
        return round(score, 1), f"{minutes:.1f}分钟 (偏短)"

    if 20 < minutes <= 30:
        score = 10.0 * (0.92 ** (minutes - 20))
        return round(score, 1), f"{minutes:.1f}分钟 (偏长)"

    if 3 <= minutes < 5:
        score = 6.0 * (0.91 ** (5 - minutes))
        return round(score, 1), f"{minutes:.1f}分钟 (过短)"

    if 30 < minutes <= 45:
        score = 6.0 * (0.93 ** (minutes - 30))
        return round(score, 1), f"{minutes:.1f}分钟 (过长)"

    return 0.0, "未知时长"


def calc_d2_freshness_score(publish_timestamp: int) -> Tuple[float, str]:
    """
    D2: 时效得分 (v3.4.0 保持不变)
    """
    if publish_timestamp <= 0:
        return 0.0, "未知发布时间"

    current_time = int(time.time())
    days_ago = (current_time - publish_timestamp) / 86400

    if days_ago < 0:
        return 0.0, "发布时间异常"
    elif days_ago <= 3:
        return 10.0, f"{days_ago:.1f}天前 (最新)"
    elif days_ago <= 7:
        score = 10.0 * (0.86 ** (days_ago - 3))
        return round(score, 1), f"{days_ago:.1f}天前 (次新)"
    elif days_ago <= 14:
        score = 6.0 * (0.92 ** (days_ago - 7))
        return round(score, 1), f"{days_ago:.1f}天前 (近期)"
    elif days_ago <= 21:
        score = 3.0 * (0.91 ** (days_ago - 14))
        return round(score, 1), f"{days_ago:.1f}天前 (较旧)"
    else:
        return 0.0, f"{days_ago:.1f}天前 (不推荐)"


# v5.4.6: 互动率的最小样本门槛
#   互动率 = (点赞+评论+弹幕) / 播放量, 分母过小时比率会失真 ——
#   实测 views=5 且有 1 个赞 就能算出 >10% 的"极高互动" -> D3 直接满分,
#   进而把总分推到 19.5。属于典型的"小样本比率虚高", 必须先设门槛再计算。
MIN_VIEWS_FOR_ENGAGEMENT = 100


def calc_d3_engagement_score(
    views: int,
    likes: int = 0,
    comments: int = 0,
    danmaku: int = 0
) -> Tuple[float, str]:
    """
    D3: 互动得分 (v3.4.0 调整阈值, v5.4.6 增加最小样本门槛)
    
    v3.4.0 调整：
    - 提高各档位阈值，增加区分度
    - 原来>5%就得满分，现在需要>10%
    
    新评分规则：
    - >10%：10 分（极高互动）
    - 7-10%：9 分（高互动）
    - 5-7%：7 分（中高）
    - 3-5%：5 分（中等互动）
    - 1-3%：3 分（低互动）
    - <1%：1 分（极低互动）

    v5.4.6 新增：
    - 观看数 < MIN_VIEWS_FOR_ENGAGEMENT(100) 时**不计互动分**（返回 0）
      避免"5 次观看 + 1 个赞"这类小样本比率虚高
    """
    if views <= 0:
        return 0.0, "无播放数据"
    if views < MIN_VIEWS_FOR_ENGAGEMENT:
        return 0.0, f"样本不足(观看{views} < {MIN_VIEWS_FOR_ENGAGEMENT}), 不计互动分"
    
    total_engagement = likes + comments + danmaku
    engagement_rate = (total_engagement / views) * 100
    
    if engagement_rate > 10.0:
        return 10.0, f"互动率{engagement_rate:.2f}% (极高)"
    elif engagement_rate > 7.0:
        return 9.0, f"互动率{engagement_rate:.2f}% (高)"
    elif engagement_rate > 5.0:
        return 7.0, f"互动率{engagement_rate:.2f}% (中高)"
    elif engagement_rate > 3.0:
        return 5.0, f"互动率{engagement_rate:.2f}% (中等)"
    elif engagement_rate > 1.0:
        return 3.0, f"互动率{engagement_rate:.2f}% (低)"
    else:
        return 1.0, f"互动率{engagement_rate:.2f}% (极低)"


def calc_d4_appeal_score(ai_appeal_score: int) -> Tuple[float, str]:
    """
    D4: AI吸引力评分 (v3.4.0 新增)
    
    将AI评分（0-100）映射到0-10分
    
    映射规则：
    - 90-100: 10分 (极佳)
    - 80-89: 9分 (优秀)
    - 70-79: 8分 (良好)
    - 60-69: 7分 (中上)
    - 50-59: 6分 (中等)
    - 40-49: 5分 (中下)
    - 30-39: 4分 (较差)
    - 20-29: 3分 (差)
    - 10-19: 2分 (很差)
    - 0-9: 1分 (极差)
    
    参数：
        ai_appeal_score: AI吸引力评分（0-100）
    
    返回：
        (得分, 描述)
    """
    if ai_appeal_score >= 90:
        return 10.0, f"AI评分{ai_appeal_score} (极佳)"
    elif ai_appeal_score >= 80:
        return 9.0, f"AI评分{ai_appeal_score} (优秀)"
    elif ai_appeal_score >= 70:
        return 8.0, f"AI评分{ai_appeal_score} (良好)"
    elif ai_appeal_score >= 60:
        return 7.0, f"AI评分{ai_appeal_score} (中上)"
    elif ai_appeal_score >= 50:
        return 6.0, f"AI评分{ai_appeal_score} (中等)"
    elif ai_appeal_score >= 40:
        return 5.0, f"AI评分{ai_appeal_score} (中下)"
    elif ai_appeal_score >= 30:
        return 4.0, f"AI评分{ai_appeal_score} (较差)"
    elif ai_appeal_score >= 20:
        return 3.0, f"AI评分{ai_appeal_score} (差)"
    elif ai_appeal_score >= 10:
        return 2.0, f"AI评分{ai_appeal_score} (很差)"
    else:
        return 1.0, f"AI评分{ai_appeal_score} (极差)"


class SelectionHistory:
    """入选历史管理类"""
    
    def __init__(self, history_file: str = None):
        if history_file is None:
            history_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "data", "selection_history.json"
            )
        self.history_file = history_file
        self.history: Dict[str, Dict[str, Any]] = self._load_history()
    
    def _load_history(self) -> Dict[str, Dict[str, Any]]:
        if not os.path.exists(self.history_file):
            return {"youtube": {}, "bilibili": {}, "last_updated": ""}
        try:
            with open(self.history_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"youtube": {}, "bilibili": {}, "last_updated": ""}
    
    def _save_history(self) -> None:
        self.history["last_updated"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        os.makedirs(os.path.dirname(self.history_file), exist_ok=True)
        with open(self.history_file, 'w', encoding='utf-8') as f:
            json.dump(self.history, f, ensure_ascii=False, indent=2)
    
    def get_selection_count(self, platform: str, author_id: str) -> int:
        platform_key = platform.lower()
        if platform_key not in self.history:
            return 0
        return self.history[platform_key].get(str(author_id), 0)
    
    def increment_selection_count(self, platform: str, author_id: str) -> int:
        platform_key = platform.lower()
        if platform_key not in self.history:
            self.history[platform_key] = {}
        author_key = str(author_id)
        current_count = self.history[platform_key].get(author_key, 0)
        new_count = current_count + 1
        self.history[platform_key][author_key] = new_count
        self._save_history()
        return new_count
    
    def get_all_authors(self, platform: str) -> Dict[str, int]:
        platform_key = platform.lower()
        return self.history.get(platform_key, {})


def calc_m1_newcomer_multiplier(selection_count: int) -> Tuple[float, str]:
    """
    M1: 新人修正系数 (v3.4.0 调整)
    
    v3.4.0 调整：降低乘数幅度，避免分数膨胀
    
    新修正规则：
    - 0 次：1.2x（新博主）
    - 1 次：1.15x（轻度鼓励）
    - 2 次：1.1x（轻微鼓励）
    - 3 次：1.05x（微弱鼓励）
    - 4 次：1.0x（标准评分）
    - 5 次：0.95x（轻微惩罚）
    - >5 次：0.9x（惩罚：避免垄断）
    """
    if selection_count == 0:
        return 1.2, "新博主 (1.2x)"
    elif selection_count == 1:
        return 1.15, "1次入选 (1.15x)"
    elif selection_count == 2:
        return 1.1, "2次入选 (1.1x)"
    elif selection_count == 3:
        return 1.05, "3次入选 (1.05x)"
    elif selection_count == 4:
        return 1.0, "4次入选 (1.0x)"
    elif selection_count == 5:
        return 0.95, "5次入选 (0.95x)"
    else:
        return 0.9, f"{selection_count}次入选 (0.9x)"


def get_score_grade(score: float) -> Tuple[str, str]:
    """
    根据分数获取等级和建议 (v3.4.0 调整)
    
    v3.4.0 调整：优化等级划分，提升区分度
    
    新等级划分：
    - >= 10.0: A 优质 (优先处理)
    - >= 8.0: B 良好 (推荐)
    - >= 6.0: C 一般 (可考虑)
    - >= 4.0: D 较差 (不推荐)
    - < 4.0: E 极差 (拒绝)
    """
    if score >= 10.0:
        return "A 优质", "优先处理"
    elif score >= 8.0:
        return "B 良好", "推荐"
    elif score >= 6.0:
        return "C 一般", "可考虑"
    elif score >= 4.0:
        return "D 较差", "不推荐"
    else:
        return "E 极差", "拒绝"


def calculate_full_score(
    duration_seconds: int,
    publish_timestamp: int,
    views: int,
    likes: int = 0,
    comments: int = 0,
    danmaku: int = 0,
    platform: str = "",
    author_id: str = "",
    selection_history: SelectionHistory = None,
    human_priority: str = "normal",
    ai_appeal_score: int = 0
) -> ScoreResult:
    """
    计算完整评分 (v3.9.0)

    v3.9.0 更新：
    - YouTube视频现在也计算D2时效分和D3互动分（数据从飞书回填）
    - YouTube采用动态维度平均：有数据的维度参与平均，无数据的自动排除
      完整数据时: (D1+D2+D3+D4)/4，与B站一致
      缺失D2/D3时: (D1+D4)/2，向后兼容旧数据

    v3.4.2 更新：
    - AI审查是必选项，没有完成AI审查则评分为0
    - ai_appeal_score <= 0 时，整个评分直接返回0
    - 报告"AI审查未完成"的问题

    v3.4.0 更新：
    - 新增D4 AI吸引力评分
    - B站视频：内容分 = (D1 + D2 + D3 + D4) / 4
    - YouTube视频：内容分 = 动态维度平均（v3.9.0前为(D1+D4)/2）
    - D1=0 时直接排除
    """
    # v3.4.3: 修复AI审查前评分逻辑
    # 先计算D1/D2/D3（这些不依赖AI），D4在AI审查完成前设为"待审查"
    d1_score, d1_desc = calc_d1_duration_score(duration_seconds)
    
    # 如果AI评分尚未完成，D4设为待定状态，但不影响D1-D3的计算
    if ai_appeal_score <= 0:
        d4_score, d4_desc = 0.0, "AI审查待完成"
    else:
        d4_score, d4_desc = calc_d4_appeal_score(ai_appeal_score)

    platform_lower = platform.lower() if platform else ""
    is_youtube = platform_lower == "youtube"

    if is_youtube:
        d2_score, d2_desc = calc_d2_freshness_score(publish_timestamp)
        d3_score, d3_desc = calc_d3_engagement_score(views, likes, comments, danmaku)

        if d1_score == 0:
            content_score = 0.0
        else:
            valid_dims = [d1_score, d4_score]
            if d2_score > 0:
                valid_dims.append(d2_score)
            if d3_score > 0:
                valid_dims.append(d3_score)
            content_score = sum(valid_dims) / len(valid_dims)
    else:
        d2_score, d2_desc = calc_d2_freshness_score(publish_timestamp)
        d3_score, d3_desc = calc_d3_engagement_score(views, likes, comments, danmaku)

        if d1_score == 0 or d2_score == 0:
            content_score = 0.0
        else:
            content_score = (d1_score + d2_score + d3_score + d4_score) / 4

    m1_multiplier = 1.0
    m1_desc = "标准评分 (1.0x)"

    if selection_history and platform and author_id:
        selection_count = selection_history.get_selection_count(platform, author_id)
        m1_multiplier, m1_desc = calc_m1_newcomer_multiplier(selection_count)

    final_score = content_score * m1_multiplier
    grade, recommendation = get_score_grade(final_score)

    return ScoreResult(
        d1_score=d1_score,
        d1_desc=d1_desc,
        d2_score=d2_score,
        d2_desc=d2_desc,
        d3_score=d3_score,
        d3_desc=d3_desc,
        d4_score=d4_score,
        d4_desc=d4_desc,
        m1_multiplier=m1_multiplier,
        m1_desc=m1_desc,
        content_score=content_score,
        final_score=final_score,
        grade=grade,
        recommendation=recommendation,
        is_creator=False,
        human_priority=human_priority,
        platform=platform_lower
    )


def filter_and_rank_videos(
    video_list: List[Dict[str, Any]],
    selection_history: SelectionHistory = None,
    max_videos: int = 8,
    max_per_author: int = 1
) -> List[Dict[str, Any]]:
    """
    筛选和排序视频 (v3.4.0)

    筛选规则：
    1. D1=0 → 直接排除（超长/超短视频）
    2. B站视频D2=0 → 直接排除（超旧视频）
    3. YouTube视频不检查D2
    4. 每个博主最多保留 max_per_author 个视频
    5. 按综合分降序排列
    6. 最多返回 max_videos 个视频
    """
    def get_composite_score(video: Dict) -> float:
        score_result = video.get('score_result')
        if not score_result:
            return 0.0
        priority = getattr(score_result, 'human_priority', 'normal') or 'normal'
        priority_weight = PRIORITY_WEIGHT.get(priority, 0.0)
        return score_result.final_score + priority_weight

    valid_videos = []

    for video in video_list:
        score_result = video.get('score_result')
        if not score_result:
            continue
        
        if score_result.d1_score == 0:
            continue
        
        is_youtube = getattr(score_result, 'platform', '').lower() == 'youtube'
        
        if not is_youtube and score_result.d2_score == 0:
            continue
        
        valid_videos.append(video)

    valid_videos.sort(key=get_composite_score, reverse=True)

    author_count: Dict[str, int] = {}
    selected_videos = []

    for video in valid_videos:
        author_id = video.get('author_id', '')
        platform = video.get('platform', '').lower()
        author_key = f"{platform}:{author_id}"

        current_count = author_count.get(author_key, 0)

        if current_count < max_per_author:
            selected_videos.append(video)
            author_count[author_key] = current_count + 1

        if len(selected_videos) >= max_videos:
            break

    return selected_videos


if __name__ == "__main__":
    print("=" * 60)
    print("评分系统测试 (v3.4.0)")
    print("=" * 60)
    
    print("\n[D1 时长得分测试]")
    test_durations = [120, 180, 300, 600, 900, 1200, 1800, 2400, 3600]
    for sec in test_durations:
        score, desc = calc_d1_duration_score(sec)
        print(f"  {sec//60}分钟: {score}分 - {desc}")
    
    print("\n[D2 时效得分测试]")
    current_time = int(time.time())
    test_days = [1, 5, 10, 15, 20, 25, 30]
    for days in test_days:
        ts = current_time - days * 86400
        score, desc = calc_d2_freshness_score(ts)
        print(f"  {days}天前: {score}分 - {desc}")
    
    print("\n[D3 互动得分测试 (v3.4.0 新阈值)]")
    test_cases = [
        (10000, 1200, 200, 100),  # 15%
        (10000, 800, 150, 80),    # 10.3%
        (10000, 500, 100, 50),    # 6.5%
        (10000, 300, 50, 30),     # 3.8%
        (10000, 100, 20, 10),     # 1.3%
        (10000, 50, 10, 5),       # 0.65%
    ]
    for views, likes, comments, danmaku in test_cases:
        score, desc = calc_d3_engagement_score(views, likes, comments, danmaku)
        rate = (likes + comments + danmaku) / views * 100
        print(f"  互动率{rate:.1f}%: {score}分 - {desc}")
    
    print("\n[D4 AI吸引力评分测试]")
    test_ai_scores = [95, 85, 75, 65, 55, 45, 35, 25, 15, 5]
    for ai_score in test_ai_scores:
        score, desc = calc_d4_appeal_score(ai_score)
        print(f"  AI评分{ai_score}: {score}分 - {desc}")
    
    print("\n[M1 新人修正测试 (v3.4.0 新乘数)]")
    for count in [0, 1, 2, 3, 4, 5, 6, 10]:
        multiplier, desc = calc_m1_newcomer_multiplier(count)
        print(f"  {count}次入选: {desc}")
    
    print("\n[完整评分测试 - B站视频]")
    result = calculate_full_score(
        duration_seconds=900,
        publish_timestamp=current_time - 5 * 86400,
        views=10000,
        likes=500,
        comments=100,
        danmaku=50,
        platform="bilibili",
        author_id="test_author_123",
        ai_appeal_score=85
    )
    print(f"  D1: {result.d1_score}分 ({result.d1_desc})")
    print(f"  D2: {result.d2_score}分 ({result.d2_desc})")
    print(f"  D3: {result.d3_score}分 ({result.d3_desc})")
    print(f"  D4: {result.d4_score}分 ({result.d4_desc})")
    print(f"  M1: {result.m1_desc}")
    print(f"  内容分: {result.content_score:.2f}")
    print(f"  最终分: {result.final_score:.2f}")
    print(f"  等级: {result.grade}")
    
    print("\n[完整评分测试 - YouTube视频]")
    result_yt = calculate_full_score(
        duration_seconds=1200,
        publish_timestamp=0,
        views=0,
        platform="youtube",
        author_id="yt_test_123",
        ai_appeal_score=85
    )
    print(f"  D1: {result_yt.d1_score}分 ({result_yt.d1_desc})")
    print(f"  D2: {result_yt.d2_score}分 ({result_yt.d2_desc})")
    print(f"  D3: {result_yt.d3_score}分 ({result_yt.d3_desc})")
    print(f"  D4: {result_yt.d4_score}分 ({result_yt.d4_desc})")
    print(f"  M1: {result_yt.m1_desc}")
    print(f"  内容分: {result_yt.content_score:.2f}")
    print(f"  最终分: {result_yt.final_score:.2f}")
    print(f"  等级: {result_yt.grade}")
    
    print("\n" + "=" * 60)
    print("[OK] 评分系统测试完成")
