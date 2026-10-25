#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import asyncio
import sys
import requests
from datetime import datetime, timezone
from typing import Dict, Any, List

# 引入核心基建 (不修改 base_utils 函数签名，直接复用)
from base_utils import (
    setup_windows_console, 
    analyze_micro_feedback, 
    write_to_feishu,
    build_feishu_record,
    SELECTED_PROXY_URL,
    rate_limit,
    trim_history_map,
    HISTORY_MAX_SIZE
)

setup_windows_console()

# ================= 配置读取 =================
DISCORD_USER_TOKEN = os.getenv("DISCORD_USER_TOKEN")
# 服务器 ID：用于拼接帖子永久链接
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")
DISCORD_FETCH_LIMIT = int(os.getenv("DISCORD_FETCH_LIMIT", 15))

# 使用智能选择的代理
PROXY_URL = SELECTED_PROXY_URL
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
HISTORY_FILE = "discord_history.json"

# 物理写死：目标频道映射表 (频道 ID -> 语义分类)
# 2026-07-29 平台字段统一添加 [Discord] 前缀，便于在总表中区分来源
def _parse_channel_map(raw: str) -> Dict[str, str]:
    """解析 DISCORD_CHANNELS 环境变量为 频道ID -> 语义名 映射"""
    result: Dict[str, str] = {}
    for item in raw.split(","):
        cid, _, name = item.partition("=")
        cid = cid.strip()
        if cid:
            result[cid] = name.strip() or cid
    return result


TARGET_CHANNELS: Dict[str, str] = _parse_channel_map(os.getenv("DISCORD_CHANNELS", ""))

# 频道时间过滤配置 (频道 ID -> 最大采集天数, None 表示不限制)
CHANNEL_TIME_FILTERS: Dict[str, int] = {}
for _item in os.getenv("DISCORD_CHANNEL_MAX_AGE", "").split(","):
    _cid, _, _days = _item.partition("=")
    if _cid.strip() and _days.strip().isdigit():
        CHANNEL_TIME_FILTERS[_cid.strip()] = int(_days.strip())
# ==========================================

def load_history() -> dict:
    if not os.path.exists(HISTORY_FILE): return {}
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f: 
            data = json.load(f)
        if isinstance(data, dict):
            result = {}
            for k, v in data.items():
                if isinstance(v, dict):
                    result[str(k)] = {"timestamp": int(v.get("timestamp", 0)), "title_prefix": v.get("title_prefix", "")}
                else:
                    result[str(k)] = {"timestamp": int(v) if isinstance(v, (int, float)) else 0, "title_prefix": ""}
            return result
        if isinstance(data, list):
            return {str(i): {"timestamp": 0, "title_prefix": ""} for i in data}
        return {}
    except Exception as e:
        print(f"[WARN] 历史记录读取异常: {e}")
        return {}

def save_history(history_map: dict) -> None:
    try:
        trimmed = trim_history_map({k: v["timestamp"] for k, v in history_map.items()}, HISTORY_MAX_SIZE)
        migrated = {}
        for k, ts in trimmed.items():
            if k in history_map:
                migrated[k] = {"timestamp": ts, "title_prefix": history_map[k].get("title_prefix", "")}
            else:
                migrated[k] = {"timestamp": ts, "title_prefix": ""}
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f: 
            json.dump(migrated, f, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] 历史记录保存失败: {e}")

async def progress_spinner(msg: str, stop_event: asyncio.Event):
    """
    进度提示：仅在任务开始时打一行标记，结束时不擦除。
    之前的实现使用 \\r\\033[K 行内更新会**覆盖**掉 AI 完成后打印的"写入成功"日志，
    导致用户在终端看不到任何反馈（虽然日志文件里是有的）。
    现在简化为"开始提示 -> 等待 -> 自动结束"，保证所有 print 行都能正常显示。
    """
    sys.stdout.write(f"  [..] 开始 {msg}\n")
    sys.stdout.flush()
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    # 不再做任何输出清理，避免覆盖其他 print

# --- 核心抓取逻辑 (Discord 论坛下潜) ---

async def fetch_thread_comments(thread_id: str, headers: dict) -> str:
    """下潜抓取论坛帖子内的所有回复评论"""
    url = f"https://discord.com/api/v9/channels/{thread_id}/messages?limit=50"
    try:
        await rate_limit(1.5)
        res = requests.get(url, headers=headers, proxies=PROXIES, timeout=15)
        if res.status_code != 200: return ""
        
        msgs = res.json()
        if not isinstance(msgs, list): return ""
        
        # Discord 返回的是倒序（最新的在前面），我们需要反转成正常阅读顺序
        msgs.reverse()
        comments = []
        
        for m in msgs:
            # 过滤掉楼主的首条消息（防止与外层的 first_message 重复）
            if str(m.get("id")) == str(thread_id): 
                continue
                
            author = m.get("author", {}).get("username", "Unknown")
            content = m.get("content", "").strip()
            
            if content:
                clean_content = content.replace('\n', ' ')
                comments.append(f"[{author}]回复: {clean_content}")
                
        if not comments: return ""
        
        # 组装并保护长度
        final_text = "\n\n【楼内玩家讨论】\n" + "\n".join(comments)
        if len(final_text) > 5000:
            return final_text[:5000] + "\n...(回复过多，已安全截断)"
        return final_text
    except Exception as e:
        return f"\n(评论拉取异常: {e})"

async def fetch_channel_messages(channel_id: str, channel_name: str) -> List[Dict[str, Any]]:
    """抓取论坛频道列表，并调用下潜"""
    if not DISCORD_USER_TOKEN:
        print("[ERR] 缺少 DISCORD_USER_TOKEN 环境变量，请在 .env 中配置！")
        return []
    headers = {
        "Authorization": DISCORD_USER_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    parsed_posts = []
    print(f"\n[🔎] 正在读取频道 [{channel_name}] ...")
    
    try:
        url_forum = f"https://discord.com/api/v9/channels/{channel_id}/threads/search?archived=false&limit={DISCORD_FETCH_LIMIT}"
        res_forum = requests.get(url_forum, headers=headers, proxies=PROXIES, timeout=15)
        
        if res_forum.status_code == 401:
            print("[ERR] Token 鉴权失败！请检查 DISCORD_USER_TOKEN 配置。")
            return []
        
        if res_forum.status_code == 403:
            print(f"[ERR] Token 可能已过期 (403 Forbidden)。请更新 DISCORD_USER_TOKEN。")
            return []
            
        if res_forum.status_code != 200:
            print(f"[ERR] Discord API 请求失败，状态码: {res_forum.status_code}。请检查网络和 Token。")
            return []

        try:
            data = res_forum.json()
        except Exception as e:
            print(f"[ERR] Discord API 返回了无效的 JSON 响应: {e}")
            print(f"[DEBUG] 响应内容前300字符: {res_forum.text[:300]}")
            return []

        if "threads" not in data:
            print(f"[WARN] 频道 {channel_name} 不是标准论坛格式或请求失败，状态码: {res_forum.status_code}")
            print(f"[DEBUG] 响应 keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            print(f"[DEBUG] 响应前200字符: {str(data)[:200]}")
            return []

        threads = data.get("threads", [])
        first_msgs = {m["id"]: m for m in data.get("first_messages", [])}
        history = load_history()

        for thread in threads:
            t_id = thread.get("id")
            title = thread.get("name", "")

            if t_id in history:
                title_prefix = title[:6] if title else ""
                stored = history[t_id]
                if stored.get("title_prefix") and title_prefix and stored["title_prefix"] == title_prefix:
                    print(f"  -> 触碰历史边界 [{t_id}]（标题验证通过），停止。")
                    break
                elif not stored.get("title_prefix"):
                    print(f"  -> 触碰历史边界 [{t_id}]（旧版无标题），停止。")
                    break
                else:
                    print(f"  -> ID命中但标题不符 [{t_id}]，继续。")
                    continue

            op_msg = first_msgs.get(t_id, {})
            op_content = op_msg.get("content", "").strip()
            op_author = op_msg.get("author", {}).get("username", "Unknown")

            print(f"  [↓] 发现新贴: {title[:15]}... 正在抓取楼内回复")
            comments_text = await fetch_thread_comments(t_id, headers)

            full_text = f"【标题】{title}\n【楼主 {op_author}】: {op_content}{comments_text}".strip()

            if t_id and full_text:
                # 从 thread_metadata 获取真正的创建时间
                ts_str = thread.get("thread_metadata", {}).get("create_timestamp", "")
                if ts_str:
                    ts_str = ts_str.split("+")[0]
                    dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                    post_time_ms = int(dt.timestamp() * 1000)
                else:
                    post_time_ms = int(time.time() * 1000)

                parsed_posts.append({
                    "comment_id": t_id,
                    "author": op_author,
                    "content": full_text,
                    "post_time_ms": post_time_ms,
                    "url": f"https://discord.com/channels/{DISCORD_GUILD_ID}/{t_id}",
                    "platform": channel_name,
                    "title_prefix": title[:6] if title else ""
                })

        return parsed_posts

    except Exception as e:
        print(f"[ERR] Discord 通信层异常: {e}")
        return []

# --- AI 与 数据处理 ---

async def async_process_post(post: Dict[str, Any], idx: int, total: int, semaphore: asyncio.Semaphore):
    async with semaphore:
        stop_event = asyncio.Event()
        # 恢复花哨的转圈动画
        spinner_task = asyncio.create_task(progress_spinner(f"[进度 {idx}/{total}] 正在由 AI 诊断帖子: {post['author']}", stop_event))
        
        loop = asyncio.get_event_loop()
        try:
            # 提取主帖标题作为上下文，帮助AI理解评论背景
            content = post['content']
            context = ""
            # Discord帖子内容格式：【标题】xxx\n【楼主 xxx】: yyy...，提取标题部分作为上下文
            if '【标题】' in content:
                title_start = content.find('【标题】') + len('【标题】')
                title_end = content.find('\n', title_start)
                if title_end == -1:
                    title_end = len(content)
                context = content[title_start: title_end].strip()[:150]  # 提取标题，最多150字
            
            ai_res = await asyncio.wait_for(
                loop.run_in_executor(None, analyze_micro_feedback, content, context),
                timeout=60
            )
            
            fields = build_feishu_record(post, ai_res, remark="Discord API 直抓")
            # 统一写入总表（DISCORD_FEISHU_TABLE_ID 已废弃，write_to_feishu 默认回退到 FEISHU_TABLE_ID）
            await loop.run_in_executor(None, write_to_feishu, fields, None)
            
            # 正常完成，停止动画
            stop_event.set()
            await spinner_task
            
            return post["comment_id"]
            
        except Exception as e:
            # 异常发生：先停止转圈动画，再打印报错，防止互相覆盖！
            stop_event.set()
            await spinner_task
            print(f"\n[ERR] 处理帖子 {post.get('comment_id')} 时异常: {e}")
            return None

async def main_pipeline():
    if not DISCORD_USER_TOKEN:
        print("[ERR] 缺少 DISCORD_USER_TOKEN 环境变量，请在 .env 中配置！")
        return
    history = load_history()
    print(f"\n[🕵️] Discord 个人账号潜水员启动 | 缓存: {len(history)} 条")
    
    all_new_posts = []
    
    for channel_id, channel_name in TARGET_CHANNELS.items():
        posts = await fetch_channel_messages(channel_id, channel_name)

        max_age_days = CHANNEL_TIME_FILTERS.get(channel_id)
        if max_age_days is not None:
            cutoff_ms = int((time.time() - max_age_days * 86400) * 1000)
            before_count = len(posts)
            posts = [p for p in posts if p.get("post_time_ms", 0) >= cutoff_ms]
            if before_count > len(posts):
                print(f"[TIME] 频道 {channel_name} 按时间过滤: {before_count} -> {len(posts)} 条 (保留最近{max_age_days}天)")

        new_posts = [p for p in reversed(posts) if p["comment_id"] not in history]
        
        if new_posts:
            print(f"[OK] 频道 {channel_name} 发现 {len(new_posts)} 条新动态。")
            all_new_posts.extend(new_posts)
        else:
            print(f"[SKIP] 频道 {channel_name} 无新动态。")
            
        await rate_limit(3)

    if not all_new_posts:
        print("\n[🎉] 所有 Discord 频道均无新内容，巡逻结束。")
        return

    print(f"\n[⚡] 正在并发处理三大频道的共 {len(all_new_posts)} 条微观动态 (AI双并发)...")
    sem = asyncio.Semaphore(3)
    # 传入 idx 和 len 用于进度显示
    tasks = [async_process_post(p, i, len(all_new_posts), sem) for i, p in enumerate(all_new_posts, 1)]
    results = await asyncio.gather(*tasks)
    
    new_ts_map = {}
    for p in all_new_posts:
        new_ts_map[p["comment_id"]] = {"timestamp": p["post_time_ms"], "title_prefix": p.get("title_prefix", "")}
    for rid in results:
        if rid and isinstance(rid, str) and rid in new_ts_map:
            history[rid] = new_ts_map[rid]
    save_history(history)
    print(f"\n[🎉] Discord 增量抓取完成！")

if __name__ == "__main__":
    try:
        asyncio.run(main_pipeline())
    except KeyboardInterrupt:
        print("\n[STOP] 用户强制停止。")
