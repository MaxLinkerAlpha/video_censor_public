#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bilibili_client.py - B站字幕获取模块 (v4.3.0)

功能：
- 获取视频信息（标题、作者、时长、发布时间等）
- 获取视频字幕列表（手动字幕和AI字幕）
- 下载并转换字幕为文本
- 请求限速控制

使用方法：
    from platforms.bilibili_client import BilibiliSubtitleFetcher

    fetcher = BilibiliSubtitleFetcher(cookie)
    video_info, error = fetcher.fetch("BV1234567890")
"""

import re
import time
import random
import threading
import requests
from typing import Optional, Tuple, List, Dict, Any

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core"))
from config import CONFIG, BILI_COOKIE


_last_bili_request_time = 0
_bili_lock = threading.Lock()


def _rate_limited_bili_request():
    """
    B站请求限速

    说明：
    - 使用全局锁确保线程安全
    - 根据配置的请求间隔进行限速
    - 添加随机抖动避免请求过于规律
    """
    global _last_bili_request_time
    with _bili_lock:
        interval = CONFIG.get('bili_request_interval', 2)
        current = time.time()
        elapsed = current - _last_bili_request_time
        if elapsed < interval:
            time.sleep(interval - elapsed + random.uniform(0, 1))
        _last_bili_request_time = time.time()


class BilibiliSubtitleFetcher:
    """
    Bilibili 视频字幕获取类

    功能：
    - 获取视频信息（标题、作者、时长、发布时间等）
    - 获取视频字幕列表（手动字幕和AI字幕）
    - 下载并转换字幕为文本
    """

    _cookie_validated = False
    _cookie_error_shown = False

    def __init__(self, cookie: str = ""):
        """
        初始化 B站字幕获取器

        参数：
            cookie: B站 Cookie 字符串（可选，默认使用配置中的 Cookie）
        """
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Referer': 'https://www.bilibili.com',
            'Origin': 'https://www.bilibili.com',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site'
        })
        if cookie:
            self.session.headers['Cookie'] = cookie

    def validate_cookie(self) -> bool:
        """
        验证 Cookie 是否有效

        通过访问用户信息 API 来验证 Cookie 有效性

        返回：
            True 如果有效，False 如果失效
        """
        if BilibiliSubtitleFetcher._cookie_validated:
            return True

        try:
            url = "https://api.bilibili.com/x/web-interface/nav"
            resp = self.session.get(url, timeout=10)
            data = resp.json()

            if data.get('code') == 0:
                BilibiliSubtitleFetcher._cookie_validated = True
                return True
            else:
                self._show_cookie_error()
                return False
        except Exception as e:
            print(f"[WARN] Cookie验证请求失败: {e}")
            return False

    def _show_cookie_error(self):
        """显示 Cookie 失效错误提示"""
        if BilibiliSubtitleFetcher._cookie_error_shown:
            return

        BilibiliSubtitleFetcher._cookie_error_shown = True
        print("""
[ERROR] Bilibili Cookie 已失效或权限不足

请更新 .env 文件中的 BILIBILI_COOKIE

获取新 Cookie 方法：
1. 登录 Bilibili 网页版 (https://www.bilibili.com)
2. 按 F12 打开开发者工具 -> Application -> Cookies
3. 复制 SESSDATA 值（还需要 bili_jct、DedeUserID 等）
4. 更新到 refactor/.env 文件中的 BILIBILI_COOKIE
""")

    def get_video_info_by_api(self, bvid: str) -> Tuple[List[str], Optional[str], dict]:
        """
        通过 API 获取视频信息

        参数：
            bvid: B站视频 BV 号

        返回：
            (CID列表, AID, 元数据字典)
        """
        self.validate_cookie()
        _rate_limited_bili_request()
        url = "https://api.bilibili.com/x/web-interface/view"
        params = {"bvid": bvid}
        resp = None  # v5.3.1: 预初始化, 避免网络异常时 except 中访问未定义变量
        try:
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                print(f"[DEBUG] 状态码: {resp.status_code}, 内容: {resp.text[:200] if resp.text else '空'}")
                return [], None, {
                    'title': '未知标题',
                    'author': '未知作者',
                    'mid': 0,
                    'publish_time': 0,
                    'aid': 0,
                    'duration': 0,
                    'views': 0,
                    'likes': 0,
                    'comments': 0,
                    'danmaku': 0
                }
            data = resp.json()
            if data.get('code') == 0:
                d = data.get('data', {})
                pages = d.get('pages', [])
                cids = [str(p.get('cid')) for p in pages if p.get('cid')]
                aid = str(d.get('aid', ''))
                stat = d.get('stat', {})
                metadata = {
                    'title': d.get('title', '未知标题'),
                    'author': d.get('owner', {}).get('name', '未知作者'),
                    'mid': d.get('owner', {}).get('mid', 0),
                    'publish_time': d.get('pubdate', 0),
                    'aid': aid,
                    'duration': d.get('duration', 0),
                    'views': stat.get('view', 0),
                    'likes': stat.get('like', 0),
                    'comments': stat.get('reply', 0),
                    'danmaku': stat.get('danmaku', 0)
                }
                return cids, aid, metadata
            else:
                print(f"[DEBUG] API返回错误: code={data.get('code')}, message={data.get('message')}")
        except Exception as e:
            print(f"[DEBUG] API获取视频信息失败: {e}, 响应内容: {resp.text[:200] if resp else '无响应'}")
        return [], None, {
            'title': '未知标题',
            'author': '未知作者',
            'mid': 0,
            'publish_time': 0,
            'aid': 0,
            'duration': 0,
            'views': 0,
            'likes': 0,
            'comments': 0,
            'danmaku': 0
        }

    def get_subtitle_list(self, bvid: str, cid: str) -> list:
        """获取字幕列表"""
        endpoints = [
            "https://api.bilibili.com/x/player/v2",
            "https://api.bilibili.com/x/player/wbi/v2"
        ]
        subtitle_map = {}
        params = {"bvid": bvid, "cid": cid}

        for url in endpoints:
            try:
                resp = self.session.get(url, params=params, timeout=10)
                data = resp.json()
                if data.get('code') != 0:
                    continue
                data_sec = data.get('data')
                if data_sec is None:
                    continue
                subtitle_info = data_sec.get('subtitle') if isinstance(data_sec, dict) else {}
                if subtitle_info is None:
                    subtitle_info = {}
                subs = subtitle_info.get('subtitles', [])
                for sub in subs:
                    sub_url = sub.get('subtitle_url', '')
                    key = sub.get('id_str') or sub_url
                    if key:
                        subtitle_map[key] = sub
            except Exception as e:
                print(f"[DEBUG] 获取字幕列表失败: {e}")

        return list(subtitle_map.values())

    def normalize_subtitle_url(self, subtitle_url: str) -> str:
        """规范化字幕URL"""
        if subtitle_url.startswith('//'):
            return 'https:' + subtitle_url
        return subtitle_url

    def get_auto_subtitle_list(self, bvid: str, cid: str) -> list:
        """获取自动生成字幕列表"""
        endpoints = [
            "https://api.bilibili.com/x/player/videoshot",
            "https://api.bilibili.com/x/player/v2",
            "https://api.bilibili.com/x/player/wbi/v2",
            "https://api.bilibili.com/x/web-interface/wbi/view",
            "https://api.bilibili.com/x/web-interface/view"
        ]
        subtitle_map = {}

        for url in endpoints:
            try:
                resp = self.session.get(url, params={"bvid": bvid, "cid": cid}, timeout=10)
                data = resp.json()

                if data.get('code') != 0:
                    continue

                data_sec = data.get('data')
                if data_sec is None:
                    continue
                subtitle_info = data_sec.get('subtitle') if isinstance(data_sec, dict) else {}
                if subtitle_info is None:
                    subtitle_info = {}

                auto_subs = subtitle_info.get('subtitles', [])
                for sub in auto_subs:
                    sub_url = sub.get('subtitle_url', '')
                    if sub_url:
                        key = sub.get('id_str') or sub_url
                        subtitle_map[key] = sub

                ai_sub = subtitle_info.get('ai_subtitle', {})
                if ai_sub and ai_sub.get('url'):
                    subtitle_map['ai_subtitle'] = {
                        'subtitle_url': ai_sub['url'],
                        'lan': ai_sub.get('lan', 'zh'),
                        'lan_doc': ai_sub.get('lan_doc', '中文'),
                        'ai_type': 1
                    }

                if subtitle_info:
                    auto_sub = subtitle_info.get('auto_subtitle')
                    if auto_sub and auto_sub.get('url'):
                        subtitle_map['auto_subtitle'] = {
                            'subtitle_url': auto_sub['url'],
                            'lan': auto_sub.get('lan', 'zh'),
                            'lan_doc': auto_sub.get('lan_doc', '中文'),
                            'ai_type': 1
                        }

            except Exception as e:
                print(f"[DEBUG] 获取自动生成字幕失败: {e}")

        return list(subtitle_map.values())

    def extract_prod_id(self, subtitle_url: str) -> str:
        """从字幕URL提取prod_id"""
        match = re.search(r'prod/([a-f0-9]+)', subtitle_url or '')
        return match.group(1) if match else ''

    def build_subtitle_candidates(self, bvid: str, cids: List[str], aid: str) -> List[Dict[str, Any]]:
        """构建字幕候选列表"""
        candidates: List[Dict[str, Any]] = []
        aid_str = str(aid) if aid else ""

        for cid in cids:
            subs = self.get_subtitle_list(bvid, cid)
            auto_subs = self.get_auto_subtitle_list(bvid, cid)
            all_subs = subs + auto_subs

            for sub in all_subs:
                raw_url = sub.get('subtitle_url', '')
                sub_url = self.normalize_subtitle_url(raw_url)
                if not sub_url:
                    continue
                prod_id = self.extract_prod_id(sub_url)
                aid_match = bool(aid_str and prod_id and prod_id.startswith(aid_str))
                lang = str(sub.get('lan', '')).lower()
                lang_doc = str(sub.get('lan_doc', ''))
                is_zh = lang.startswith('zh') or ('中文' in lang_doc)
                ai_type = sub.get('ai_type')
                is_manual = (ai_type == 0 or ai_type is None)
                score = (
                    (3 if aid_match else 0) +
                    (2 if is_zh else 0) +
                    (1 if is_manual else 0) +
                    (1 if not prod_id else 0)
                )
                candidates.append({
                    'cid': cid,
                    'sub': sub,
                    'subtitle_url': sub_url,
                    'prod_id': prod_id,
                    'aid_match': aid_match,
                    'score': score
                })

        if not candidates:
            return []

        has_aid_match = any(c['aid_match'] for c in candidates)
        filtered: List[Dict[str, Any]] = []
        for c in candidates:
            if has_aid_match and c['prod_id'] and not c['aid_match']:
                continue
            filtered.append(c)

        dedup = {}
        for c in filtered:
            key = (c['cid'], c['subtitle_url'])
            if key not in dedup or c['score'] > dedup[key]['score']:
                dedup[key] = c

        return sorted(
            dedup.values(),
            key=lambda x: (x['score'], x['aid_match']),
            reverse=True
        )

    def download_subtitle(self, subtitle_url: str) -> Optional[dict]:
        """下载字幕数据"""
        subtitle_url = self.normalize_subtitle_url(subtitle_url)

        try:
            resp = self.session.get(subtitle_url, timeout=10)
            if resp.status_code == 200 and resp.text.strip():
                return resp.json()
        except requests.RequestException as e:
            print(f"[DEBUG] 字幕下载失败: {e}")
        except (ValueError, KeyError) as e:
            print(f"[DEBUG] 字幕JSON解析失败: {e}")
        return None

    def convert_to_text(self, subtitle_data: dict) -> str:
        """将字幕数据转换为纯文本"""
        if not subtitle_data or 'body' not in subtitle_data:
            return ""

        body = subtitle_data.get('body', [])
        if not isinstance(body, list):
            return ""

        lines = []
        for item in body:
            if isinstance(item, dict):
                content = item.get('content', '')
                if content:
                    lines.append(content.replace('\n', ' '))

        return ' '.join(lines)

    def fetch(self, bvid: str) -> Tuple[Optional[dict], str]:
        """
        获取B站视频字幕

        参数：
            bvid: B站视频 BV 号

        返回：
            (视频信息字典, 错误信息)

        v5.3.1: 整体重试 3 次, 缓解 B站 API 网络瞬时错误 (10054 连接重置 / Read timeout)
        """
        import time as _time
        last_err = ""
        for attempt in range(1, 4):
            try:
                result, err = self._fetch_once(bvid)
                if result is not None or not err:
                    return result, err
                # 网络类错误才重试 (非"无字幕"类正常返回)
                last_err = err
                if "字幕获取异常" in err or "无法获取视频CID" in err:
                    print(f"    [WARN] fetch({bvid}) 第{attempt}/3次失败: {err[:80]}")
                    if attempt < 3:
                        _time.sleep(2 * attempt)
                    continue
                return result, err
            except Exception as e:
                last_err = str(e)
                if attempt < 3:
                    print(f"    [WARN] fetch({bvid}) 第{attempt}/3次异常: {type(e).__name__}: {e}")
                    _time.sleep(2 * attempt)
        return None, last_err or "重试3次仍失败"

    def _fetch_once(self, bvid: str) -> Tuple[Optional[dict], str]:
        """单次获取B站视频字幕 (fetch 的原始实现)"""
        try:
            bvid = bvid.strip()
            match = re.search(r'(BV[a-zA-Z0-9]{10})', bvid, re.IGNORECASE)
            if match:
                bvid = match.group(1)

            cids, aid, metadata = self.get_video_info_by_api(bvid)
            if not cids:
                return None, "无法获取视频CID"

            subtitle_candidates = self.build_subtitle_candidates(bvid, cids, aid or "")
            if not subtitle_candidates:
                result = {
                    'bvid': bvid,
                    'title': metadata['title'],
                    'author': metadata['author'],
                    'mid': metadata.get('mid', 0),
                    'publish_time': metadata['publish_time'],
                    'duration': metadata['duration'],
                    'views': metadata.get('views', 0),
                    'likes': metadata.get('likes', 0),
                    'comments': metadata.get('comments', 0),
                    'danmaku': metadata.get('danmaku', 0),
                    'subtitle_text': '',
                    'url': f"https://www.bilibili.com/video/{bvid}/",
                    'has_subtitle': False
                }
                return result, ""

            for candidate in subtitle_candidates:
                data = self.download_subtitle(candidate['subtitle_url'])
                if not data:
                    continue
                text = self.convert_to_text(data)
                if text.strip():
                    result = {
                        'bvid': bvid,
                        'title': metadata['title'],
                        'author': metadata['author'],
                        'mid': metadata.get('mid', 0),
                        'publish_time': metadata['publish_time'],
                        'duration': metadata['duration'],
                        'views': metadata.get('views', 0),
                        'likes': metadata.get('likes', 0),
                        'comments': metadata.get('comments', 0),
                        'danmaku': metadata.get('danmaku', 0),
                        'subtitle_text': text,
                        'url': f"https://www.bilibili.com/video/{bvid}/",
                        'has_subtitle': True
                    }
                    return result, ""

            result = {
                'bvid': bvid,
                'title': metadata['title'],
                'author': metadata['author'],
                'mid': metadata.get('mid', 0),
                'publish_time': metadata['publish_time'],
                'duration': metadata['duration'],
                'views': metadata.get('views', 0),
                'likes': metadata.get('likes', 0),
                'comments': metadata.get('comments', 0),
                'danmaku': metadata.get('danmaku', 0),
                'subtitle_text': '',
                'url': f"https://www.bilibili.com/video/{bvid}/",
                'has_subtitle': False
            }
            return result, ""

        except Exception as e:
            return None, f"B站字幕获取异常: {str(e)}"
