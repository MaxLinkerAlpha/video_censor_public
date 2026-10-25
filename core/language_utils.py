#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
language_utils.py - 语言代码归一化工具 (v5.1.0)

将 BCP-47 标签 (e.g. en-US, de-DE) 映射为 ISO 639-1 二字符代码 (e.g. en, de),
保证飞书 Language 字段中主流语言只有一个标识符。

设计动机 (v5.1.0):
- 旧版 fetcher 写入飞书的是 BCP-47 (en-US, de-DE, ja-JP, ...), 导致:
  * 同一语言多种变体各占一个 select 选项 (UI 杂乱)
  * ScriptPrescreen 豁免判断时, "en-US" 和 "en-GB" 被当成不同语言
- 改造后: 写入前统一归一化为 2 字符代码 (en, de, ja, ...),
  配合飞书 UI 端清理 select 选项, 达到"主流语言单一标识符"目标。

使用方法:
    from language_utils import normalize_language

    normalize_language("en-US")     # "en"
    normalize_language("de-DE")     # "de"
    normalize_language("zh-CN")     # "zh"
    normalize_language("en")        # "en"  (已是 2 字符)
    normalize_language("")          # "N/A" (空)
    normalize_language(None)        # "N/A" (None)
    normalize_language("xx-YY")     # "xx"  (未知变体取主标签)
    normalize_language("N/A")       # "N/A" (透传, 不识别为语言)
    normalize_language("EN-US")     # "en"  (大小写不敏感)

兼容性:
- 飞书现有 select 选项可能是 BCP-47 (en-US, de-DE), 切换后:
  * 新数据: 写入 2 字符代码, 飞书会创建新选项 (en, de)
  * 老数据: 不动, ScriptPrescreen 读取时会自动归一化
  * 建议: 切换前先在飞书 UI 添加新选项 (en/de/zh/...), 切换后清理老选项
"""

# 显式映射表 - 覆盖 BCP-47 主流变体 → ISO 639-1 二字符
# key 统一使用小写, 查找时也会先 lowercase
_BCP47_MAP = {
    # 英语
    "en-us": "en", "en-gb": "en", "en-au": "en", "en-ca": "en",
    "en-nz": "en", "en-ie": "en", "en-in": "en", "en-za": "en",
    "en-hk": "en", "en-sg": "en", "en-ph": "en", "en-ng": "en",
    # 德语
    "de-de": "de", "de-at": "de", "de-ch": "de", "de-lu": "de",
    # 法语
    "fr-fr": "fr", "fr-ca": "fr", "fr-be": "fr", "fr-ch": "fr",
    # 西班牙语
    "es-es": "es", "es-mx": "es", "es-ar": "es", "es-co": "es",
    "es-cl": "es", "es-pe": "es", "es-ve": "es",
    # 葡萄牙语
    "pt-pt": "pt", "pt-br": "pt",
    # 中文
    "zh-cn": "zh", "zh-tw": "zh", "zh-hk": "zh", "zh-sg": "zh",
    "zh-mo": "zh",
    # 日语
    "ja-jp": "ja",
    # 韩语
    "ko-kr": "ko",
    # 俄语
    "ru-ru": "ru",
    # 阿拉伯语
    "ar-sa": "ar", "ar-eg": "ar", "ar-ae": "ar", "ar-iq": "ar",
    "ar-ma": "ar", "ar-dz": "ar", "ar-tn": "ar", "ar-ly": "ar",
    # 印地语
    "hi-in": "hi",
    # 意大利语
    "it-it": "it", "it-ch": "it",
    # 荷兰语
    "nl-nl": "nl", "nl-be": "nl",
    # 波兰语
    "pl-pl": "pl",
    # 土耳其语
    "tr-tr": "tr",
    # 越南语
    "vi-vn": "vi",
    # 泰语
    "th-th": "th",
    # 印尼语
    "id-id": "id",
    # 马来语
    "ms-my": "ms", "ms-id": "ms",
    # 瑞典语
    "sv-se": "sv",
    # 挪威语
    "no-no": "no", "nb-no": "no", "nn-no": "no",
    # 丹麦语
    "da-dk": "da",
    # 芬兰语
    "fi-fi": "fi",
    # 希腊语
    "el-gr": "el",
    # 希伯来语 (he / iw 同义)
    "he-il": "he", "iw-il": "he",
    # 乌克兰语
    "uk-ua": "uk",
    # 捷克语
    "cs-cz": "cs",
    # 匈牙利语
    "hu-hu": "hu",
    # 罗马尼亚语
    "ro-ro": "ro",
    # 保加利亚语
    "bg-bg": "bg",
    # 其它常见
    "ca-es": "ca",     # 加泰罗尼亚语
    "eu-es": "eu",     # 巴斯克语
    "gl-es": "gl",     # 加利西亚语
    "is-is": "is",     # 冰岛语
    "sr-rs": "sr",     # 塞尔维亚语
    "hr-hr": "hr",     # 克罗地亚语
    "sk-sk": "sk",     # 斯洛伐克语
    "sl-si": "sl",     # 斯洛文尼亚语
    "lt-lt": "lt",     # 立陶宛语
    "lv-lv": "lv",     # 拉脱维亚语
    "et-ee": "et",     # 爱沙尼亚语
    "fa-ir": "fa",     # 波斯语
    "ur-pk": "ur",     # 乌尔都语
    "bn-bd": "bn", "bn-in": "bn",  # 孟加拉语
    "ta-in": "ta",     # 泰米尔语
    "te-in": "te",     # 泰卢固语
    "mr-in": "mr",     # 马拉地语
    "gu-in": "gu",     # 古吉拉特语
    "kn-in": "kn",     # 卡纳达语
    "ml-in": "ml",     # 马拉雅拉姆语
    "sw-ke": "sw",     # 斯瓦希里语
    "af-za": "af",     # 南非语
}


def normalize_language(lang) -> str:
    """将语言代码归一化为 2 字符 ISO 639-1 代码.

    Args:
        lang: 输入语言代码
              - BCP-47: "en-US", "de-DE", "zh-CN", ...
              - ISO 639-1: "en", "de", "zh", ...
              - 特殊值: None, "", "N/A"

    Returns:
        归一化后的 2 字符代码, 无法识别返回 "N/A"

    Examples:
        >>> normalize_language("en-US")
        'en'
        >>> normalize_language("de-DE")
        'de'
        >>> normalize_language("zh-CN")
        'zh'
        >>> normalize_language("en")
        'en'
        >>> normalize_language("")
        'N/A'
        >>> normalize_language(None)
        'N/A'
        >>> normalize_language("xx-YY")  # 未知变体
        'xx'
        >>> normalize_language("xyz")    # 单段且不在白名单
        'N/A'
        >>> normalize_language("N/A")
        'N/A'
    """
    if lang is None:
        return "N/A"
    s = str(lang).strip()
    if not s:
        return "N/A"
    lower = s.lower()

    # 透传特殊值 (N/A, unknown, etc.)
    if lower in ("n/a", "na", "unknown", "none", "null", "undefined"):
        return "N/A"

    # 1) 显式映射 (覆盖 BCP-47 常见变体)
    if lower in _BCP47_MAP:
        return _BCP47_MAP[lower]

    # 2) 已是 2 字符代码 (e.g. "en", "de")
    if "-" not in lower and len(lower) == 2 and lower.isalpha():
        return lower

    # 3) BCP-47 主标签提取 (e.g. "en-US-xx" -> "en", "pt-BR" -> "pt")
    if "-" in lower:
        primary = lower.split("-", 1)[0]
        if len(primary) == 2 and primary.isalpha():
            return primary

    # 4) 其它 (3+ 字符单段, 非标准格式) — 视为不可识别
    return "N/A"


if __name__ == "__main__":
    # 简单自检
    import sys
    cases = [
        ("en-US", "en"),
        ("de-DE", "de"),
        ("zh-CN", "zh"),
        ("zh-TW", "zh"),
        ("ja-JP", "ja"),
        ("ko-KR", "ko"),
        ("fr-FR", "fr"),
        ("pt-BR", "pt"),
        ("es-MX", "es"),
        ("ru-RU", "ru"),
        ("en", "en"),
        ("de", "de"),
        ("EN-us", "en"),       # 大小写
        ("xx-YY", "xx"),       # 未知变体
        ("xyz", "N/A"),        # 单段未知
        ("", "N/A"),
        (None, "N/A"),
        ("N/A", "N/A"),
        ("unknown", "N/A"),
    ]
    fail = 0
    for inp, exp in cases:
        got = normalize_language(inp)
        ok = "OK" if got == exp else "FAIL"
        if got != exp:
            fail += 1
        print(f"  {ok}: normalize_language({inp!r}) = {got!r} (expected {exp!r})")
    print()
    if fail:
        print(f"[FAIL] {fail} case(s) failed")
        sys.exit(1)
    print("[OK] All self-check cases passed")
