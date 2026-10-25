#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
discord_video_mark.py - Discord videos-n-streams 自荐采集 (v5.4.0)

功能：
1. 拉取 Discord 频道 <DISCORD_VIDEO_CHANNEL_ID> (videos-n-streams, type=0 普通文字频道, 非论坛)
   最近 100 条消息 (需代理: http 17890/17891/4780 或 socks5 37890)
2. 按消息 timestamp 过滤最近 7 天, 正则提取 youtube 链接 (watch?v= / youtu.be/) 并按视频ID去重
3. 与飞书 YouTube 表 (Video ID) 匹配 (v5.4.0 来源注册表架构):
   - 命中: 仅追加 Notes 来源标注 + 写入本地来源注册表 (source_registry.json)
     ⚠️ 不再前置写 Human Prescreen=High Priority —— 自荐视频必须以普通身份
     走完整流程 (机器初筛 -> ScriptDedup -> AI审核+评分), AI 完成后由
     workflow_orchestrator 统一翻转为 High Priority
   - 未命中: 通过 YouTube Data API 拉取元数据补录入表 (同样只标注来源),
     补录记录当轮即可参与 Prescreen/ScriptDedup/AI 全流程
4. 历史去重: data/discord_history.json (key=消息ID), 仿 spider_discord.py 边界停止逻辑;
   仅当消息内全部视频都已"注册/补录成功"才记入历史,
   消息内有任何失败视频则不记 (保留下次重扫重试机会)

DISCORD_USER_TOKEN 读取顺序: 进程环境变量 -> refactor/.env -> add-on/.env

使用方法：
    python core/discord_video_mark.py             # 正式运行 (写飞书+记历史)
    python core/discord_video_mark.py --dry-run   # 只打印发现的消息与计划动作, 不写飞书不记历史
"""

import os
import re
import sys
import json
import time
import socket
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)

try:
    import requests
except ImportError:
    print("[ERROR] requests 库未安装")
    sys.exit(1)

from feishu_fields import (
    YT_F_VIDEO_ID, YT_F_NOTES, YT_F_TITLE, YT_F_LINK,
    YT_F_PUBLISH_DATE, YT_F_PUBLISHED_AT, YT_F_CHANNEL, YT_F_CHANNEL_ID,
    YT_F_VIEWS, YT_F_LIKES, YT_F_COMMENTS, YT_F_DURATION, YT_F_DURATION_MIN,
    YT_F_LANGUAGE, YT_F_LIVE_RECORDING, YT_F_HAS_CAPTIONS, YT_F_TAGS,
    YT_F_DESCRIPTION,
)
from source_registry import register_source, SOURCE_DISCORD

# ================= 配置常量 =================

# videos-n-streams 频道 (type=0 普通文字频道, 非论坛, 用 messages 接口)
DISCORD_CHANNEL_ID = os.getenv("DISCORD_VIDEO_CHANNEL_ID", "")
DISCORD_API_BASE = "https://discord.com/api/v9"
MESSAGES_LIMIT = 100
DAYS_WINDOW = 7  # 只处理最近 7 天的消息

# YouTube 视频链接正则 (watch?v=ID 与 youtu.be/ID, ID 固定 11 位)
YT_WATCH_PATTERN = re.compile(r"watch\?v=([\w-]{11})")
YT_SHORT_PATTERN = re.compile(r"youtu\.be/([\w-]{11})")

# 历史文件 (绝对路径)
HISTORY_FILE = os.path.join(PROJECT_ROOT, "data", "discord_history.json")
SOURCE_NOTE = "来源: Discord videos-n-streams 自荐"

# 代理端口 (参考 add-on/base_utils.select_best_proxy: 用户代理 http + 自带 clash socks5)
HTTP_PROXY_PORTS = [17890, 17891, 4780]
SOCKS5_PROXY_PORT = 37890


def _load_token() -> str:
    """读取 DISCORD_USER_TOKEN: 进程环境变量 -> refactor/.env -> add-on/.env"""
    token = os.getenv("DISCORD_USER_TOKEN", "").strip()
    if token:
        return token
    for env_path in (
        os.path.join(PROJECT_ROOT, ".env"),
        os.path.join(PROJECT_ROOT, "add-on", ".env"),
    ):
        if not os.path.exists(env_path):
            continue
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DISCORD_USER_TOKEN") and "=" in line:
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            print(f"[OK] 已从 {env_path} 读取 DISCORD_USER_TOKEN")
                            return val
        except OSError as e:
            print(f"[WARN] 读取 {env_path} 失败: {e}")
    return ""


def _port_alive(port: int) -> bool:
    """1 秒内测试本地端口是否可连接"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(("127.0.0.1", port))
        sock.close()
        return result == 0
    except OSError:
        return False


def select_proxy() -> Optional[str]:
    """检测可用代理: http 17890/17891/4780 优先, 其次 socks5 37890; 无则直连"""
    for port in HTTP_PROXY_PORTS:
        if _port_alive(port):
            print(f"[PROXY] 检测到用户代理 (http://127.0.0.1:{port})")
            return f"http://127.0.0.1:{port}"
    if _port_alive(SOCKS5_PROXY_PORT):
        print(f"[PROXY] 检测到自带代理 (socks5://127.0.0.1:{SOCKS5_PROXY_PORT})")
        return f"socks5://127.0.0.1:{SOCKS5_PROXY_PORT}"
    print("[PROXY] 未检测到可用代理, 尝试直连 (Discord 大概率需要代理)")
    return None


# ================= 历史记录 =================

def load_history() -> Dict[str, Dict]:
    """加载历史: key=消息ID, value={timestamp: ms, title_prefix: 内容前6字}"""
    if not os.path.exists(HISTORY_FILE):
        return {}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[WARN] 历史记录读取异常: {e}")
        return {}
    result: Dict[str, Dict] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                result[str(k)] = {
                    "timestamp": v.get("timestamp", 0) if isinstance(v.get("timestamp", 0), (int, float)) else 0,
                    "title_prefix": str(v.get("title_prefix", "") or ""),
                }
            else:
                result[str(k)] = {"timestamp": v if isinstance(v, (int, float)) else 0, "title_prefix": ""}
    return result


def save_history(history: Dict[str, Dict]) -> None:
    """保存历史 (UTF-8, 目录不存在则创建)"""
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
    except OSError as e:
        print(f"[ERROR] 历史记录保存失败: {e}")


# ================= Discord 拉取 =================

def _parse_message_ts(msg: Dict) -> float:
    """解析 Discord 消息 timestamp (ISO 8601) 为秒级时间戳; 失败返回 0"""
    ts_str = str(msg.get("timestamp", "") or "").strip()
    if not ts_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return 0.0


def fetch_channel_messages(token: str, proxies: Optional[Dict[str, str]]) -> List[Dict]:
    """拉取频道最近 100 条消息 (普通文字频道用 messages 接口, 返回按时间倒序)"""
    url = f"{DISCORD_API_BASE}/channels/{DISCORD_CHANNEL_ID}/messages?limit={MESSAGES_LIMIT}"
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    try:
        res = requests.get(url, headers=headers, proxies=proxies, timeout=20)
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Discord 请求异常: {type(e).__name__}: {str(e)[:150]}")
        return []

    if res.status_code == 401:
        print("[ERROR] Token 鉴权失败 (401), 请检查 DISCORD_USER_TOKEN 配置")
        return []
    if res.status_code == 403:
        print("[ERROR] Token 无权限或已过期 (403), 请更新 DISCORD_USER_TOKEN")
        return []
    if res.status_code != 200:
        print(f"[ERROR] Discord API 请求失败, 状态码: {res.status_code}")
        return []

    try:
        msgs = res.json()
    except ValueError as e:
        print(f"[ERROR] Discord API 返回非 JSON 响应: {e}")
        return []
    if not isinstance(msgs, list):
        print(f"[ERROR] Discord API 返回格式异常: {type(msgs).__name__}")
        return []
    print(f"[OK] 拉取到 {len(msgs)} 条消息 (频道 {DISCORD_CHANNEL_ID})")
    return msgs


def scan_new_messages(messages: List[Dict], history: Dict[str, Dict]) -> List[Dict]:
    """边界停止 + 7 天时间窗过滤 (消息按时间倒序, 触碰边界即停止)

    边界逻辑 (仿 spider_discord.py):
      - 消息ID命中历史: 标题(内容前6字)一致 -> break; 旧版无标题 -> break; 不一致 -> continue
      - 消息早于 7 天前 -> break (倒序扫描, 后续更旧)
    """
    cutoff_ts = time.time() - DAYS_WINDOW * 86400
    new_msgs: List[Dict] = []
    for msg in messages:
        msg_id = str(msg.get("id", ""))
        content = str(msg.get("content", "") or "").strip()
        prefix = content[:6]

        if msg_id in history:
            stored = history[msg_id]
            if stored.get("title_prefix") and prefix and stored["title_prefix"] == prefix:
                print(f"  -> 触碰历史边界 [{msg_id}] (内容验证通过), 停止扫描")
                break
            elif not stored.get("title_prefix"):
                print(f"  -> 触碰历史边界 [{msg_id}] (旧版无标题), 停止扫描")
                break
            else:
                print(f"  -> ID命中但内容不符 [{msg_id}], 继续")
                continue

        ts = _parse_message_ts(msg)
        if ts and ts < cutoff_ts:
            print(f"  -> 到达 {DAYS_WINDOW} 天时间边界 [{msg_id}], 停止扫描")
            break
        if not ts:
            print(f"  [WARN] 消息 {msg_id} timestamp 无法解析, 保留处理")
        new_msgs.append(msg)
    return new_msgs


def extract_video_ids(content: str) -> List[str]:
    """从消息内容提取 YouTube 视频ID (watch?v= 与 youtu.be/, 保持出现顺序)"""
    ids: List[str] = []
    for pattern in (YT_WATCH_PATTERN, YT_SHORT_PATTERN):
        for vid in pattern.findall(content or ""):
            if vid not in ids:
                ids.append(vid)
    return ids


# ================= YouTube 元数据补录 (v5.4.0) =================

def parse_iso_duration(duration_str: Optional[str]) -> Tuple[str, float]:
    """ISO8601 时长 (PT1H2M3S) -> (HH:MM:SS, 分钟)

    口径与 bilibili_youtube_once_for_all_fetcher.parse_duration 完全一致
    (不直接 import 该模块, 避免拉起 playwright/pandas 等重依赖)
    """
    if not duration_str:
        return "00:00:00", 0.0
    match = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?").match(duration_str)
    if not match:
        return "00:00:00", 0.0
    h, m, s = [int(x) if x else 0 for x in match.groups()]
    return f"{h:02d}:{m:02d}:{s:02d}", round(h * 60 + m + s / 60, 2)


def fetch_yt_metadata(video_id: str, api_key: str, proxies: Optional[Dict[str, str]]) -> Optional[Dict]:
    """YouTube Data API v3 拉取单个视频元数据; 失败返回 None"""
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"part": "snippet,statistics,contentDetails", "id": video_id, "key": api_key}
    try:
        res = requests.get(url, params=params, proxies=proxies, timeout=20)
    except requests.exceptions.RequestException as e:
        print(f"  [ERROR] YouTube API 请求失败 ({video_id}): {type(e).__name__}: {str(e)[:100]}")
        return None
    if res.status_code == 403:
        print(f"  [ERROR] YouTube API 拒绝 (403, Key 无效/配额耗尽) ({video_id})")
        return None
    if res.status_code != 200:
        print(f"  [ERROR] YouTube API 状态码 {res.status_code} ({video_id})")
        return None
    try:
        items = res.json().get("items", [])
    except ValueError:
        print(f"  [ERROR] YouTube API 返回非 JSON ({video_id})")
        return None
    if not items:
        print(f"  [WARN] YouTube API 无结果 ({video_id}, 视频可能已删除/设为私享)")
        return None
    return items[0]


def build_yt_record_fields(item: Dict, video_id: str) -> Dict:
    """YouTube API item -> YT 表字段 dict (字段名/格式与 fetcher 口径一致)"""
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    details = item.get("contentDetails", {})

    # ISO8601 UTC -> GMT+8 (与 fetcher: publishedAt + 8h 后格式化, 口径一致)
    publish_date, published_at = "", ""
    try:
        dt_utc = datetime.strptime(snippet.get("publishedAt", ""), "%Y-%m-%dT%H:%M:%SZ")
        dt_gmt8 = dt_utc + timedelta(hours=8)
        publish_date = dt_gmt8.strftime("%Y/%m/%d")
        published_at = dt_gmt8.strftime("%Y/%m/%d %H:%M:%S")
    except ValueError:
        print(f"  [WARN] 发布时间解析失败 ({video_id}): {snippet.get('publishedAt', '')!r}")

    dur_str, dur_min = parse_iso_duration(details.get("duration", ""))
    views = int(stats.get("viewCount", 0) or 0)
    likes = int(stats.get("likeCount", 0) or 0)
    comments = int(stats.get("commentCount", 0) or 0)
    engagement = f"{((likes + comments) / views * 100):.2f}%" if views > 0 else "0.00%"
    tags = snippet.get("tags", []) or []

    # Language 归一化失败不阻塞 (fetcher 同款调用)
    try:
        from language_utils import normalize_language
        lang = normalize_language(snippet.get("defaultAudioLanguage", "N/A"))
    except Exception:
        lang = ""

    return {
        YT_F_VIDEO_ID: video_id,
        YT_F_TITLE: str(snippet.get("title", "") or ""),
        YT_F_PUBLISH_DATE: publish_date,
        YT_F_PUBLISHED_AT: published_at,
        YT_F_LINK: f"https://www.youtube.com/watch?v={video_id}",
        YT_F_CHANNEL: str(snippet.get("channelTitle", "") or ""),
        YT_F_CHANNEL_ID: str(snippet.get("channelId", "") or ""),
        YT_F_VIEWS: views,
        YT_F_LIKES: likes,
        YT_F_COMMENTS: comments,
        "Engagement Rate": engagement,  # 与 fetcher 一致的文本格式
        YT_F_DURATION: dur_str,
        YT_F_DURATION_MIN: dur_min,
        YT_F_LANGUAGE: lang,
        YT_F_LIVE_RECORDING: "Yes" if snippet.get("liveBroadcastContent", "none") != "none" else "No",
        YT_F_HAS_CAPTIONS: "Yes" if details.get("caption") == "true" else "No",
        YT_F_TAGS: ", ".join(tags[:15]),
        YT_F_DESCRIPTION: str(snippet.get("description", "") or "")[:500],
        YT_F_NOTES: SOURCE_NOTE,  # 新记录直接带来源标注
    }


# ================= 飞书匹配与来源注册 =================

def match_and_register(
    video_msgs: List[Tuple[str, Dict]],
    dry_run: bool,
    proxies: Optional[Dict[str, str]],
) -> Tuple[int, int, int, int]:
    """与飞书 YT 表匹配并注册来源 (v5.4.0: 不再写 Human Prescreen)

    参数:
        video_msgs: [(video_id, 消息dict)], 已按视频ID去重
        dry_run: True 只读不写
        proxies: 供 YouTube API 补录使用的代理 dict

    返回:
        (registered 命中注册数, already 已注册跳过数, imported 补录入表数, failed 失败数)
    """
    from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, YT_TABLE_ID

    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)

    # 规则: 读取/写入前重新拉取表结构, 确认字段存在
    print("[*] 拉取 YT 表字段结构...")
    fields = sync.get_fields(YT_TABLE_ID)
    required = (YT_F_VIDEO_ID, YT_F_NOTES)
    if not fields:
        print("[ERROR] 无法获取 YT 表字段结构, 跳过匹配")
        return 0, 0, 0, len(video_msgs)
    missing = [f for f in required if f not in fields]
    if missing:
        print(f"[ERROR] YT 表缺少字段 {missing}, 跳过匹配 (写入需确认字段类型)")
        return 0, 0, 0, len(video_msgs)

    print("[*] 读取 YT 表记录, 建立 Video ID 索引...")
    records = sync.get_records(YT_TABLE_ID)
    index: Dict[str, Dict] = {}
    for rec in records:
        rec_fields = rec.get("fields", {})
        vid = str(rec_fields.get(YT_F_VIDEO_ID, "") or "").strip()
        if vid:
            index[vid] = rec

    registered = 0
    already = 0
    imported = 0
    failed = 0
    # 每条消息中各 vid 的处理结果: msg_id -> [vid 状态, ...]
    # 状态: "registered_ok" 命中注册 / "already" 已注册 /
    #       "imported_ok" 补录入表 / "write_fail"/"import_fail" 失败 / "dry_run" 演练
    msg_vid_status: Dict[str, List[str]] = {}

    for vid, msg in video_msgs:
        msg_id = str(msg.get("id", ""))
        rec = index.get(vid)

        if rec is not None:
            # ---- 命中: 注册来源 + 追加 Notes 标注 (不动 Human Prescreen) ----
            rec_fields = rec.get("fields", {})
            existing_notes = str(rec_fields.get(YT_F_NOTES, "") or "").strip()
            has_note = SOURCE_NOTE in existing_notes

            if dry_run:
                action = "注册来源 (已有标注, 不重复写)" if has_note else "注册来源 + Notes 追加来源标注"
                print(f"  [DRY-RUN] {vid} 将{action} (Human Prescreen 不动)")
                registered += 1
                msg_vid_status.setdefault(msg_id, []).append("dry_run")
                continue

            newly = register_source("youtube", vid, SOURCE_DISCORD)
            if has_note and not newly:
                print(f"  [SKIP] {vid} 已注册且已有来源标注")
                already += 1
                msg_vid_status.setdefault(msg_id, []).append("already")
                continue

            merged_notes = existing_notes if has_note else (
                f"{existing_notes}\n{SOURCE_NOTE}" if existing_notes else SOURCE_NOTE
            )
            ok = sync.update_record(YT_TABLE_ID, str(rec.get("record_id", "")), {
                YT_F_NOTES: merged_notes,
            })
            if ok:
                registered += 1
                msg_vid_status.setdefault(msg_id, []).append("registered_ok")
                print(f"  [OK] {vid} 来源已注册 (Discord 自荐)")
            else:
                failed += 1
                msg_vid_status.setdefault(msg_id, []).append("write_fail")
                print(f"  [FAIL] {vid} Notes 写入失败 (不计入历史, 下次重试)")
            continue

        # ---- 未命中: YouTube API 补录入表 (只标注来源, 当轮参与全流程) ----
        if dry_run:
            print(f"  [DRY-RUN] {vid} 不在 YT 表, 将通过 YouTube API 补录入表 + 注册来源")
            imported += 1
            msg_vid_status.setdefault(msg_id, []).append("dry_run")
            continue

        try:
            from config import CONFIG
            api_key = CONFIG.get("yt_api_key", "")
        except Exception:
            api_key = ""
        if not api_key:
            print(f"  [FAIL] {vid} 缺少 yt_api_key 配置, 无法补录 (不计入历史, 下次重试)")
            failed += 1
            msg_vid_status.setdefault(msg_id, []).append("import_fail")
            continue

        item = fetch_yt_metadata(vid, api_key, proxies)
        if item is None:
            failed += 1
            msg_vid_status.setdefault(msg_id, []).append("import_fail")
            continue

        record_fields = build_yt_record_fields(item, vid)
        record_id = sync.create_record(YT_TABLE_ID, record_fields)
        if not record_id:
            failed += 1
            msg_vid_status.setdefault(msg_id, []).append("import_fail")
            print(f"  [FAIL] {vid} 补录写入失败 (不计入历史, 下次重试)")
            continue

        register_source("youtube", vid, SOURCE_DISCORD)
        imported += 1
        msg_vid_status.setdefault(msg_id, []).append("imported_ok")
        print(f"  [OK] {vid} 已补录入表 + 注册来源 (record_id={record_id[:12]}...)")

    # 仅当该消息的全部视频都已"注册/补录成功"时才记入历史;
    # 同一消息中存在任何失败 vid, 则不记该 msg_id, 保留下次重扫重试机会
    if not dry_run and msg_vid_status:
        history = load_history()
        now_ms = int(time.time() * 1000)
        saved_count = 0
        for vid, msg in video_msgs:
            msg_id = str(msg.get("id", ""))
            if not msg_id or msg_id in history:
                continue
            statuses = msg_vid_status.get(msg_id, [])
            if statuses and all(s in ("registered_ok", "already", "imported_ok") for s in statuses):
                history[msg_id] = {
                    "timestamp": int(_parse_message_ts(msg) * 1000) or now_ms,
                    "title_prefix": str(msg.get("content", "") or "").strip()[:6],
                }
                saved_count += 1
        if saved_count:
            save_history(history)
        print(f"[OK] {saved_count} 条全部成功的消息已记入历史 ({HISTORY_FILE})")

    return registered, already, imported, failed


# ================= 主流程 =================

def main() -> int:
    parser = argparse.ArgumentParser(description="Discord videos-n-streams 自荐采集标记")
    parser.add_argument("--dry-run", action="store_true", help="只打印发现的消息与链接, 不写飞书不记历史")
    args = parser.parse_args()

    print("=" * 60)
    print("Discord videos-n-streams 自荐采集 (v5.4.0 来源注册表架构)")
    if args.dry_run:
        print("模式: DRY-RUN (不写飞书, 不记历史)")
    print("=" * 60)

    token = _load_token()
    if not token:
        print("[ERROR] 缺少 DISCORD_USER_TOKEN (refactor/.env 或 add-on/.env)")
        return 1

    proxy_url = select_proxy()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    messages = fetch_channel_messages(token, proxies)
    if not messages:
        print("[ERROR] 未拉取到任何消息")
        return 1

    history = load_history()
    print(f"[*] 历史缓存: {len(history)} 条消息")

    new_msgs = scan_new_messages(messages, history)
    print(f"[*] 窗口内新消息: {len(new_msgs)} 条 (最近 {DAYS_WINDOW} 天)")

    # 提取链接并按视频ID去重 (保留首次出现顺序)
    video_msgs: List[Tuple[str, Dict]] = []
    seen_ids = set()
    for msg in new_msgs:
        content = str(msg.get("content", "") or "")
        author = str(msg.get("author", {}).get("username", "Unknown") or "Unknown")
        ts_str = str(msg.get("timestamp", "") or "")[:19].replace("T", " ")
        ids = extract_video_ids(content)
        preview = content.replace("\n", " ")[:60]
        if ids:
            print(f"  [MSG] {ts_str} {author}: {preview}")
            for vid in ids:
                if vid not in seen_ids:
                    seen_ids.add(vid)
                    video_msgs.append((vid, msg))
                    print(f"        -> 发现视频: https://www.youtube.com/watch?v={vid}")
        else:
            print(f"  [MSG] {ts_str} {author}: {preview} (无YouTube链接)")

    if not video_msgs:
        print("[OK] 窗口内没有发现 YouTube 链接, 无需标记")
        return 0

    print(f"\n[*] 共发现 {len(video_msgs)} 个去重视频, 开始匹配 YT 表...")
    try:
        registered, already, imported, failed = match_and_register(
            video_msgs, dry_run=args.dry_run, proxies=proxies
        )
    except Exception as e:
        print(f"[ERROR] 匹配注册流程异常: {type(e).__name__}: {e}")
        return 1

    print("\n" + "=" * 60)
    print(f"[SUMMARY] 发现视频: {len(video_msgs)} | 注册: {registered} | "
          f"已注册跳过: {already} | 补录入表: {imported} | 失败: {failed}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
