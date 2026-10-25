import os
import re
import json
import time
import datetime as dt_module
import requests
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional

import pandas as pd
from playwright.sync_api import sync_playwright
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ================= 🚀 代理配置 (v3.4.2) =================
# 导入自动代理检测模块
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from console_utils import detect_proxy_port, get_proxy_url

_PROXY_URL = get_proxy_url()
if _PROXY_URL:
    print(f"  [INFO] YouTube API will use proxy: {_PROXY_URL}")
else:
    print("  [WARN] No proxy available, YouTube API may fail")
# ==========================================================

# ================= 🚀 核心配置区 =================
# 从环境变量读取敏感信息（避免硬编码泄露）
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CONFIG, BILIBILI_EXCEL_PATH, YOUTUBE_EXCEL_PATH

YT_API_KEY = CONFIG.get('yt_api_key', '')
YT_KEYWORD = os.getenv("YT_KEYWORD", "your_game_keyword")
BILI_KEYWORD = os.getenv("BILI_KEYWORD", "你的游戏名")
BILI_HEADLESS = True  # B站抓取是否使用无头模式

# 默认追溯天数 (仅在本地没有Excel历史文件时生效，一旦有了文件，将自动智能无缝衔接)
FETCH_DAYS = 30
MAX_BILI_PAGES = 5 # B站向下翻的页数

# ====================================================

# ================= 🟢 飞书多维表格 Bitable API 模块 (v4.1.0 混合模式) =================
from feishu_sync import FeishuSync, BILI_BITABLE_TOKEN, BILI_TABLE_ID, YT_TABLE_ID, FEISHU_APP_ID, FEISHU_APP_SECRET
from language_utils import normalize_language  # v5.1.0: BCP-47 -> ISO 639-1

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


def _get_tenant_token(app_id: str, app_secret: str) -> str:
    """获取 tenant_access_token（项目 bot 身份）"""
    url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
    res = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
    data = res.json()
    if data.get("code") == 0:
        return data["tenant_access_token"]
    print(f"  [ERR] 获取飞书 Token 失败: {data.get('msg')}")
    return ""


def push_to_bitable(app_token: str, table_id: str, new_df: pd.DataFrame):
    """使用 requests + tenant_access_token 批量写入新记录到飞书多维表格"""
    if new_df.empty:
        return

    # 获取 bot token
    token = _get_tenant_token(FEISHU_APP_ID, FEISHU_APP_SECRET)
    if not token:
        print("  [ERR] 无法获取飞书认证，写入中止")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 获取字段列表 (用 requests + bot token, 替代 lark-cli 避免限制)
    from feishu_sync import FeishuSync
    _sync = FeishuSync()
    _sync.set_bitable_token(app_token)
    feishu_fields = _sync.get_fields(table_id)  # 返回 {name: type_str}
    if not feishu_fields:
        print("  [ERR] 无法获取飞书字段列表，写入中止")
        return

    # v4.1.2 修复: 支持字符串和整数 type (lark-cli=字符串, OpenAPI=整数)
    # 数字表示: 1=text, 2=number, 3=select, 4=checkbox, 5=datetime, 7=auto_number, 15=formula
    TYPE_STR_TO_INT = {
        # 字符串兼容 (lark-cli)
        "text": 1, "number": 2, "select": 3, "checkbox": 4,
        "datetime": 5, "created_at": 6, "auto_number": 7, "formula": 8,
        "multi_select": 3,
        # 整数兼容 (OpenAPI)
        1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8,
        1005: 7,  # auto_number 在 OpenAPI 是 1005
        1001: 6,  # created_time
        15: 8,    # formula
    }
    feishu_fields = {k: TYPE_STR_TO_INT.get(v, TYPE_STR_TO_INT.get(str(v).lower(), 1)) for k, v in feishu_fields.items()}
    df_columns = set(new_df.columns)
    valid_columns = [c for c in new_df.columns if c in feishu_fields]
    skip_columns = df_columns - set(valid_columns)
    if skip_columns:
        print(f"  [WARN] Excel有但飞书没有的列已忽略: {skip_columns}")

    if not valid_columns:
        print("  [ERR] 没有可用的匹配列，写入中止")
        return

    # 构建 batch_create records
    records = []
    for _, row in new_df.iterrows():
        fields_dict = {}
        for col in valid_columns:
            val = row[col]
            ft = feishu_fields.get(col, 1)

            if ft == 1005 or ft == 15:
                continue
            elif ft == 7:
                continue
            elif pd.isna(val) or val is None:
                # v5.4.5: DATETIME 字段无值时跳过整个字段 —— 写空串会触发 DatetimeFieldConvFail
                if ft == 5:
                    continue
                val = ""
            elif ft == 5:
                # v4.1.1 修复: 飞书 DATETIME 字段要求合法毫秒时间戳 (>0 且 非 1970)
                # 1) 解析失败/NaT/None -> 不发送该字段(避免 DatetimeFieldConvFail)
                # 2) Excel 里的日期是 GMT+8 本地时间, 解析后需附加 tz=GMT+8 再转 UTC 毫秒
                # 3) 已经是毫秒级整数 (>1e12) 直接用; 秒级 (<=1e10) 乘 1000
                skip_field = False
                try:
                    if pd.isna(val) or val is None:
                        skip_field = True
                    elif isinstance(val, str):
                        s = val.strip()
                        if not s or s.lower() in ("nat", "none", "null"):
                            skip_field = True
                        else:
                            # 解析字符串日期, 强制当作 GMT+8 本地时间
                            dt = pd.to_datetime(s, errors="coerce")
                            if pd.isna(dt):
                                skip_field = True
                            else:
                                ts = self_dt_to_gmt8_ms(dt)
                                if ts <= 0:
                                    skip_field = True
                                else:
                                    val = ts
                    elif isinstance(val, (int, float)):
                        fv = float(val)
                        if pd.isna(val) or fv <= 0:
                            skip_field = True
                        elif fv > 1e12:
                            # 已是毫秒
                            val = int(fv)
                        else:
                            # 秒级 -> 毫秒
                            val = int(fv * 1000)
                    elif isinstance(val, (pd.Timestamp, dt_module.datetime, dt_module.date)):
                        try:
                            ts = self_dt_to_gmt8_ms(val)
                            if ts <= 0:
                                skip_field = True
                            else:
                                val = ts
                        except (OSError, ValueError, OverflowError):
                            skip_field = True
                    else:
                        skip_field = True
                except (ValueError, TypeError, OSError, OverflowError):
                    skip_field = True

                if skip_field:
                    # 不发送此字段, 飞书会保持空值, 避免 DatetimeFieldConvFail
                    continue
            elif ft in (2, 4):
                try:
                    if pd.isna(val):
                        val = 0
                    else:
                        val = int(float(val))
                        if val != val:  # 检查 NaN
                            val = 0
                except (ValueError, TypeError, OverflowError):
                    val = 0
            else:
                val = str(val) if not pd.isna(val) else ""

            fields_dict[col] = val

        records.append({"fields": fields_dict})

    # v4.8.0: 分批 + 4 并发 (飞书 batch_create 单次最多 1000 条)
    if not records:
        print(f"  [WARN] 没有可写入的记录")
        return

    BATCH_SIZE = 1000
    CONCURRENCY = 4
    batches = [records[i:i+BATCH_SIZE] for i in range(0, len(records), BATCH_SIZE)]
    print(f"  [INFO] 分 {len(batches)} 批 × {BATCH_SIZE} 条, {CONCURRENCY} 并发")

    def push_one_batch(batch):
        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
        try:
            res = requests.post(url, headers=headers, json={"records": batch}, timeout=60)
            data = res.json()
            if data.get("code") == 0:
                return len(batch), None
            return 0, f"code={data.get('code')}, msg={data.get('msg')}"
        except Exception as e:
            return 0, str(e)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    success = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(push_one_batch, b): i for i, b in enumerate(batches)}
        for f in as_completed(futures):
            n, err = f.result()
            if err:
                failed += 1
                print(f"  [ERR] 批次失败: {err[:200]}")
            else:
                success += n
    print(f"  [OK] 成功向多维表格子表追加 {success}/{len(records)} 条 (失败 {failed} 批)")


def self_dt_to_gmt8_ms(val) -> int:
    """将任意时间值转为毫秒时间戳 (按 GMT+8 时区计算)。
    - Excel/抓取数据里的日期是 GMT+8 本地时间字符串 (无时区信息)
    - 飞书 DATETIME 字段要求 UTC 毫秒时间戳
    - 若 val 带 tz 则先转 UTC, 否则按 GMT+8 处理
    """
    try:
        from datetime import timezone, timedelta
        gmt8 = timezone(timedelta(hours=8))

        if isinstance(val, pd.Timestamp):
            # 带 tz -> 转 UTC; 不带 tz -> 当作 GMT+8
            if val.tz is None:
                val = val.tz_localize(gmt8)
            else:
                val = val.tz_convert("UTC")
            return int(val.timestamp() * 1000)

        if isinstance(val, dt_module.datetime):
            if val.tzinfo is None:
                val = val.replace(tzinfo=gmt8)
            utc_dt = val.astimezone(timezone.utc)
            return int(utc_dt.timestamp() * 1000)

        if isinstance(val, dt_module.date):
            # date 只有年月日, 强制 00:00:00 GMT+8
            d = dt_module.datetime(val.year, val.month, val.day, tzinfo=gmt8)
            return int(d.timestamp() * 1000)
    except Exception:
        return 0
    return 0

# ================= 🧠 智能序列化与增量提取模块 =================
def save_and_get_new_records(df: pd.DataFrame, file_path: str, unique_key: str, sort_key: str) -> pd.DataFrame:
    if df.empty: return df
    
    # 强制去重与文本化，防止隐藏空格导致匹配失效
    df = df.drop_duplicates(subset=[unique_key]).copy()
    df[unique_key] = df[unique_key].astype(str).str.strip()
    
    new_records = df.copy()
    max_index = 0
    old_df = pd.DataFrame()
    
    if os.path.exists(file_path):
        print(f"  [>] 发现本地历史数据，正在比对增量: {file_path}")
        try:
            old_df = pd.read_excel(file_path)
            if not old_df.empty and unique_key in old_df.columns:
                old_df[unique_key] = old_df[unique_key].astype(str).str.strip()
                new_records = df[~df[unique_key].isin(old_df[unique_key])].copy()
                
                if '序号' in old_df.columns:
                    max_index = pd.to_numeric(old_df['序号'], errors='coerce').max()
                    max_index = 0 if pd.isna(max_index) else int(max_index)
                else:
                    max_index = len(old_df)
                    old_df.insert(0, '序号', range(1, len(old_df) + 1))
        except Exception as e:
            print(f"  [WARN] 读取原有文件失败: {e}")
            old_df = pd.DataFrame()

    if new_records.empty:
        print("  [INFO] 现有数据已是最新，未发现增量内容。")
        return new_records

    if sort_key in new_records.columns:
        new_records = new_records.sort_values(sort_key, ascending=True)

    start_index = max_index + 1
    new_records.insert(0, '序号', range(start_index, start_index + len(new_records)))
    df_to_save = pd.concat([old_df, new_records], ignore_index=True) if not old_df.empty else new_records.copy()
    
    try:
        df_to_save.to_excel(file_path, index=False, engine='openpyxl')
        print(f"  [OK] 本地 Excel 已更新 (本次新增 {len(new_records)} 条，分配序号 {start_index} ~ {start_index + len(new_records) - 1})")
    except PermissionError:
        print(f"\n  [🚨 致命错误] Excel 文件被占用！已阻断飞书推送防污染。\n")
        return pd.DataFrame() 
    return new_records

# ================= 🕒 YouTube 动态时间窗获取器 =================
def get_yt_published_after(file_path: str, fallback_days: int) -> str:
    """读取本地最新视频的时间，精准定位 API 抓取起点，防止 500 条截断漏抓"""
    now_utc = datetime.now(timezone.utc)
    if os.path.exists(file_path):
        try:
            df = pd.read_excel(file_path)
            if not df.empty and 'Published At' in df.columns:
                latest_str = df['Published At'].dropna().max()
                # 格式: YYYY/MM/DD HH:MM:SS (本地时间 GMT+8)
                dt_gmt8 = datetime.strptime(str(latest_str), "%Y/%m/%d %H:%M:%S")
                # 转为 UTC 时间并回退 12 小时作为缓冲，确保绝对不漏抓
                dt_utc = (dt_gmt8 - timedelta(hours=8)) - timedelta(hours=12)
                iso_time = dt_utc.replace(microsecond=0).isoformat() + 'Z'
                print(f"  [💡 动态时间窗] 已定位本地最新时间，将从 {iso_time} 之后精准获取新数据。")
                return iso_time
        except Exception:
            pass
    return (now_utc - timedelta(days=fallback_days)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


# ================= 📺 Bilibili 抓取模块 =================
def _load_bilibili_cookies() -> list:
    """从 bilibili_cookies.json 加载 Playwright 格式的 Cookie 列表"""
    import json
    cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bilibili_cookies.json")
    if os.path.exists(cookie_path):
        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                raw_cookies = json.load(f)
            # v3.4.3: 清洗 Cookie 字段，适配 Playwright 严格校验
            valid_samesite = {"Strict", "Lax", "None"}
            cleaned = []
            for c in raw_cookies:
                c = dict(c)
                # sameSite 必须是 Strict/Lax/None，null 转为 Lax
                ss = c.get("sameSite")
                if ss is None or (isinstance(ss, str) and ss not in valid_samesite):
                    c["sameSite"] = "Lax"
                cleaned.append(c)
            return cleaned
        except Exception as e:
            print(f"  [WARN] 加载 bilibili_cookies.json 失败: {e}")
    else:
        print(f"  [WARN] 未找到 bilibili_cookies.json，将使用无 Cookie 模式（可能触发风控）")
    return []

def scrape_bilibili(keyword: str) -> pd.DataFrame:
    print(f"\n[1/2] 正在启动 Bilibili 自动化引擎 (关键词: {keyword})...")
    all_extracted_data = []
    cookies = _load_bilibili_cookies()

    # v3.4.3: 为 Playwright 浏览器配置代理（复用自动检测的代理端口）
    launch_args = {"headless": BILI_HEADLESS}
    if _PROXY_URL:
        import re
        m = re.match(r'http://[^:]+:(\d+)', _PROXY_URL)
        if m:
            port = int(m.group(1))
            launch_args["proxy"] = {"server": f"http://127.0.0.1:{port}"}
            print(f"  [INFO] Playwright 使用代理: 127.0.0.1:{port}")

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_args) 
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", viewport={'width': 1280, 'height': 800})
        if cookies:
            context.add_cookies(cookies)
            print(f"  [OK] 已加载 {len(cookies)} 条 Bilibili Cookie")
        page = context.new_page()
        # v3.4.3: 增加页面加载超时到 60 秒，避免慢网络导致超时
        page.goto(f"https://search.bilibili.com/all?keyword={keyword}&order=pubdate", timeout=60000)
        
        for current_page in range(1, MAX_BILI_PAGES + 1):
            print(f"  [>] 正在抓取第 {current_page} 页...")
            wait_render_time = 0
            while wait_render_time < 30: 
                if page.evaluate("() => !!(document.querySelector('.geetest_panel') || document.querySelector('.bili-mini-mask') || document.querySelector('.yym-captcha'))"):
                    print("  [🚨风控警告] 请手动过验证码。脚本等待中...")
                    time.sleep(3)
                    continue
                if page.locator('.bili-video-card').count() > 0: break
                time.sleep(1); wait_render_time += 1

            if page.locator('.bili-video-card').count() == 0: break
            first_bvid = page.evaluate("() => { let a = document.querySelector('.bili-video-card a'); return a ? (a.href.match(/(BV\\w+)/) || ['', ''])[1] : ''; }")

            js_extractor = """() => {
                function parseNum(s) { let n=parseFloat(s); if(s.includes('万'))n*=10000; return isNaN(n)?0:n; }
                function parseDate(s) {
                    // v5.4.5 修复: B站搜索页日期文本实测为中文格式 ('5小时前' / '昨天' / '3天前' / '9月16日'),
                    // 旧版只认 分钟前/小时前/昨天/前天/MM-DD, 其余全部落到"返回运行当天"的兜底分支,
                    // 导致整批视频的发布日期被写成抓取当天 (2026-09-20 实测 23/23 全错)。
                    // 现补全实际出现的格式; 且解析失败返回空串 —— 宁可为空, 也不伪造当天日期。
                    const now = new Date();
                    const pad = n => ('0' + n).slice(-2);
                    const fmt = d => `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
                    const daysAgo = n => fmt(new Date(now.getTime() - n * 86400000));
                    s = (s || '').replace(/[·\\s]/g, '').trim();
                    if (!s) return '';
                    let m;
                    // 绝对日期: YYYY-M-D / YYYY/M/D (可带时间), 及 YYYY年M月D日
                    if ((m = s.match(/^(\\d{4})[-/年](\\d{1,2})[-/月](\\d{1,2})/)))
                        return `${m[1]}/${pad(+m[2])}/${pad(+m[3])}`;
                    // 无年份: M月D日 / M-D —— 若该日期晚于今天, 说明是去年的
                    if ((m = s.match(/^(\\d{1,2})[月-](\\d{1,2})日?$/))) {
                        const mo = +m[1], da = +m[2], y = now.getFullYear();
                        const cand = new Date(y, mo - 1, da);
                        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
                        return `${cand > today ? y - 1 : y}/${pad(mo)}/${pad(da)}`;
                    }
                    // 相对时间
                    if (/^\\d+秒前$/.test(s)) return fmt(now);
                    if ((m = s.match(/^(\\d+)分钟前$/))) return fmt(new Date(now.getTime() - (+m[1]) * 60000));
                    // "X小时前"是向下取整的, 真实偏移 ∈ [X, X+1) 小时, 跨午夜时无法唯一确定日期;
                    // 取区间中点(减 X+0.5 小时)可最小化跨日误差
                    if ((m = s.match(/^(\\d+)小时前$/))) return fmt(new Date(now.getTime() - ((+m[1]) + 0.5) * 3600000));
                    if (s === '刚刚' || s === '今天') return fmt(now);
                    if (s === '昨天') return daysAgo(1);
                    if (s === '前天') return daysAgo(2);
                    if ((m = s.match(/^(\\d+)天前$/))) return daysAgo(+m[1]);
                    if ((m = s.match(/^(\\d+)周前$/))) return daysAgo(7 * (+m[1]));
                    if ((m = s.match(/^(\\d+)个月前$/))) { const d = new Date(now); d.setMonth(d.getMonth() - (+m[1])); return fmt(d); }
                    return '';  // 未知格式 -> 留空 (写入侧会跳过该字段)
                }
                let data = [];
                document.querySelectorAll('.bili-video-card').forEach((c) => {
                    try {
                        let a = c.querySelector('a'), bv = (a ? a.href.match(/(BV\\w+)/) : [])?.[1]; if(!bv) return;
                        let t = c.querySelector('h3'), up = c.querySelector('.bili-video-card__info--author');
                        let st = c.querySelectorAll('.bili-video-card__stats--item'), dt = c.querySelector('.bili-video-card__info--date');
                        let v = parseNum(st[0]?.innerText.trim()), dRaw = dt ? dt.innerText.trim() : (c.querySelector('.bili-video-card__info--bottom')?.innerText.match(/(\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}|\\d{1,2}月\\d{1,2}日|\\d{1,2}-\\d{1,2}|昨天|前天|今天|\\d+天前|\\d+周前|\\d+个月前|\\d+小时前|\\d+分钟前|\\d+秒前)/)?.[0]||"");
                        data.push({
                            "标题": t?(t.getAttribute('title')||t.innerText.trim()):"无标题", "BV号": bv,
                            "链接": a?a.href:"", "完整链接": a?a.href:"",
                            "时长(MM:SS)": c.querySelector('.bili-video-card__stats__duration')?.innerText.trim()||"",
                            "UP主": up?up.innerText.trim():"", "播放量(原数据)": st[0]?.innerText.trim()||"0",
                            "播放量(k)": (v/1000).toFixed(2), "播放量(万)": (v/10000).toFixed(2), "弹幕数": parseNum(st[1]?.innerText.trim()), "发布日期": parseDate(dRaw)
                        });
                    } catch(e) {}
                });
                return data;
            }"""
            res = page.evaluate(js_extractor)
            if res: all_extracted_data.extend(res)
            
            if current_page < MAX_BILI_PAGES:
                btn = page.locator('.vui_button:has-text("下一页")').first
                if btn.is_visible() and not btn.is_disabled():
                    btn.click()
                    wait = 0
                    while wait < 15:
                        time.sleep(1)
                        if page.evaluate("() => { let a = document.querySelector('.bili-video-card a'); return a ? (a.href.match(/(BV\\w+)/) || ['', ''])[1] : ''; }") != first_bvid: break
                        wait += 1
                else: break
        browser.close()

    if not all_extracted_data: return pd.DataFrame()
# ... (前面的 B站 抓取代码保持不变) ...
    
    df = pd.DataFrame(all_extracted_data).drop_duplicates(subset=['BV号'])
    cols = ['标题', 'BV号', '链接', '完整链接', '时长(MM:SS)', 'UP主', '播放量(原数据)', '播放量(k)', '播放量(万)', '弹幕数', '发布日期']
    for c in cols:
        if c not in df.columns: df[c] = ""
    # 将抓取到的完整链接同步写入"链接"字段（供飞书文本字段使用）
    # v5.2.3: 兜底条件增强 - 同时处理"链接"列缺失、全 NaN、全空字符串 三种情况
    link_col = df["链接"] if "链接" in df.columns else None
    if link_col is None or link_col.isna().all() or (link_col.astype(str) == "").all():
        df["链接"] = df["完整链接"]
        
    # 👇 ============ 新增：强制格式化，消除文本警告 ============
    # 强制将播放量等数值列转换为纯数字类型 (Float/Int)
    for num_col in ['播放量(原数据)', '播放量(k)', '播放量(万)', '弹幕数']:
        df[num_col] = pd.to_numeric(df[num_col], errors='coerce')
        
    # 强制将发布日期转换为标准的时间对象后再转为规范字符串，洗掉任何隐藏的文本杂质
    df['发布日期'] = pd.to_datetime(df['发布日期'], errors='coerce').dt.strftime('%Y/%m/%d')
    # 👆 =======================================================
    
    return df[cols]

# ================= 🔴 YouTube 抓取模块 =================
def parse_duration(duration_str: Optional[str]) -> Tuple[str, float]:
    if not duration_str: return "00:00:00", 0.0
    match = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?').match(duration_str)
    if not match: return "00:00:00", 0.0
    h, m, s = [int(x) if x else 0 for x in match.groups()]
    return f"{h:02d}:{m:02d}:{s:02d}", round(h * 60 + m + s / 60, 2)

def _create_proxy_http():
    """创建带代理的 HTTP 对象用于 YouTube API (v3.4.2)"""
    import httplib2
    proxy_info = None
    if _PROXY_URL:
        port = detect_proxy_port()
        if port:
            proxy_info = httplib2.ProxyInfo(
                httplib2.socks.PROXY_TYPE_HTTP,
                '127.0.0.1',
                port
            )
    return httplib2.Http(proxy_info=proxy_info)

def scrape_youtube(api_key: str, keyword: str, published_after_iso: str) -> pd.DataFrame:
    print(f"\n[2/2] 正在启动 YouTube API 引擎 (关键词: {keyword})...")
    if not api_key or api_key.startswith("你的_"): return pd.DataFrame()
    try:
        http = _create_proxy_http()
        youtube = build('youtube', 'v3', developerKey=api_key, http=http, cache_discovery=False)
    except Exception as e:
        print(f"  [WARN] YouTube API init failed: {e}")
        return pd.DataFrame()
    
    videos_data, video_ids, basic_info_map = [], [], {}
    next_page_token = None

    for _ in range(50):
        try:
            req = youtube.search().list(q=keyword, part='snippet', type='video', publishedAfter=published_after_iso, maxResults=50, order='date', pageToken=next_page_token)
            res = req.execute()
            for item in res.get('items', []):
                vid = item['id']['videoId']
                video_ids.append(vid)
                gmt8_time = datetime.strptime(item['snippet']['publishedAt'], "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=8)
                basic_info_map[vid] = {
                    'Title': item['snippet']['title'], 'Published Date': gmt8_time.strftime("%Y/%m/%d"), 'Published At': gmt8_time.strftime("%Y/%m/%d %H:%M:%S"),
                    'Link': f"https://www.youtube.com/watch?v={vid}", 'Channel': item['snippet']['channelTitle'], 'Channel ID': item['snippet']['channelId']
                }
            next_page_token = res.get('nextPageToken')
            if not next_page_token: break
        except HttpError: break

    if not video_ids: return pd.DataFrame()

    for i in range(0, len(video_ids), 50):
        try:
            stats_res = youtube.videos().list(id=','.join(video_ids[i:i+50]), part='snippet,statistics,contentDetails').execute()
            for item in stats_res.get('items', []):
                vid = item['id']
                stats, snippet = item.get('statistics', {}), item.get('snippet', {})
                v, l, c = int(stats.get('viewCount', 0)), int(stats.get('likeCount', 0)), int(stats.get('commentCount', 0))
                dur_str, dur_min = parse_duration(item.get('contentDetails', {}).get('duration'))
                data = basic_info_map[vid].copy()
                data.update({
                    'Views': v, 'Likes': l, 'Engagement Rate': f"{((l+c)/v*100):.2f}%" if v>0 else "0.00%",
                    'Duration': dur_str, 'Duration (Min)': dur_min,
                    # v5.1.0: 归一化语言代码 (de-DE -> de, en-US -> en), 保证飞书 Language 字段单一标识符
                    'Language': normalize_language(snippet.get('defaultAudioLanguage', 'N/A')),
                    'Live Recording': "Yes" if snippet.get('liveBroadcastContent') != 'none' else "No",
                    'Has Captions': "Yes" if item.get('contentDetails', {}).get('caption') == 'true' else "No",
                    'Tags': ", ".join(snippet.get('tags', [])[:15]) if snippet.get('tags') else "",
                    'Video ID': vid, 'Description': snippet.get('description', '')[:500], 'Comments': c
                })
                videos_data.append(data)
        except HttpError: continue

# ... (前面的 YouTube 抓取代码保持不变) ...
    
    cols = ['Title', 'Published Date', 'Published At', 'Link', 'Channel', 'Channel ID', 'Views', 'Likes', 'Engagement Rate', 'Duration', 'Duration (Min)', 'Language', 'Live Recording', 'Has Captions', 'Tags', 'Video ID', 'Description', 'Comments']
    df = pd.DataFrame(videos_data)
    for c in cols:
        if c not in df.columns: df[c] = None
        
    # 👇 ============ 新增：强制格式化，消除文本警告 ============
    # 强制数值化
    for num_col in ['Views', 'Likes', 'Comments', 'Duration (Min)']:
        df[num_col] = pd.to_numeric(df[num_col], errors='coerce')
        
    # 强制日期化
    df['Published Date'] = pd.to_datetime(df['Published Date'], errors='coerce').dt.strftime('%Y/%m/%d')
    # 注：Engagement Rate 带有 % 号，保持文本即可
    # 👆 =======================================================
    
    return df[cols]

# ================= ⚡ 总控中心 =================
def main():
    print("=" * 50)
    print(f"🚀 {BILI_KEYWORD} 全平台自动抓取 & 飞书多维表格分发系统")
    print("=" * 50)

    print("\n[0] 准备抓取数据并写入飞书多维表格...")

    # 自动备份（在数据变更前）
    try:
        from tools.rollback_manager import auto_backup
        auto_backup()
    except ImportError:
        pass  # 回滚工具可选，失败不影响主流程

    # Bilibili 处理流
    bili_df = scrape_bilibili(BILI_KEYWORD)
    if bili_df.empty:
        print("  [WARN] Bilibili 抓取器未获取到任何视频数据（可能页面无内容或风控拦截）")
    else:
        new_bili = save_and_get_new_records(bili_df, BILIBILI_EXCEL_PATH, unique_key="BV号", sort_key="发布日期")
        if new_bili.empty:
            print("  [INFO] Bilibili 抓取到数据但与本地 Excel 完全重复，无新增记录推送到飞书")
        else:
            push_to_bitable(BILI_BITABLE_TOKEN, BILI_TABLE_ID, new_bili)

    # YouTube 处理流 (动态锁定时间窗)
    yt_published_after = get_yt_published_after(YOUTUBE_EXCEL_PATH, FETCH_DAYS)
    yt_df = scrape_youtube(YT_API_KEY, YT_KEYWORD, yt_published_after)
    if not yt_df.empty:
        new_yt = save_and_get_new_records(yt_df, YOUTUBE_EXCEL_PATH, unique_key="Video ID", sort_key="Published At")
        push_to_bitable(BILI_BITABLE_TOKEN, YT_TABLE_ID, new_yt)

    print("\n🎉 所有任务执行完毕！")

if __name__ == "__main__":
    main()