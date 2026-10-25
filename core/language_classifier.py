#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
language_classifier.py - 字幕语言 AI 分类模块 (v5.3.5)

功能：
1. classify_language_code(code): 纯函数, 将 BCP-47/ISO 语言代码映射为 10 个分类名之一
   (中文/英文/德文/波兰文/日文/韩文/法文/俄文/西班牙文/其它); 空/None 返回 None
2. classify_by_ai(subtitle_text, title, hint_code): 调用智谱 glm-4-flash 对字幕文本
   做 AI 语言分类, 3 次重试, AI 结果为准(即使与 hint_code 不同);
   AI 全部失败时回退 classify_language_code(hint_code)
3. --verify: 抽样验证模式 —— 从飞书 YT 表取 3 条不同 Language 的记录,
   走 fetch字幕 -> classify_by_ai -> 仅更新这 3 条的 语言_Language 字段

AI 凭证：从 add-on/.env 读取 AI_API_KEY / AI_BASE_URL (智谱 openai 兼容端点)

使用方法：
    from language_classifier import classify_language_code, classify_by_ai
    cat = classify_by_ai(subtitle_text[:1500], title, "en-US")

    # 自检 (映射用例 + mock, 不发起真实 AI 请求)
    python core/language_classifier.py

    # 抽样验证 (真实请求, 仅写 3 条)
    python core/language_classifier.py --verify
"""

import os
import sys
import json
import time
from typing import Optional, List, Tuple, Dict, Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

# 分类名 SSOT: 与 feishu_fields.LANG_OPTIONS 保持一致 (feishu_fields 为唯一数据源)
from feishu_fields import (
    LANG_FIELD,
    LANG_CN, LANG_EN, LANG_DE, LANG_PL, LANG_JA, LANG_KO,
    LANG_FR, LANG_RU, LANG_ES, LANG_OTHER,
    LANG_OPTIONS,
    FIELD_TYPE_SELECT,
)

# openai SDK 可用性探测 (智谱提供 openai 兼容端点)
try:
    from openai import OpenAI, OpenAIError
    _OPENAI_AVAILABLE = True
except ImportError:
    OpenAI = None  # type: ignore[assignment]
    OpenAIError = Exception  # type: ignore[assignment,misc]
    _OPENAI_AVAILABLE = False

# add-on/.env 显式路径 (AI_API_KEY / AI_BASE_URL 所在)
ADDON_ENV_PATH = os.path.join(PROJECT_ROOT, "add-on", ".env")

AI_MODEL = "glm-4-flash"
AI_MAX_SUBTITLE_CHARS = 1500
AI_RETRY_TIMES = 3
AI_RETRY_WAIT_SECONDS = 2.0

# 验证模式: 可选的固定样本 (Language原值, Video ID)。
# 留空时由 --verify 在运行时从 YT 表按不同 Language 原值自动抽样 3 条。
VERIFY_SAMPLES: List[Tuple[str, str]] = []

# 主语言前缀 -> 分类名 (zh/zh-*/cmn/yue 归中文; 其余按主前缀匹配)
_MAIN_CODE_MAP: Dict[str, str] = {
    "zh": LANG_CN, "cmn": LANG_CN, "yue": LANG_CN,
    "en": LANG_EN,
    "de": LANG_DE,
    "pl": LANG_PL,
    "ja": LANG_JA,
    "ko": LANG_KO,
    "fr": LANG_FR,
    "ru": LANG_RU,
    "es": LANG_ES,
}

# AI 返回值别名归一 (AI 可能返回英文名/语言代码/中文别名)
_AI_ALIAS_MAP: Dict[str, str] = {
    "chinese": LANG_CN, "mandarin": LANG_CN, "zh": LANG_CN, "中文": LANG_CN,
    "english": LANG_EN, "en": LANG_EN, "英语": LANG_EN, "英文": LANG_EN,
    "german": LANG_DE, "de": LANG_DE, "德语": LANG_DE, "德文": LANG_DE,
    "polish": LANG_PL, "pl": LANG_PL, "波兰语": LANG_PL, "波兰文": LANG_PL,
    "japanese": LANG_JA, "ja": LANG_JA, "日语": LANG_JA, "日文": LANG_JA,
    "korean": LANG_KO, "ko": LANG_KO, "韩语": LANG_KO, "韩文": LANG_KO,
    "french": LANG_FR, "fr": LANG_FR, "法语": LANG_FR, "法文": LANG_FR,
    "russian": LANG_RU, "ru": LANG_RU, "俄语": LANG_RU, "俄文": LANG_RU,
    "spanish": LANG_ES, "es": LANG_ES, "西班牙语": LANG_ES, "西班牙文": LANG_ES,
    "other": LANG_OTHER, "others": LANG_OTHER, "unknown": LANG_OTHER,
    "其它": LANG_OTHER, "其他": LANG_OTHER,
}

_CLASSIFY_SYSTEM_PROMPT = (
    "你是一个多语言文本分类器。用户会提供视频字幕片段(可能被截断)、视频标题和字幕语言代码提示。"
    f"你必须判断这段字幕最可能属于以下{len(LANG_OPTIONS)}个分类中的哪一个: {', '.join(LANG_OPTIONS)}。"
    "只输出一个严格的JSON对象, 格式: {\"language\": \"<分类名>\"}, "
    "不要输出任何其他文字、解释或代码块标记。"
)

# OpenAI 兼容客户端单例 (线程安全: OpenAI 客户端可被多线程共享)
_ai_client: Optional[Any] = None


# ============================================================
# 多源语言仲裁 (v5.3.7, 多重兜底第 2/3 层)
# ============================================================

def arbitrate_language(hint_cls: str, ai_cls: str, channel_hist_cls: str) -> str:
    """多源语言仲裁 (纯函数, 输入均为 10 分类名或空串, 返回分类名或空串)

    证据源优先级语义 (v5.3.7 教训: 字幕轨可能挂错语言, AI 文本分类未必代表内容语言):
      - channel_hist_cls  频道历史主导语言 —— 频道语言高度稳定, 最强信号
      - hint_cls          视频声明音频语言(defaultAudioLanguage)推断 —— UP 主自声明
      - ai_cls            字幕文本 AI 分类 —— 仅基于可能挂错轨的文本

    仲裁规则:
      1. 有效证据 2:1 多数胜
      2. 三者互异 -> 频道历史 > hint > AI
      3. 两证据冲突 -> 含频道历史则频道历史胜; 否则 hint 胜 AI
      4. 全空 -> 空串 (调用方自行兜底)
    """
    votes = [v for v in (str(hint_cls or "").strip(),
                         str(ai_cls or "").strip(),
                         str(channel_hist_cls or "").strip()) if v]
    if not votes:
        return ""
    if len(set(votes)) == 1:
        return votes[0]

    if len(votes) == 3:
        # 2:1 多数胜
        if hint_cls == ai_cls:
            return hint_cls
        if hint_cls == channel_hist_cls:
            return hint_cls
        if ai_cls == channel_hist_cls:
            return ai_cls
        # 三者互异 -> 频道历史 > hint > AI
        return channel_hist_cls or hint_cls or ai_cls

    # 两证据且冲突: 频道历史 > hint > AI
    if channel_hist_cls:
        return channel_hist_cls
    return hint_cls or ai_cls


def _get_ai_client() -> Any:
    """惰性创建智谱 openai 兼容客户端 (凭证来自 add-on/.env)"""
    global _ai_client
    if _ai_client is not None:
        return _ai_client

    # 显式加载 add-on/.env; override=False: 进程环境变量优先, .env 值仅补充缺失项
    # (与 discord_video_mark.py 的读取顺序语义一致: 进程环境变量 > add-on/.env)
    try:
        from dotenv import load_dotenv
        load_dotenv(ADDON_ENV_PATH, override=False)
    except ImportError:
        print("[WARN] python-dotenv 未安装, 仅从进程环境变量读取 AI_API_KEY")

    api_key = os.getenv("AI_API_KEY", "")
    base_url = os.getenv("AI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
    if not api_key:
        raise ValueError(f"缺少 AI_API_KEY (已尝试加载: {ADDON_ENV_PATH})")

    # timeout=10s: 1500字分类任务10s富余, 减少AI拥塞时对抓取线程的阻塞
    # (max_retries=0 关闭SDK内部重试, 上层 classify_by_ai 自带3次重试)
    _ai_client = OpenAI(api_key=api_key, base_url=base_url, timeout=10.0, max_retries=0)
    return _ai_client


def classify_language_code(code: Optional[str]) -> Optional[str]:
    """纯函数: 语言代码 -> 10 分类名之一; 空/None -> None

    映射规则:
      zh/zh-*/cmn/yue -> 中文
      en*   -> 英文    de* -> 德文    pl  -> 波兰文
      ja    -> 日文    ko  -> 韩文    fr* -> 法文
      ru*   -> 俄文    es*/es-419 -> 西班牙文
      zxx/未知 -> 其它  空/None -> None
    """
    if code is None:
        return None
    code = str(code).strip()
    if not code:
        return None
    main = code.lower().split("-")[0].split("_")[0]
    return _MAIN_CODE_MAP.get(main, LANG_OTHER)


def _normalize_ai_answer(raw: str) -> str:
    """解析 AI 返回文本中的 {\"language\": \"...\"} 并归一为合法分类名; 失败返回空串"""
    text = (raw or "").strip()
    if not text:
        return ""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return ""
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return ""
    value = str(data.get("language", "") or "").strip()
    if not value:
        return ""
    if value in LANG_OPTIONS:
        return value
    return _AI_ALIAS_MAP.get(value.lower(), _AI_ALIAS_MAP.get(value, ""))


def _call_glm(user_content: str) -> str:
    """调用智谱 glm-4-flash, 返回原始回复文本"""
    client = _get_ai_client()
    resp = client.chat.completions.create(
        model=AI_MODEL,
        messages=[
            {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.0,
        max_tokens=32,
    )
    return str(resp.choices[0].message.content or "")


def classify_by_ai(subtitle_text: str, title: str = "", hint_code: str = "") -> str:
    """AI 语言分类: 输入字幕前1500字+标题+hint_code, 返回 10 分类名之一 (永不抛异常)

    - AI 结果为准 (即使与 hint_code 推断不同)
    - 3 次重试, 每次失败等待 2s
    - AI 全部失败时回退 classify_language_code(hint_code) (hint 也为空则归 其它)
    """
    subtitle_text = (subtitle_text or "")[:AI_MAX_SUBTITLE_CHARS]
    user_content = (
        f"字幕语言代码提示(仅供参考, 可能缺失或不准确): {hint_code or '无'}\n"
        f"视频标题: {title or '无'}\n"
        f"字幕前{AI_MAX_SUBTITLE_CHARS}字符:\n{subtitle_text or '(空)'}"
    )

    if not _OPENAI_AVAILABLE:
        print("[WARN] openai 库未安装, 语言分类回退到代码映射")
        return classify_language_code(hint_code) or LANG_OTHER

    last_err = ""
    for attempt in range(1, AI_RETRY_TIMES + 1):
        try:
            raw = _call_glm(user_content)
            parsed = _normalize_ai_answer(raw)
            if parsed:
                return parsed
            last_err = f"AI返回无法解析为合法分类: {raw[:80]}"
        except (OpenAIError, OSError, ValueError, KeyError, TypeError,
                IndexError, AttributeError) as e:
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
        print(f"  [WARN] 语言AI分类第{attempt}/{AI_RETRY_TIMES}次失败: {last_err}")
        if attempt < AI_RETRY_TIMES:
            time.sleep(AI_RETRY_WAIT_SECONDS)

    fallback = classify_language_code(hint_code) or LANG_OTHER
    print(f"  [WARN] 语言AI分类全部失败, 回退代码映射: {hint_code or '(空)'} -> {fallback}")
    return fallback


# ============================================================
# 自检 (映射用例 + mock, 不发起真实 AI 请求)
# ============================================================

_SELF_TEST_CASES: List[Tuple[Optional[str], Optional[str]]] = [
    ("zh", LANG_CN), ("zh-CN", LANG_CN), ("zh-TW", LANG_CN),
    ("cmn-Hant", LANG_CN), ("yue", LANG_CN), ("ZH", LANG_CN),
    ("en", LANG_EN), ("en-US", LANG_EN), ("en-GB", LANG_EN),
    ("de", LANG_DE), ("de-DE", LANG_DE),
    ("pl", LANG_PL), ("pl-PL", LANG_PL),
    ("ja", LANG_JA), ("jp", LANG_OTHER),
    ("ko", LANG_KO),
    ("fr", LANG_FR), ("fr-CA", LANG_FR),
    ("ru", LANG_RU), ("ru-RU", LANG_RU),
    ("es", LANG_ES), ("es-419", LANG_ES), ("es-MX", LANG_ES),
    ("zxx", LANG_OTHER), ("pt", LANG_OTHER), ("pt-PT", LANG_OTHER),
    ("it", LANG_OTHER), ("uk", LANG_OTHER), ("nl", LANG_OTHER),
    ("", None), (None, None), ("   ", None),
]


def _self_check() -> int:
    """自检入口: 纯函数映射用例 + classify_by_ai mock 测试"""
    print("=" * 60)
    print("language_classifier 自检 (映射用例 + mock)")
    print("=" * 60)

    failed = 0
    for code, expected in _SELF_TEST_CASES:
        got = classify_language_code(code)
        status = "OK" if got == expected else "FAIL"
        if got != expected:
            failed += 1
        print(f"  [{status}] classify_language_code({code!r}) = {got!r} (期望 {expected!r})")

    # mock 测试 1: AI 正常返回 JSON -> 以 AI 结果为准 (与 hint 不同也采纳)
    import language_classifier as lc

    original_call = lc._call_glm
    try:
        lc._call_glm = lambda content: '{"language": "英文"}'
        got = lc.classify_by_ai("hello world this is english", "Test", "de")
        expect = LANG_EN
        status = "OK" if got == expect else "FAIL"
        if got != expect:
            failed += 1
        print(f"  [{status}] mock AI返回英文/de提示 -> {got!r} (AI结果为准, 期望 {expect!r})")

        # mock 测试 2: AI 返回需别名归一的值
        lc._call_glm = lambda content: '{"language": "Spanish"}'
        got = lc.classify_by_ai("hola amigos", "Test", "es-MX")
        expect = LANG_ES
        status = "OK" if got == expect else "FAIL"
        if got != expect:
            failed += 1
        print(f"  [{status}] mock AI返回Spanish -> {got!r} (别名归一, 期望 {expect!r})")

        # mock 测试 3: AI 持续失败 -> 回退 classify_language_code(hint_code)
        def _always_fail(content: str) -> str:
            raise OSError("模拟网络错误")
        lc._call_glm = _always_fail
        got = lc.classify_by_ai("whatever", "Test", "ru-RU")
        expect = LANG_RU
        status = "OK" if got == expect else "FAIL"
        if got != expect:
            failed += 1
        print(f"  [{status}] mock AI持续失败 -> 回退 {got!r} (期望 {expect!r})")

        # mock 测试 4: AI 失败且 hint 为空 -> 其它
        got = lc.classify_by_ai("whatever", "Test", "")
        expect = LANG_OTHER
        status = "OK" if got == expect else "FAIL"
        if got != expect:
            failed += 1
        print(f"  [{status}] mock AI失败+hint为空 -> {got!r} (期望 {expect!r})")

    finally:
        lc._call_glm = original_call

    # v5.3.7: arbitrate_language 多源仲裁用例 (纯函数, 无 AI 请求)
    # (hint, ai, hist, 期望, 说明)
    arb_cases = [
        (LANG_DE, LANG_EN, LANG_DE, LANG_DE, "bug场景: hint+频道历史 2:1 压过 AI 误判"),
        (LANG_DE, LANG_EN, "", LANG_DE, "hint 压过 AI (无频道历史)"),
        ("", LANG_EN, LANG_DE, LANG_DE, "频道历史压过 AI"),
        (LANG_EN, LANG_EN, LANG_DE, LANG_EN, "hint+AI 2:1 压过频道历史 (UP 换语言)"),
        (LANG_DE, LANG_EN, LANG_EN, LANG_EN, "AI+频道历史 2:1 压过 hint (UP 乱标)"),
        (LANG_DE, LANG_EN, LANG_RU, LANG_RU, "三者互异 -> 频道历史胜"),
        ("", LANG_EN, LANG_RU, LANG_RU, "两证据冲突 -> 频道历史胜"),
        ("", LANG_EN, "", LANG_EN, "仅 AI 有效"),
        (LANG_EN, LANG_EN, "", LANG_EN, "hint+AI 一致"),
        ("", "", "", "", "全空 -> 空串"),
    ]
    for hint, ai, hist, expect, desc in arb_cases:
        got = lc.arbitrate_language(hint, ai, hist)
        status = "OK" if got == expect else "FAIL"
        if got != expect:
            failed += 1
        print(f"  [{status}] arbitrate({hint!r}, {ai!r}, {hist!r}) = {got!r} (期望 {expect!r}) — {desc}")

    print("=" * 60)
    if failed == 0:
        print("[OK] 自检全部通过")
        return 0
    print(f"[FAIL] 自检失败 {failed} 项")
    return 1


# ============================================================
# 抽样验证 (--verify): 仅更新 3 条硬编码视频, 禁止批量回填
# ============================================================

def run_verify() -> int:
    """抽样验证: fetch字幕 -> classify_by_ai -> 仅更新 3 条的语言_Language 字段"""
    from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID
    from platforms.youtube_client import fetch_youtube_subtitle

    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)

    # 规则: 写入前重新拉取表结构, 确认字段存在与类型
    print(f"[*] 拉取 YT 表字段结构...")
    fields = sync.get_fields(YT_TABLE_ID)
    if not fields:
        print("[ERROR] 无法获取 YT 表字段结构, 中止验证")
        return 1
    if LANG_FIELD not in fields:
        actual_type = fields.get(LANG_FIELD)
        print(f"[INFO] YT 表缺少 {LANG_FIELD} 字段 (若已存在请检查类型: {actual_type}), 正在创建 select 字段...")
        if not sync.create_field(YT_TABLE_ID, LANG_FIELD, FIELD_TYPE_SELECT, {"options": LANG_OPTIONS}):
            print("[ERROR] 字段创建失败, 中止验证")
            return 1

    print(f"[*] 建立 Video ID -> record 索引...")
    records = sync.get_records(YT_TABLE_ID)
    id_map: Dict[str, Dict] = {}
    for rec in records:
        vid = str(rec.get("fields", {}).get("Video ID", "") or "").strip()
        if vid:
            id_map[vid] = rec

    verified: List[Tuple[str, str, str, str, str]] = []  # (vid, 原Language, 语言代码, AI判定, record_id)

    # 未指定固定样本时, 按不同 Language 原值从表内各取 1 条
    samples: List[Tuple[str, str]] = list(VERIFY_SAMPLES)
    if not samples:
        seen_lang: set = set()
        for rec in records:
            _f = rec.get("fields", {})
            _vid = str(_f.get("Video ID", "") or "").strip()
            _lang = str(_f.get("Language", "") or "").strip() or "(空)"
            if _vid and _lang not in seen_lang:
                seen_lang.add(_lang)
                samples.append((_lang, _vid))
            if len(samples) >= 3:
                break
        print(f"[*] 自动抽样 {len(samples)} 条 (不同 Language 原值)")

    for expect_lang, vid in samples:
        rec = id_map.get(vid)
        if rec is None:
            print(f"[WARN] 抽样视频 {vid} (期望Language={expect_lang}) 不在 YT 表中, 跳过")
            continue
        orig_lang = str(rec["fields"].get("Language", "") or "").strip() or "(空)"
        print(f"\n[FETCH] {vid} (Language原值: {orig_lang}) 获取字幕中...")
        data, err = fetch_youtube_subtitle(vid)
        if err or not data or not data.get("has_subtitle"):
            print(f"  [SKIP] 字幕获取失败: {err or 'no subtitle'}")
            continue
        subtitle = str(data.get("subtitle_text", "") or "")
        lang_code = str(data.get("subtitle_language", "") or "")
        print(f"  字幕长度: {len(subtitle)} 字符 | 语言代码: {lang_code or '(无)'}")
        ai_cls = classify_by_ai(subtitle[:AI_MAX_SUBTITLE_CHARS], str(data.get("title", "") or ""), lang_code)
        print(f"  [AI判定] {ai_cls}  <-->  [Language原值] {orig_lang}  (hint代码: {lang_code or '(无)'})")
        verified.append((vid, orig_lang, lang_code, ai_cls, str(rec.get("record_id", ""))))

    if not verified:
        print("[ERROR] 没有任何抽样视频完成分类, 无写入")
        return 1

    print(f"\n[*] 仅写回 {len(verified)} 条抽样记录的 {LANG_FIELD} 字段...")
    ok_count = 0
    for vid, orig_lang, lang_code, ai_cls, record_id in verified:
        if record_id and sync.update_record(YT_TABLE_ID, record_id, {LANG_FIELD: ai_cls}):
            ok_count += 1
            print(f"  [OK] {vid}: {LANG_FIELD}={ai_cls} (Language原值: {orig_lang})")
        else:
            print(f"  [FAIL] {vid}: 写回失败")

    print("\n" + "=" * 60)
    print(f"[SUMMARY] 抽样验证完成: {ok_count}/{len(verified)} 条写回 (仅3条, 未批量回填)")
    for vid, orig_lang, lang_code, ai_cls, _ in verified:
        print(f"  {vid}: AI={ai_cls} | Language原值={orig_lang} | 代码={lang_code or '(无)'}")
    print("=" * 60)
    return 0 if ok_count == len(verified) else 1


if __name__ == "__main__":
    if "--verify" in sys.argv:
        sys.exit(run_verify())
    sys.exit(_self_check())
