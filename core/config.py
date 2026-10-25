#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py - 配置集中管理模块

用途：统一管理所有配置项，遵循 SSOT（单一数据源）原则
使用方法：from config import CONFIG

配置来源优先级：
1. 环境变量（最高优先级）
2. .env 文件
3. 默认值（最低优先级）
"""

import os
from typing import Dict, Any, List


def load_env_config(env_path: str = None) -> Dict[str, str]:
    """
    从 .env 文件加载配置
    
    参数：
        env_path: .env 文件路径，默认为脚本同目录下的 .env
    
    返回：
        配置字典，键值对均为字符串
    
    说明：
        - 忽略空行和以 # 开头的注释行
        - 支持等号分隔的键值对
        - 值两侧的空白会被去除
    """
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
    
    config: Dict[str, str] = {}
    
    if not os.path.exists(env_path):
        return config
    
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, val = line.split('=', 1)
                config[key.strip()] = val.strip()
    
    return config


ENV_CONFIG: Dict[str, str] = load_env_config()

# ==================== 项目路径配置 ====================
# 工作目录（refactor 目录本身）
WORK_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 项目根目录（refactor 的上一级）
PROJECT_ROOT: str = os.path.dirname(WORK_DIR)


def _auto_refresh_cookies() -> None:
    """
    v5.2.1: 自动从 JSON cookie 文件刷新 .env (每次 import config 时执行)

    规则:
      - 检查 WORK_DIR 下的 bilibili_cookies.json / youtube_cookies.json
      - 如果文件存在且 .env 中的 Cookie 不同，自动更新 .env
      - 只增不减 (如果 JSON 缺失关键字段则跳过)
      - 注意: 此时 CONFIG 尚未定义, 只更新 ENV_CONFIG 和 .env 文件
    """
    import json as _json

    env_path = os.path.join(WORK_DIR, ".env")
    if not os.path.exists(env_path):
        return

    def _write_env(key: str, value: str) -> None:
        """直接写入 .env 文件 (不依赖 _save_cookie_to_env, 避免循环引用)"""
        prefix = f"{key}="
        lines = []
        updated = False
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith(prefix):
                    lines.append(f"{key}={value}\n")
                    updated = True
                else:
                    lines.append(line)
        if not updated:
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(f"{key}={value}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        ENV_CONFIG[key] = value

    current_bili = ENV_CONFIG.get("BILIBILI_COOKIE", "")
    current_yt = ENV_CONFIG.get("YOUTUBE_COOKIE", "")

    # -- Bilibili --
    bili_json = os.path.join(WORK_DIR, "bilibili_cookies.json")
    if os.path.exists(bili_json):
        try:
            with open(bili_json, "r", encoding="utf-8") as f:
                cookies = _json.load(f)
            parts = [f'{c["name"]}={c["value"]}' for c in cookies if isinstance(c, dict) and "name" in c]
            new_ck = "; ".join(parts)
            if len(new_ck) > 100 and new_ck != current_bili:
                print(f"  [AUTO-COOKIE] bilibili_cookies.json -> .env BILIBILI_COOKIE ({len(new_ck)}B)"
                      + (" (SESSDATA OK)" if "SESSDATA=" in new_ck else " [WARN: no SESSDATA!]"))
                _write_env("BILIBILI_COOKIE", new_ck)
        except Exception as e:
            print(f"  [AUTO-COOKIE] bilibili_cookies.json 读取失败: {e}")

    # -- YouTube --
    yt_json = os.path.join(WORK_DIR, "youtube_cookies.json")
    if os.path.exists(yt_json):
        try:
            with open(yt_json, "r", encoding="utf-8") as f:
                cookies = _json.load(f)
            parts = [f'{c["name"]}={c["value"]}' for c in cookies if isinstance(c, dict) and "name" in c]
            new_ck = "; ".join(parts)
            if len(new_ck) > 50 and new_ck != current_yt:
                print(f"  [AUTO-COOKIE] youtube_cookies.json -> .env YOUTUBE_COOKIE ({len(new_ck)}B)")
                _write_env("YOUTUBE_COOKIE", new_ck)
        except Exception as e:
            print(f"  [AUTO-COOKIE] youtube_cookies.json 读取失败: {e}")


# 模块加载时自动刷新 cookies
_auto_refresh_cookies()

# ==================== 本地 Excel 数据库路径 (SSOT) ====================
# ⚠️ 所有脚本必须使用这些路径常量，禁止硬编码！
BILIBILI_EXCEL_PATH: str = os.path.join(WORK_DIR, "Bilibili_local_db.xlsx")
YOUTUBE_EXCEL_PATH: str = os.path.join(WORK_DIR, "YouTube_local_db.xlsx")


CONFIG: Dict[str, Any] = {
    # ==================== AI 模型配置 ====================
    # v5.3.0: 切换为 DeepSeek 为主，SiliconFlow 为备用
    "api_key": ENV_CONFIG.get('DEEPSEEK_API_KEY', ''),
    "base_url": ENV_CONFIG.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1/chat/completions'),
    "model_cheap": [m.strip() for m in ENV_CONFIG.get('DEEPSEEK_MODEL_CHEAP', 'deepseek-v4-flash').split(',') if m.strip()],
    "model_heavy": [m.strip() for m in ENV_CONFIG.get('DEEPSEEK_MODEL_HEAVY', 'deepseek-v4-flash').split(',') if m.strip()],
    "cheap_threshold": int(ENV_CONFIG.get('DEEPSEEK_CHEAP_THRESHOLD', '5000')),
    
    # ==================== 字幕处理配置 ====================
    "max_subtitle_chars": int(ENV_CONFIG.get('MAX_SUBTITLE_CHARS', '15000')),
    
    # ==================== 并发配置 ====================
    "max_workers": int(ENV_CONFIG.get('MAX_WORKERS', '10')),
    
    # ==================== YouTube 配置 ====================
    "yt_enable_playwright": ENV_CONFIG.get('YT_ENABLE_PLAYWRIGHT', '0').strip() in ("1", "true", "TRUE", "yes", "YES"),
    "yt_api_key": ENV_CONFIG.get('YT_API_KEY', ''),

    # ==================== 飞书应用凭证 (安全敏感) ====================
    "feishu_app_id": ENV_CONFIG.get('FEISHU_APP_ID', ''),
    "feishu_app_secret": ENV_CONFIG.get('FEISHU_APP_SECRET', ''),
    "feishu_bitable_token": ENV_CONFIG.get('FEISHU_BITABLE_TOKEN', ''),
    "bilibili_table_id": ENV_CONFIG.get('BILIBILI_TABLE_ID', ''),
    "youtube_table_id": ENV_CONFIG.get('YOUTUBE_TABLE_ID', ''),
    "creator_table_id": ENV_CONFIG.get('CREATOR_TABLE_ID', ''),
    
    # ==================== Bilibili 配置 ====================
    "bili_cookie": ENV_CONFIG.get('BILIBILI_COOKIE', ''),
    "bili_max_workers": int(ENV_CONFIG.get('BILIBILI_MAX_WORKERS', '2')),
    "bili_request_interval": float(ENV_CONFIG.get('BILIBILI_REQUEST_INTERVAL', '2')),
    
    # ==================== 评分系统配置 (v3.0.0) ====================
    # D1: 时长得分阈值（分钟）
    "d1_optimal_min": 5,      # 最佳时长下限
    "d1_optimal_max": 20,     # 最佳时长上限
    "d1_acceptable_min": 3,   # 可接受时长下限
    "d1_acceptable_max": 30,  # 可接受时长上限
    
    # D2: 时效得分阈值（天）
    "d2_fresh_days": 7,       # 最新视频天数
    "d2_recent_days": 14,     # 近期视频天数
    "d2_old_days": 21,        # 较旧视频天数（超过此天数不考虑）
    
    # D3: 互动得分阈值（互动率百分比）
    "d3_excellent": 5.0,      # 极高互动率
    "d3_high": 3.0,           # 高互动率
    "d3_medium": 1.0,         # 中等互动率
    "d3_low": 0.5,            # 低互动率
    
    # M1: 新人修正系数
    "m1_newcomer": 1.5,       # 0次入选
    "m1_fresh": 1.3,          # 1次入选
    "m1_slight": 1.1,         # 2次入选
    "m1_normal": 1.0,         # 3次入选
    "m1_penalty_slight": 0.9, # 4次入选
    "m1_penalty": 0.8,        # >4次入选
    
    # 筛选配置
    "max_videos_per_week": 8,          # 每周推荐视频数量
    "max_videos_per_author": 1,        # 每个博主最多入选视频数
    "selection_history_file": "selection_history.json",  # 入选历史记录文件

    # 人工初筛时间过滤 (v3.3.0)
    # 只处理最近N天写入飞书表格的视频，0表示不过滤
    # 设计意图：每次只需对当天或昨天获取的新视频进行评分和审核
    "human_prescreen_days_filter": 1,
}


BILI_COOKIE: str = CONFIG.get('bili_cookie', '')


VIDEO_LIST_FILE: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "video_urls.txt")


# ================= 项目标识配置 =================
# 本仓库是通用的「游戏视频逐字稿内容审查」框架，不绑定任何具体产品。
# 部署到你自己的项目时，用环境变量指定游戏名与厂商名即可。
GAME_TITLE = os.getenv("GAME_TITLE", "你的游戏名")
GAME_DEVELOPER = os.getenv("GAME_DEVELOPER", "开发商")
GAME_PUBLISHER = os.getenv("GAME_PUBLISHER", "发行商")


SYSTEM_PROMPT_CENSOR: str = (
    "你是一个专业的互联网内容安全审查助手。"
    "用户将提供一段视频逐字稿，请通读全文并做合规性检查。\n"
    "\n"
    "【审查范围】：\n"
    "1. [RED] 法律合规：检查是否包含中国大陆法律法规明确禁止传播的内容（包括但不限于政治敏感、宗教、领土完整、违禁品等）。\n"
    "\n"
    "【必须排除的误报情况 - 绝对不要拦截】：\n"
    "- 语气词和口头禅：如\"man\"、\"oh\"、\"of course\"、\"what the [blank]\"等\n"
    "- 游戏术语和技能名：如\"double speed\"（双倍速度）、\"phantom\"（幻影单位）、\"factory\"（工厂）等游戏内机制\n"
    "- 玩家对局势的吐槽：如\"this is trash\"（这太垃圾了）、\"bad luck\"（运气差）等\n"
    "- 脏话作为强调词：如\"fuck\"、\"shit\"等当语气词使用时\n"
    "- 涉及游戏平衡/战术的正常讨论：如\"nerf this\"（削弱这个）、\"broken\"（不平衡）等\n"
    "- 对游戏本身、开发商、发行商及其员工的任何评价、批评、吐槽或抱怨（无论语气强弱）——这些属于正常的玩家表达，不构成本审查的拦截理由\n"
    "\n"
    "【强制输出规范】：\n"
    "情况 1：如果全文未触犯上述红线，请绝对不要输出任何表格，仅回复：\n"
    "> [OK 全文安全] 未发现违规内容。\n"
    "\n"
    "情况 2：如果发现触犯红线的言论，请输出 Markdown 表格，【仅列出违规行】：\n"
    "表头：| 违规原文片段 | 判定等级 | 违规原因及详细解释 |\n"
    "判定等级仅限使用：[RED] 严重违规（法律/政治）\n"
    "\n"
    "注意：禁止输出任何开场白、解释或总结。保持审查的客观与克制。"
)


SYSTEM_PROMPT_APPEAL: str = (
    f"你是一位资深的游戏内容运营专家，专注于《{GAME_TITLE}》游戏社区。"
    "用户将提供一段视频逐字稿，请评估该内容对玩家的吸引力。\n"
    "\n"
    "【评估维度】：\n"
    f"1. 游戏相关性：内容是否与《{GAME_TITLE}》高度相关（战术讲解、比赛解说、新版本分析等）\n"
    "2. 信息价值：是否提供有价值的游戏知识、战术技巧或独特见解\n"
    "3. 目标受众：是否清晰定位目标玩家群体（新手/进阶/高端玩家）\n"
    "4. 创新性：内容是否有新意，是否重复常见话题\n"
    "5. 可理解性：解说是否清晰易懂，逻辑是否连贯\n"
    "\n"
    "【强制输出规范】：\n"
    "你必须且只能输出一个有效的JSON对象，不要输出任何其他文字、解释、前言或结语。\n"
    "【JSON格式】（严格遵循以下格式，字段不可省略）：\n"
    "```json\n"
    "{\n"
    "  \"score\": <1-100的整数>,\n"
    "  \"game_relevance\": \"<高/中/低>\",\n"
    "  \"info_value\": \"<高/中/低>\",\n"
    "  \"target_audience\": \"<新手/进阶/高端/通用>\",\n"
    "  \"innovation\": \"<高/中/低>\",\n"
    "  \"clarity\": \"<高/中/低>\",\n"
    "  \"summary\": \"<一句话总结该视频的核心价值>\"\n"
    "}\n"
    "```\n"
    "\n"
    "【评分标准】：\n"
    "- 90-100分：顶级内容，强烈推荐给所有玩家\n"
    "- 70-89分：优质内容，对目标玩家很有价值\n"
    "- 50-69分：一般内容，有一定参考价值\n"
    "- 30-49分：较差内容，价值有限\n"
    "- 0-29分：无价值内容，不推荐"
)


def get_config(key: str, default: Any = None) -> Any:
    """
    获取配置项
    
    参数：
        key: 配置键名
        default: 默认值
    
    返回：
        配置值，如果不存在则返回默认值
    """
    return CONFIG.get(key, default)


def update_config(key: str, value: Any) -> None:
    """
    更新配置项（运行时修改，不持久化）
    
    参数：
        key: 配置键名
        value: 新值
    
    说明：
        此函数仅修改内存中的配置，不会写入 .env 文件
    """
    CONFIG[key] = value


def prompt_for_bilibili_cookie() -> str:
    """
    提示用户输入或更新 Bilibili Cookie (v3.4.3)
    
    功能：
    1. 显示当前 Cookie 的关键信息（SESSDATA 过期时间等）
    2. 询问用户是否需要更新
    3. 支持粘贴新的 Cookie 字符串或 JSON 文件路径
    4. 自动更新 .env 文件
    
    返回：
        更新后的 Cookie 字符串
    """
    import datetime
    import json
    import re
    
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
    current_cookie = ENV_CONFIG.get('BILIBILI_COOKIE', '')
    
    print("\n" + "=" * 60)
    print("[Bilibili Cookie 检查]")
    print("=" * 60)
    
    # 解析当前 Cookie 信息
    if current_cookie:
        # 提取 SESSDATA
        sessdata_match = re.search(r'SESSDATA=([^;]+)', current_cookie)
        if sessdata_match:
            sessdata = sessdata_match.group(1)
            # SESSDATA 格式: value,expiry,timestamp*signature
            try:
                parts = sessdata.split('%2C')
                if len(parts) >= 2:
                    expiry_timestamp = int(parts[1])
                    expiry_date = datetime.datetime.fromtimestamp(expiry_timestamp)
                    days_left = (expiry_date - datetime.datetime.now()).days
                    print(f"  当前 SESSDATA 过期时间: {expiry_date.strftime('%Y-%m-%d')}")
                    print(f"  剩余有效期: {days_left} 天")
                    if days_left < 7:
                        print(f"  [WARN] Cookie 即将过期，建议更新！")
            except Exception:
                pass
        
        # 提取 DedeUserID
        uid_match = re.search(r'DedeUserID=([^;]+)', current_cookie)
        if uid_match:
            print(f"  当前用户ID: {uid_match.group(1)}")
        
        print(f"  Cookie 长度: {len(current_cookie)} 字符")
    else:
        print("  [WARN] 当前未配置 Bilibili Cookie")
    
    print("\n  选项:")
    print("    1. 使用当前 Cookie 继续")
    print("    2. 粘贴新的 Cookie 字符串更新")
    print("    3. 从 cookies.json 文件导入")
    print("    4. 跳过（不更改）")
    
    try:
        choice = input("\n  请选择 [1/2/3/4] (默认: 1): ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        choice = "1"
    
    if choice == "1":
        print("  [OK] 使用当前 Cookie 继续")
        return current_cookie
    
    elif choice == "2":
        print("\n  请粘贴新的 Cookie 字符串（支持格式: key=value;key2=value2）:")
        print("  提示: 从浏览器开发者工具 -> Network -> 任意请求 -> Request Headers -> Cookie 复制")
        try:
            new_cookie = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("  [WARN] 输入中断，使用当前 Cookie")
            return current_cookie
        
        if new_cookie and len(new_cookie) > 50:
            _save_cookie_to_env(new_cookie, env_path)
            print("  [OK] Cookie 已更新到 .env 文件")
            return new_cookie
        else:
            print("  [WARN] 输入的 Cookie 太短，可能无效，使用当前 Cookie")
            return current_cookie
    
    elif choice == "3":
        print("\n  请输入 cookies.json 文件路径（默认: bilibili_cookies.json）:")
        try:
            file_path = input("  > ").strip() or "bilibili_cookies.json"
        except (EOFError, KeyboardInterrupt):
            print("  [WARN] 输入中断，使用当前 Cookie")
            return current_cookie
        
        # 支持相对路径和绝对路径
        if not os.path.isabs(file_path):
            # 先尝试当前工作目录
            if os.path.exists(file_path):
                full_path = file_path
            else:
                # 再尝试项目根目录
                full_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '..', file_path)
                if not os.path.exists(full_path):
                    full_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), file_path)
        else:
            full_path = file_path
        
        if os.path.exists(full_path):
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                
                # 从 JSON 转换为 Cookie 字符串
                cookie_parts = []
                for cookie in cookies:
                    if isinstance(cookie, dict) and 'name' in cookie and 'value' in cookie:
                        cookie_parts.append(f"{cookie['name']}={cookie['value']}")
                
                new_cookie = ';'.join(cookie_parts)
                if new_cookie and len(new_cookie) > 50:
                    _save_cookie_to_env(new_cookie, env_path)
                    print(f"  [OK] 已从 {os.path.basename(full_path)} 导入并更新 Cookie")
                    return new_cookie
                else:
                    print("  [WARN] 从文件提取的 Cookie 太短，可能无效")
                    return current_cookie
            except Exception as e:
                print(f"  [ERROR] 读取文件失败: {e}")
                return current_cookie
        else:
            print(f"  [WARN] 文件不存在: {full_path}")
            return current_cookie
    
    else:
        print("  [OK] 跳过，使用当前 Cookie")
        return current_cookie


def _save_cookie_to_env(cookie_value: str, env_path: str, key: str = "BILIBILI_COOKIE") -> None:
    """
    保存 Cookie 到 .env 文件 (v3.4.3 / v5.2.1: 支持 key 参数)

    参数：
        cookie_value: 新的 Cookie 字符串
        env_path: .env 文件路径
        key: .env 中的键名 (默认 BILIBILI_COOKIE)
    """
    lines = []
    cookie_updated = False

    prefix = f"{key}="

    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip().startswith(prefix):
                    lines.append(f'{key}={cookie_value}\n')
                    cookie_updated = True
                else:
                    lines.append(line)

    if not cookie_updated:
        # 如果没有找到现有行，添加到文件末尾
        if lines and not lines[-1].endswith('\n'):
            lines.append('\n')
        lines.append(f'# -------- Cookie 自动更新 --------\n')
        lines.append(f'{key}={cookie_value}\n')

    with open(env_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    # 同时更新内存中的配置
    ENV_CONFIG[key] = cookie_value
    if key == "BILIBILI_COOKIE":
        CONFIG['bili_cookie'] = cookie_value
        global BILI_COOKIE
        BILI_COOKIE = cookie_value
    elif key == "YOUTUBE_COOKIE":
        CONFIG['yt_cookie'] = cookie_value


if __name__ == "__main__":
    print("=" * 60)
    print("配置检查")
    print("=" * 60)
    
    print("\n[AI 模型配置]")
    print(f"  API Key: {'已配置' if CONFIG['api_key'] else '未配置'}")
    print(f"  Base URL: {CONFIG['base_url']}")
    print(f"  省钱模型: {CONFIG['model_cheap']}")
    print(f"  强力模型: {CONFIG['model_heavy']}")
    print(f"  省钱阈值: {CONFIG['cheap_threshold']} 字符")
    
    print("\n[并发配置]")
    print(f"  AI 并发数: {CONFIG['max_workers']}")
    print(f"  B站并发数: {CONFIG['bili_max_workers']}")
    
    print("\n[评分系统配置 v3.0.0]")
    print(f"  D1 最佳时长: {CONFIG['d1_optimal_min']}-{CONFIG['d1_optimal_max']} 分钟")
    print(f"  D2 时效阈值: {CONFIG['d2_fresh_days']}/{CONFIG['d2_recent_days']}/{CONFIG['d2_old_days']} 天")
    print(f"  D3 互动阈值: {CONFIG['d3_excellent']}/{CONFIG['d3_high']}/{CONFIG['d3_medium']}/{CONFIG['d3_low']}%")
    print(f"  M1 新人加成: {CONFIG['m1_newcomer']}x")
    
    print("\n[筛选配置]")
    print(f"  每周推荐: {CONFIG['max_videos_per_week']} 个视频")
    print(f"  每博主上限: {CONFIG['max_videos_per_author']} 个视频")
    
    print("\n" + "=" * 60)
    print("[OK] 配置检查完成")
