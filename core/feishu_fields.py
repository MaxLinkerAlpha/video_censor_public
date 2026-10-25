#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu_fields.py - 飞书字段名与字段值唯一数据源 (SSOT)

用途：集中定义 B站/YouTube 飞书表格的所有字段名、类型、选项值，
     消除项目中散落的硬编码字段名和魔法字符串。
使用方法：from feishu_fields import BILI_F_PRESCREEN, bili_value, ...
"""

from typing import Any, Dict, Optional


FIELD_TYPE_TEXT = 1
FIELD_TYPE_NUMBER = 2
FIELD_TYPE_SELECT = 3
FIELD_TYPE_CHECKBOX = 4
FIELD_TYPE_DATETIME = 5
FIELD_TYPE_CREATED_AT = 6
FIELD_TYPE_AUTO_NUMBER = 7
FIELD_TYPE_FORMULA = 8


# ============================================================
# 语言分类字段 (v5.3.5) - B站/YouTube 两表共用
# ============================================================

LANG_FIELD = "语言_Language"
LANG_CN = "中文"
LANG_EN = "英文"
LANG_DE = "德文"
LANG_PL = "波兰文"
LANG_JA = "日文"
LANG_KO = "韩文"
LANG_FR = "法文"
LANG_RU = "俄文"
LANG_ES = "西班牙文"
LANG_OTHER = "其它"
# 10 个固定选项 (select 字段, 写入时传 NAME 不传 ID)
LANG_OPTIONS: list = [LANG_CN, LANG_EN, LANG_DE, LANG_PL, LANG_JA, LANG_KO, LANG_FR, LANG_RU, LANG_ES, LANG_OTHER]


# ============================================================
# B站字段定义
# ============================================================

BILI_FIELDS: Dict[str, Dict[str, Any]] = {
    "初筛状态": {"type": FIELD_TYPE_SELECT, "options": ["pending", "通过", "拒绝"]},
    "字幕获取状态": {"type": FIELD_TYPE_SELECT, "options": ["pending", "成功", "失败"]},
    "AI审核结果": {"type": FIELD_TYPE_SELECT, "options": ["通过", "不通过"]},
    "人工初筛": {"type": FIELD_TYPE_SELECT, "options": ["高优先级", "中优先级", "低优先级", "入选", "拒绝"]},
    "评分等级": {"type": FIELD_TYPE_SELECT, "options": ["A", "B", "C", "D", "E"]},
    "D1时长分": {"type": FIELD_TYPE_NUMBER},
    "D2时效分": {"type": FIELD_TYPE_NUMBER},
    "D3互动分": {"type": FIELD_TYPE_NUMBER},
    "D4吸引力分": {"type": FIELD_TYPE_NUMBER},
    "综合评分": {"type": FIELD_TYPE_NUMBER},
    "BV号": {"type": FIELD_TYPE_TEXT},
    "UP主": {"type": FIELD_TYPE_SELECT},
    "标题": {"type": FIELD_TYPE_TEXT},
    "字幕文本": {"type": FIELD_TYPE_TEXT},
    "备注": {"type": FIELD_TYPE_TEXT},
    "播放量(k)": {"type": FIELD_TYPE_NUMBER},
    "播放量(万)": {"type": FIELD_TYPE_NUMBER},
    "播放量(原数据)": {"type": FIELD_TYPE_TEXT},
    "弹幕数": {"type": FIELD_TYPE_TEXT},
    "时长(MM:SS)": {"type": FIELD_TYPE_TEXT},
    "发布日期": {"type": FIELD_TYPE_DATETIME},
    "写入时间": {"type": FIELD_TYPE_CREATED_AT},
    "序号": {"type": FIELD_TYPE_AUTO_NUMBER},
    "去重": {"type": FIELD_TYPE_FORMULA},
    "链接": {"type": FIELD_TYPE_TEXT},
    "完整链接": {"type": FIELD_TYPE_FORMULA},
    "人工确认": {"type": FIELD_TYPE_CHECKBOX},
    LANG_FIELD: {"type": FIELD_TYPE_SELECT, "options": LANG_OPTIONS},
}


# ============================================================
# YouTube字段定义
# ============================================================

YT_FIELDS: Dict[str, Dict[str, Any]] = {
    "Prescreen Status": {
        "type": FIELD_TYPE_SELECT,
        "options": ["Pending", "Passed", "Rejected", "拒绝", "通过"],
        "note": "MIXED_LANG: 包含英文和中文选项，历史遗留",
    },
    "Subtitle Status": {"type": FIELD_TYPE_SELECT, "options": ["pending", "success", "failed"]},
    "AI Review Result": {"type": FIELD_TYPE_SELECT, "options": ["safe", "unsafe", "rejected"]},
    "Human Prescreen": {
        "type": FIELD_TYPE_SELECT,
        "options": ["High Priority", "Medium Priority", "Low Priority", "Selected", "Rejected"],
    },
    "Rating": {"type": FIELD_TYPE_SELECT, "options": ["A", "B", "C", "D", "E"]},
    "D1 Duration": {"type": FIELD_TYPE_NUMBER},
    "D2 Timeliness": {"type": FIELD_TYPE_NUMBER},
    "D3 Engagement": {"type": FIELD_TYPE_NUMBER},
    "D4 Appeal": {"type": FIELD_TYPE_NUMBER},
    "Total Score": {"type": FIELD_TYPE_NUMBER},
    "Video ID": {"type": FIELD_TYPE_TEXT},
    "Channel": {"type": FIELD_TYPE_SELECT},
    "Title": {"type": FIELD_TYPE_TEXT},
    "字幕文本": {"type": FIELD_TYPE_TEXT},
    "Notes": {"type": FIELD_TYPE_TEXT},
    "Description": {"type": FIELD_TYPE_TEXT},
    "Comments": {"type": FIELD_TYPE_TEXT},
    "Tags": {"type": FIELD_TYPE_TEXT},
    "Duration": {"type": FIELD_TYPE_TEXT},
    "Duration (Min)": {"type": FIELD_TYPE_TEXT},
    "Views": {"type": FIELD_TYPE_TEXT},
    "Likes": {"type": FIELD_TYPE_TEXT},
    "Published Date": {"type": FIELD_TYPE_DATETIME},
    "Published At": {"type": FIELD_TYPE_TEXT},
    "Link": {"type": FIELD_TYPE_TEXT},
    "Channel ID": {"type": FIELD_TYPE_TEXT},
    "Has Captions": {"type": FIELD_TYPE_TEXT},
    "Live Recording": {"type": FIELD_TYPE_TEXT},
    "Language": {"type": FIELD_TYPE_SELECT},
    "Engagement Rate": {"type": FIELD_TYPE_TEXT},
    "Date Created": {"type": FIELD_TYPE_CREATED_AT},
    "Number": {"type": FIELD_TYPE_AUTO_NUMBER},
    "Manual Confirm": {"type": FIELD_TYPE_SELECT},
    "备注": {"type": FIELD_TYPE_TEXT},
    "标题翻译": {"type": FIELD_TYPE_TEXT},
    "count": {"type": FIELD_TYPE_NUMBER},
    "人工确认": {"type": FIELD_TYPE_SELECT},
    "字幕获取状态": {
        "type": FIELD_TYPE_SELECT,
        "options": ["pending", "success", "failed"],
        "deprecated": True,
        "note": "DEPRECATED: 重复字段，请使用 Subtitle Status",
    },
    "AI审核结果": {
        "type": FIELD_TYPE_SELECT,
        "options": ["safe", "unsafe", "pending"],
        "deprecated": True,
        "note": "DEPRECATED: 重复字段，请使用 AI Review Result",
    },
    "初筛状态": {
        "type": FIELD_TYPE_SELECT,
        "options": ["pending", "通过", "拒绝"],
        "deprecated": True,
        "note": "DEPRECATED: 重复字段，请使用 Prescreen Status",
    },
    "评分等级": {
        "type": FIELD_TYPE_SELECT,
        "options": ["A", "B", "C", "D"],
        "deprecated": True,
        "note": "DEPRECATED: 重复字段，请使用 Rating",
    },
    LANG_FIELD: {"type": FIELD_TYPE_SELECT, "options": LANG_OPTIONS},
}


# ============================================================
# B站字段名常量
# ============================================================

BILI_F_PRESCREEN = "初筛状态"
BILI_F_SUBTITLE_STATUS = "字幕获取状态"
BILI_F_AI_REVIEW = "AI审核结果"
BILI_F_AI_REVIEW_REPORT = "AI审核报告"
BILI_F_HUMAN_PRESCREEN = "人工初筛"
BILI_F_RATING = "评分等级"
BILI_F_D1 = "D1时长分"
BILI_F_D2 = "D2时效分"
BILI_F_D3 = "D3互动分"
BILI_F_D4 = "D4吸引力分"
BILI_F_TOTAL_SCORE = "综合评分"
BILI_F_BVID = "BV号"
BILI_F_UPLOADER = "UP主"
BILI_F_TITLE = "标题"
BILI_F_SUBTITLE_TEXT = "字幕文本"
BILI_F_NOTES = "备注"
BILI_F_VIEWS_K = "播放量(k)"
BILI_F_VIEWS_W = "播放量(万)"
BILI_F_VIEWS_RAW = "播放量(原数据)"
BILI_F_DANMAKU = "弹幕数"
BILI_F_DURATION = "时长(MM:SS)"
BILI_F_PUBLISH_DATE = "发布日期"
BILI_F_CREATED_AT = "写入时间"
BILI_F_AUTO_NUMBER = "序号"
BILI_F_DEDUP = "去重"
BILI_F_LINK = "链接"
BILI_F_FULL_LINK = "完整链接"
BILI_F_HUMAN_CONFIRM = "人工确认"


# ============================================================
# YouTube字段名常量
# ============================================================

YT_F_PRESCREEN = "Prescreen Status"
YT_F_SUBTITLE_STATUS = "Subtitle Status"
YT_F_AI_REVIEW = "AI Review Result"
YT_F_AI_REVIEW_REPORT = "AI Review Report"
YT_F_HUMAN_PRESCREEN = "Human Prescreen"
YT_F_RATING = "Rating"
YT_F_D1 = "D1 Duration"
YT_F_D2 = "D2 Timeliness"
YT_F_D3 = "D3 Engagement"
YT_F_D4 = "D4 Appeal"
YT_F_TOTAL_SCORE = "Total Score"
YT_F_VIDEO_ID = "Video ID"
YT_F_CHANNEL = "Channel"
YT_F_TITLE = "Title"
YT_F_SUBTITLE_TEXT = "字幕文本"
YT_F_NOTES = "Notes"
YT_F_DESCRIPTION = "Description"
YT_F_COMMENTS = "Comments"
YT_F_TAGS = "Tags"
YT_F_DURATION = "Duration"
YT_F_DURATION_MIN = "Duration (Min)"
YT_F_VIEWS = "Views"
YT_F_LIKES = "Likes"
YT_F_PUBLISH_DATE = "Published Date"
YT_F_PUBLISHED_AT = "Published At"
YT_F_LINK = "Link"
YT_F_CHANNEL_ID = "Channel ID"
YT_F_HAS_CAPTIONS = "Has Captions"
YT_F_LIVE_RECORDING = "Live Recording"
YT_F_LANGUAGE = "Language"
YT_F_ENGAGEMENT_RATE = "Engagement Rate"
YT_F_CREATED_AT = "Date Created"
YT_F_AUTO_NUMBER = "Number"
YT_F_MANUAL_CONFIRM = "Manual Confirm"


# ============================================================
# B站字段值常量 - 初筛状态
# ============================================================

BILI_PRESCREEN_PENDING = "pending"
BILI_PRESCREEN_PASS = "通过"
BILI_PRESCREEN_REJECT = "拒绝"

# B站字段值常量 - 字幕获取状态
BILI_SUBTITLE_PENDING = "pending"
BILI_SUBTITLE_SUCCESS = "成功"
BILI_SUBTITLE_FAILED = "失败"

# B站字段值常量 - AI审核结果
BILI_AI_RESULT_PASS = "通过"
BILI_AI_RESULT_FAIL = "不通过"

# B站字段值常量 - 人工初筛
BILI_HUMAN_HIGH = "高优先级"
BILI_HUMAN_MEDIUM = "中优先级"
BILI_HUMAN_LOW = "低优先级"
BILI_HUMAN_SELECTED = "入选"
BILI_HUMAN_REJECTED = "拒绝"

# B站字段值常量 - 评分等级
BILI_GRADE_A = "A"
BILI_GRADE_B = "B"
BILI_GRADE_C = "C"
BILI_GRADE_D = "D"
BILI_GRADE_E = "E"


# ============================================================
# YouTube字段值常量 - Prescreen Status
# ============================================================

YT_PRESCREEN_PENDING = "Pending"
YT_PRESCREEN_PASS = "Passed"
YT_PRESCREEN_REJECT = "Rejected"
YT_PRESCREEN_REJECT_CN = "拒绝"
YT_PRESCREEN_PASS_CN = "通过"

# YouTube字段值常量 - Subtitle Status
YT_SUBTITLE_PENDING = "pending"
YT_SUBTITLE_SUCCESS = "success"
YT_SUBTITLE_FAILED = "failed"

# YouTube字段值常量 - AI Review Result
YT_AI_RESULT_SAFE = "safe"
YT_AI_RESULT_UNSAFE = "unsafe"
YT_AI_RESULT_REJECTED = "rejected"

# YouTube字段值常量 - Human Prescreen
YT_HUMAN_HIGH = "High Priority"
YT_HUMAN_MEDIUM = "Medium Priority"
YT_HUMAN_LOW = "Low Priority"
YT_HUMAN_SELECTED = "Selected"
YT_HUMAN_REJECTED = "Rejected"

# YouTube字段值常量 - Rating
YT_GRADE_A = "A"
YT_GRADE_B = "B"
YT_GRADE_C = "C"
YT_GRADE_D = "D"
YT_GRADE_E = "E"


# ============================================================
# 值映射函数
# ============================================================

_BILI_VALUE_MAP: Dict[str, Dict[str, str]] = {
    BILI_F_PRESCREEN: {
        "pending": BILI_PRESCREEN_PENDING,
        "pass": BILI_PRESCREEN_PASS,
        "reject": BILI_PRESCREEN_REJECT,
    },
    BILI_F_SUBTITLE_STATUS: {
        "pending": BILI_SUBTITLE_PENDING,
        "success": BILI_SUBTITLE_SUCCESS,
        "failed": BILI_SUBTITLE_FAILED,
    },
    BILI_F_AI_REVIEW: {
        "pass": BILI_AI_RESULT_PASS,
        "fail": BILI_AI_RESULT_FAIL,
    },
    BILI_F_HUMAN_PRESCREEN: {
        "high": BILI_HUMAN_HIGH,
        "medium": BILI_HUMAN_MEDIUM,
        "low": BILI_HUMAN_LOW,
        "selected": BILI_HUMAN_SELECTED,
        "rejected": BILI_HUMAN_REJECTED,
    },
    BILI_F_RATING: {
        "A": BILI_GRADE_A,
        "B": BILI_GRADE_B,
        "C": BILI_GRADE_C,
        "D": BILI_GRADE_D,
        "E": BILI_GRADE_E,
    },
}

_YT_VALUE_MAP: Dict[str, Dict[str, str]] = {
    YT_F_PRESCREEN: {
        "pending": YT_PRESCREEN_PENDING,
        "pass": YT_PRESCREEN_PASS,
        "reject": YT_PRESCREEN_REJECT,
    },
    YT_F_SUBTITLE_STATUS: {
        "pending": YT_SUBTITLE_PENDING,
        "success": YT_SUBTITLE_SUCCESS,
        "failed": YT_SUBTITLE_FAILED,
    },
    YT_F_AI_REVIEW: {
        "safe": YT_AI_RESULT_SAFE,
        "unsafe": YT_AI_RESULT_UNSAFE,
        "rejected": YT_AI_RESULT_REJECTED,
    },
    YT_F_HUMAN_PRESCREEN: {
        "high": YT_HUMAN_HIGH,
        "medium": YT_HUMAN_MEDIUM,
        "low": YT_HUMAN_LOW,
        "selected": YT_HUMAN_SELECTED,
        "rejected": YT_HUMAN_REJECTED,
    },
    YT_F_RATING: {
        "A": YT_GRADE_A,
        "B": YT_GRADE_B,
        "C": YT_GRADE_C,
        "D": YT_GRADE_D,
        "E": YT_GRADE_E,
    },
}


def bili_value(field_name: str, logic_value: str) -> str:
    if field_name in _BILI_VALUE_MAP:
        mapping = _BILI_VALUE_MAP[field_name]
        if logic_value in mapping:
            return mapping[logic_value]
    return logic_value


def yt_value(field_name: str, logic_value: str) -> str:
    if field_name in _YT_VALUE_MAP:
        mapping = _YT_VALUE_MAP[field_name]
        if logic_value in mapping:
            return mapping[logic_value]
    return logic_value


def map_censor_result_to_bili(censor_result: str) -> str:
    if not censor_result:
        return BILI_AI_RESULT_FAIL
    lower = censor_result.lower()
    if "unsafe" in lower or "fail" in lower or "reject" in lower:
        return BILI_AI_RESULT_FAIL
    if "ok" in lower or "safe" in lower or "pass" in lower:
        return BILI_AI_RESULT_PASS
    return BILI_AI_RESULT_FAIL


def map_censor_result_to_yt(censor_result: str) -> str:
    if not censor_result:
        return YT_AI_RESULT_UNSAFE
    lower = censor_result.lower()
    if "unsafe" in lower or "fail" in lower or "reject" in lower:
        return YT_AI_RESULT_UNSAFE
    if "ok" in lower or "safe" in lower or "pass" in lower:
        return YT_AI_RESULT_SAFE
    return YT_AI_RESULT_UNSAFE


def map_subtitle_status_to_bili(has_subtitle: bool) -> str:
    return BILI_SUBTITLE_SUCCESS if has_subtitle else BILI_SUBTITLE_FAILED


def map_subtitle_status_to_yt(has_subtitle: bool) -> str:
    return YT_SUBTITLE_SUCCESS if has_subtitle else YT_SUBTITLE_FAILED


# ============================================================
# 优先级权重映射
# ============================================================

PRIORITY_WEIGHT: Dict[str, float] = {
    "高优先级": 10.0,
    "中优先级": 5.0,
    "低优先级": 2.0,
    "High Priority": 10.0,
    "Medium Priority": 5.0,
    "Low Priority": 2.0,
    "normal": 0.0,
    "": 0.0,
}


# ============================================================
# 必需字段列表 (用于字段验证)
# ============================================================

BILI_REQUIRED_FIELDS: list = [
    BILI_F_PRESCREEN, BILI_F_HUMAN_PRESCREEN, BILI_F_SUBTITLE_STATUS,
    BILI_F_SUBTITLE_TEXT, BILI_F_D1, BILI_F_D2, BILI_F_D3, BILI_F_D4,
    BILI_F_TOTAL_SCORE, BILI_F_RATING, BILI_F_AI_REVIEW, BILI_F_HUMAN_CONFIRM,
    BILI_F_NOTES, BILI_F_BVID, BILI_F_TITLE,
]

YT_REQUIRED_FIELDS: list = [
    YT_F_PRESCREEN, YT_F_HUMAN_PRESCREEN, YT_F_SUBTITLE_STATUS,
    YT_F_D1, YT_F_D2, YT_F_D3, YT_F_D4,
    YT_F_TOTAL_SCORE, YT_F_RATING, YT_F_AI_REVIEW,
    YT_F_VIDEO_ID, YT_F_TITLE, YT_F_LINK,
]
