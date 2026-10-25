#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu_sync.py - 飞书多维表格同步模块 (v5.3.0)

v5.3.0 变更: 时间过滤 - 已标记优先级/Selected的视频不受时间过滤限制
v4.1.0 变更: 混合模式
- 读取操作: 使用 lark-cli 子进程调用 (无需认证)
- 写入操作: 使用 requests + tenant_access_token (.env 中的项目 bot)
  原因: lark-cli 的 bot 应用与项目 bot 是不同应用，
        项目 bot 有多维表格完整权限且 QPS 限制更宽松

使用方法：
    from feishu_sync import FeishuSync
    sync = FeishuSync()
    sync.init_fields()
    sync.update_record(table_id, record_id, {"综合评分": 12.5})
"""

import os
import sys
import json
import time
import subprocess
from typing import Dict, List, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import CONFIG
from feishu_fields import (
    BILI_F_PRESCREEN, BILI_F_SUBTITLE_STATUS, BILI_F_SUBTITLE_TEXT,
    BILI_F_TOTAL_SCORE, BILI_F_RATING, BILI_F_AI_REVIEW, BILI_F_HUMAN_CONFIRM,
    BILI_F_NOTES, BILI_F_HUMAN_PRESCREEN, BILI_F_D1, BILI_F_D2,
    BILI_F_BVID, BILI_F_TITLE, BILI_F_FULL_LINK, BILI_F_CREATED_AT,
    BILI_F_AUTO_NUMBER, BILI_F_PUBLISH_DATE, BILI_F_DURATION,
    YT_F_PRESCREEN, YT_F_HUMAN_PRESCREEN, YT_F_SUBTITLE_STATUS,
    YT_F_D1, YT_F_D2, YT_F_D3, YT_F_D4, YT_F_TOTAL_SCORE, YT_F_RATING,
    YT_F_AI_REVIEW, YT_F_VIDEO_ID, YT_F_TITLE, YT_F_LINK, YT_F_NOTES, YT_F_MANUAL_CONFIRM,
    YT_F_CREATED_AT, YT_F_AUTO_NUMBER, YT_F_PUBLISH_DATE, YT_F_DURATION,
    FIELD_TYPE_TEXT, FIELD_TYPE_NUMBER, FIELD_TYPE_SELECT, FIELD_TYPE_CHECKBOX,
    BILI_PRESCREEN_PENDING, BILI_PRESCREEN_PASS, BILI_PRESCREEN_REJECT,
    BILI_SUBTITLE_PENDING, BILI_SUBTITLE_SUCCESS, BILI_SUBTITLE_FAILED,
    BILI_AI_RESULT_PASS, BILI_AI_RESULT_FAIL,
    BILI_HUMAN_HIGH, BILI_HUMAN_MEDIUM, BILI_HUMAN_LOW, BILI_HUMAN_SELECTED, BILI_HUMAN_REJECTED,
    BILI_GRADE_A, BILI_GRADE_B, BILI_GRADE_C, BILI_GRADE_D, BILI_GRADE_E,
    YT_PRESCREEN_PENDING, YT_PRESCREEN_PASS, YT_PRESCREEN_REJECT,
    YT_SUBTITLE_PENDING, YT_SUBTITLE_SUCCESS, YT_SUBTITLE_FAILED,
    YT_AI_RESULT_SAFE, YT_AI_RESULT_UNSAFE, YT_AI_RESULT_REJECTED,
    YT_HUMAN_HIGH, YT_HUMAN_MEDIUM, YT_HUMAN_LOW, YT_HUMAN_SELECTED, YT_HUMAN_REJECTED,
    YT_GRADE_A, YT_GRADE_B, YT_GRADE_C, YT_GRADE_D, YT_GRADE_E,
    LANG_FIELD, LANG_OPTIONS,
    BILI_REQUIRED_FIELDS as BILI_REQUIRED_FIELDS_CONST,
    YT_REQUIRED_FIELDS as YT_REQUIRED_FIELDS_CONST,
)

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

FEISHU_APP_ID = CONFIG.get('feishu_app_id', '')
FEISHU_APP_SECRET = CONFIG.get('feishu_app_secret', '')

BILI_BITABLE_TOKEN = CONFIG.get('feishu_bitable_token', '')
BILI_TABLE_ID = CONFIG.get('bilibili_table_id', '')
YT_TABLE_ID = CONFIG.get('youtube_table_id', '')

FIELD_TYPE_LARK_MAP = {
    1: "text",
    2: "number",
    3: "select",
    4: "checkbox",
    5: "datetime",
}

# v4.8.1: 拆分为 B站(chinese) 和 YouTube(english) 两套字段列表
# 之前的 NEEDED_FIELDS 全是中文名, 导致 YouTube 表也被创建了中文字段
BILI_NEEDED_FIELDS = {
    BILI_F_PRESCREEN: {"type": FIELD_TYPE_SELECT, "options": [BILI_PRESCREEN_PENDING, BILI_PRESCREEN_PASS, BILI_PRESCREEN_REJECT]},
    BILI_F_SUBTITLE_STATUS: {"type": FIELD_TYPE_SELECT, "options": [BILI_SUBTITLE_PENDING, BILI_SUBTITLE_SUCCESS, BILI_SUBTITLE_FAILED]},
    BILI_F_SUBTITLE_TEXT: {"type": FIELD_TYPE_TEXT},
    BILI_F_TOTAL_SCORE: {"type": FIELD_TYPE_NUMBER},
    BILI_F_RATING: {"type": FIELD_TYPE_SELECT, "options": [BILI_GRADE_A, BILI_GRADE_B, BILI_GRADE_C, BILI_GRADE_D, BILI_GRADE_E]},
    BILI_F_AI_REVIEW: {"type": FIELD_TYPE_SELECT, "options": [BILI_PRESCREEN_PENDING, BILI_AI_RESULT_PASS, BILI_AI_RESULT_FAIL]},
    BILI_F_HUMAN_CONFIRM: {"type": FIELD_TYPE_CHECKBOX},
    BILI_F_NOTES: {"type": FIELD_TYPE_TEXT},
    # v5.3.5: 语言分类字段 (select, 10 选项) — init_fields 检查缺失时自动创建
    LANG_FIELD: {"type": FIELD_TYPE_SELECT, "options": LANG_OPTIONS},
}

YT_SUBTITLE_TEXT_EN = "Subtitle Text"

YT_NEEDED_FIELDS = {
    YT_F_PRESCREEN: {"type": FIELD_TYPE_SELECT, "options": [YT_PRESCREEN_PENDING, YT_PRESCREEN_PASS, YT_PRESCREEN_REJECT]},
    YT_F_SUBTITLE_STATUS: {"type": FIELD_TYPE_SELECT, "options": [YT_SUBTITLE_PENDING, YT_SUBTITLE_SUCCESS, YT_SUBTITLE_FAILED]},
    YT_SUBTITLE_TEXT_EN: {"type": FIELD_TYPE_TEXT},
    YT_F_TOTAL_SCORE: {"type": FIELD_TYPE_NUMBER},
    YT_F_RATING: {"type": FIELD_TYPE_SELECT, "options": [YT_GRADE_A, YT_GRADE_B, YT_GRADE_C, YT_GRADE_D, YT_GRADE_E]},
    YT_F_AI_REVIEW: {"type": FIELD_TYPE_SELECT, "options": [YT_AI_RESULT_SAFE, YT_AI_RESULT_UNSAFE, YT_AI_RESULT_REJECTED]},
    YT_F_MANUAL_CONFIRM: {"type": FIELD_TYPE_CHECKBOX},
    YT_F_NOTES: {"type": FIELD_TYPE_TEXT},
    # v5.3.5: 语言分类字段 (select, 10 选项) — init_fields 检查缺失时自动创建
    LANG_FIELD: {"type": FIELD_TYPE_SELECT, "options": LANG_OPTIONS},
}

# 向后兼容 (deprecated)
NEEDED_FIELDS = BILI_NEEDED_FIELDS

BILI_REQUIRED_FIELDS = BILI_REQUIRED_FIELDS_CONST

YT_REQUIRED_FIELDS = YT_REQUIRED_FIELDS_CONST

CREATOR_APP_TOKEN = CONFIG.get('feishu_bitable_token', '')
CREATOR_TABLE_ID = CONFIG.get('creator_table_id', '')

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"

_LARK_CLI_PATH: Optional[str] = None


def _find_lark_cli() -> str:
    """在 PATH 中查找 lark-cli 的可执行路径（优先找 .cmd .exe .bat）"""
    for dir_path in os.environ.get('PATH', '').split(os.pathsep):
        for ext in ['.cmd', '.exe', '.bat']:
            candidate = os.path.join(dir_path, f'lark-cli{ext}')
            if os.path.isfile(candidate):
                return candidate
    return 'lark-cli'


def _run_lark_cli(args: List[str], stdin_data: str = None) -> Dict[str, Any]:
    """
    运行 lark-cli 命令并返回解析后的 JSON 结果（仅用于读取操作）

    Args:
        args: lark-cli 命令行参数列表（不含 'lark-cli' 本身）
        stdin_data: 可选的标准输入数据

    Returns:
        解析后的 JSON 字典
    """
    global _LARK_CLI_PATH
    if _LARK_CLI_PATH is None:
        _LARK_CLI_PATH = _find_lark_cli()

    is_script = _LARK_CLI_PATH.endswith(('.cmd', '.bat'))
    if is_script:
        cmd = ['cmd.exe', '/c', _LARK_CLI_PATH] + args
    else:
        cmd = [_LARK_CLI_PATH] + args
    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=60
        )
    except subprocess.TimeoutExpired:
        print(f"[ERROR] lark-cli 命令超时: {' '.join(args[:4])}...")
        return {}
    except FileNotFoundError:
        print("[ERROR] lark-cli 未安装或不在 PATH 中")
        return {}
    except Exception as e:
        print(f"[ERROR] lark-cli 执行失败: {e}")
        return {}

    if result.returncode != 0:
        if result.stderr:
            err_text = result.stderr.strip()
            try:
                err_json = json.loads(err_text)
                print(f"  [WARN] lark-cli 错误: code={err_json.get('error',{}).get('code')}, msg={err_json.get('error',{}).get('message','')[:100]}")
            except (json.JSONDecodeError, TypeError):
                pass
        return {}

    try:
        raw = result.stdout.strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if not data.get("ok", False):
            err = data.get("error", {})
            print(f"[ERROR] lark-cli API 错误: {err.get('message', str(err))}")
            return {}
        return data.get("data", {})
    except json.JSONDecodeError as e:
        print(f"[ERROR] lark-cli 输出非 JSON: {e}")
        return {}


class FeishuSync:
    """飞书多维表格同步类 (v4.1.0 - 混合模式: 读=lark-cli, 写=requests+bot)"""

    def __init__(self, app_id: str = None, app_secret: str = None):
        self.app_id = app_id or FEISHU_APP_ID
        self.app_secret = app_secret or FEISHU_APP_SECRET
        self.bitable_token = ""
        self._tenant_token = ""
        self._token_expire_time = 0
        self._record_id_cache: Dict[str, Dict] = {}

    def _authenticate_tenant(self) -> bool:
        """获取/刷新 tenant_access_token（项目 bot 身份）"""
        if not HAS_REQUESTS:
            print("[ERROR] requests 库未安装")
            return False

        now = time.time()
        if self._tenant_token and now < self._token_expire_time:
            return True

        url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
        try:
            res = requests.post(url, json={
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            }, timeout=10)
            data = res.json()

            if data.get("code") == 0:
                self._tenant_token = data["tenant_access_token"]
                self._token_expire_time = now + data.get("expire", 7200) - 300
                return True
            else:
                print(f"[ERROR] 获取 tenant_token 失败: code={data.get('code')}, msg={data.get('msg')}")
                return False
        except Exception as e:
            print(f"[ERROR] 认证请求失败: {e}")
            return False

    def _get_headers(self) -> Dict[str, str]:
        """构建带认证的请求头"""
        if not self._tenant_token:
            self._authenticate_tenant()
        return {"Authorization": f"Bearer {self._tenant_token}", "Content-Type": "application/json"}

    def set_bitable_token(self, bitable_token: str) -> None:
        self.bitable_token = bitable_token

    def _auth_headers(self) -> Dict[str, str]:
        """统一的 requests 风格请求头 (写入/读取都走 bot, 避免 lark-cli 限制)"""
        if not self._tenant_token:
            self._authenticate_tenant()
        return {"Authorization": f"Bearer {self._tenant_token}", "Content-Type": "application/json"}

    def get_fields(self, table_id: str) -> Dict[str, int]:
        """读取字段列表 (requests + bot token, 单次拉完无 lark-cli 限制)"""
        if not self.bitable_token:
            print("[ERROR] bitable_token 未设置")
            return {}

        if not self._authenticate_tenant():
            return {}

        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{self.bitable_token}/tables/{table_id}/fields"
        try:
            res = requests.get(url, headers=self._auth_headers(), timeout=15)
            data = res.json()
            if data.get("code") == 0:
                items = data.get("data", {}).get("items", [])
                # 返回 name -> type (字符串) 让调用方按需映射到整数
                return {item["field_name"]: item["type"] for item in items if item.get("field_name")}
            print(f"  [WARN] get_fields 失败: code={data.get('code')}, msg={data.get('msg')}")
        except Exception as e:
            print(f"  [ERROR] get_fields 异常: {e}")
        return {}

    def validate_required_fields(self, platform: str = "both") -> bool:
        if not self.bitable_token:
            print("[ERROR] bitable_token 未设置")
            return False

        all_ok = True

        if platform in ("bilibili", "both"):
            actual_fields = self.get_fields(BILI_TABLE_ID)
            missing = [f for f in BILI_REQUIRED_FIELDS if f not in actual_fields]
            if missing:
                print(f"\n[WARN] B站表格缺失字段: {missing}")
                all_ok = False
            else:
                print(f"[OK] B站表格字段验证通过 ({len(BILI_REQUIRED_FIELDS)} 个)")

        if platform in ("youtube", "both"):
            actual_fields = self.get_fields(YT_TABLE_ID)
            missing = [f for f in YT_REQUIRED_FIELDS if f not in actual_fields]
            if missing:
                print(f"\n[WARN] YouTube表格缺失字段: {missing}")
                all_ok = False
            else:
                print(f"[OK] YouTube表格字段验证通过 ({len(YT_REQUIRED_FIELDS)} 个)")

        if not all_ok:
            print("\n[!!!] 字段验证失败！请检查飞书表格字段名是否被修改。")

        return all_ok

    def get_records(self, table_id: str, filter_str: str = "") -> List[Dict]:
        """读取记录列表 (requests + bot token, 单次 1000/页 无 lark-cli 限制)"""
        if not self.bitable_token:
            print("[ERROR] bitable_token 未设置")
            return []
        if not self._authenticate_tenant():
            return []

        all_records = []
        page_size = 1000
        page_token = None
        max_retries = 3

        while True:
            url = f"{FEISHU_API_BASE}/bitable/v1/apps/{self.bitable_token}/tables/{table_id}/records"
            params = {"page_size": page_size, "with_field_ids": "false"}
            if page_token:
                params["page_token"] = page_token

            for attempt in range(1, max_retries + 1):
                try:
                    res = requests.get(url, headers=self._auth_headers(), params=params, timeout=60)
                    data = res.json()
                    if data.get("code") != 0:
                        # Token 过期，重新认证后重试
                        if data.get("code") in (99991663, 99991664, 99991668, 99991672):
                            print(f"  [WARN] get_records token 过期 (code={data.get('code')})，重新认证...")
                            self._tenant_token = None
                            self._token_expire_time = 0
                            continue
                        print(f"  [WARN] get_records 失败: code={data.get('code')}, msg={data.get('msg')}")
                        break

                    payload = data.get("data", {})
                    items = payload.get("items", [])
                    for item in items:
                        all_records.append({
                            "record_id": item.get("record_id", ""),
                            "fields": item.get("fields", {}),
                        })

                    if not payload.get("has_more", False):
                        page_token = None
                    else:
                        page_token = payload.get("page_token")
                    break  # 成功，退出重试循环进入下一页
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                    wait_sec = attempt * 2
                    print(f"  [WARN] get_records 第{attempt}/{max_retries}次 超时/连接错误: {type(e).__name__}，{wait_sec}s后重试...")
                    if attempt < max_retries:
                        time.sleep(wait_sec)
                        continue
                    print(f"  [ERROR] get_records 重试 {max_retries} 次后仍失败")
                    page_token = None
                    break
                except Exception as e:
                    print(f"  [ERROR] get_records 异常: {e}")
                    page_token = None
                    break

            if page_token is None:
                break

        return all_records

    def get_record_by_field(self, table_id: str, field_name: str, field_value: str) -> Optional[Dict]:
        """按字段值搜索单条记录（requests + bot token，避免 lark-cli 限流）"""
        if not self.bitable_token or not field_value:
            return None
        if not self._authenticate_tenant():
            return None

        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{self.bitable_token}/tables/{table_id}/records/search"
        body = {
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {
                            "field_name": field_name,
                            "operator": "is",
                            "value": [str(field_value)],
                        }
                    ]
                },
                "page_size": 20,
            }
        RATE_LIMIT_CODES = {800004135, 800004134, 99991400}

        for attempt in range(3):
            try:
                res = requests.post(url, headers=self._get_headers(), json=body, timeout=15)
                data = res.json()
                if data.get("code") == 0:
                    items = data.get("data", {}).get("items", [])
                    if items:
                        item = items[0]
                        return {
                            "record_id": item.get("record_id", ""),
                            "fields": item.get("fields", {}),
                        }
                    return None

                err_code = data.get("code", 0)
                if err_code in RATE_LIMIT_CODES and attempt < 2:
                    wait = min(2 ** attempt, 4)
                    print(f"    [RETRY] 飞书搜索限流 (code={err_code})，等待 {wait}s ({attempt+1}/3)...")
                    time.sleep(wait)
                    continue
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(1)
                    continue
                print(f"  [ERROR] 搜索记录异常: {e}")
                return None

        return None

    def create_field(self, table_id: str, field_name: str, field_type: int, extra: Dict = None) -> bool:
        """创建字段（requests + tenant token）"""
        if not self._authenticate_tenant():
            return False

        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{self.bitable_token}/tables/{table_id}/fields"

        body = {"field_name": field_name, "type": field_type}
        if field_type == 3 and extra and "options" in extra:
            body["property"] = {"options": [{"name": o} for o in extra["options"]]}

        try:
            res = requests.post(url, headers=self._get_headers(), json=body, timeout=10)
            data = res.json()
            if data.get("code") == 0:
                print(f"  [OK] 创建字段: {field_name}")
                return True
            print(f"  [ERROR] 创建字段失败: {field_name}, code={data.get('code')}, msg={data.get('msg')}")
        except Exception as e:
            print(f"  [ERROR] 创建字段异常: {field_name}, {e}")
        return False

    def update_field(self, table_id: str, field_id: str, field_name: str, field_type: int, extra: Dict = None) -> bool:
        """更新字段配置（requests + tenant token）"""
        if not self._authenticate_tenant():
            return False

        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{self.bitable_token}/tables/{table_id}/fields/{field_id}"

        body = {"field_name": field_name, "type": field_type}
        if field_type == 3 and extra and "options" in extra:
            body["property"] = {"options": [{"name": o} for o in extra["options"]]}

        try:
            res = requests.put(url, headers=self._get_headers(), json=body, timeout=10)
            data = res.json()
            if data.get("code") == 0:
                print(f"  [OK] 更新字段: {field_name}")
                return True
            print(f"  [ERROR] 更新字段失败: {field_name}")
        except Exception as e:
            print(f"  [ERROR] 更新字段异常: {field_name}, {e}")
        return False

    def update_record(self, table_id: str, record_id: str, fields: Dict[str, Any], max_retries: int = 3) -> bool:
        """更新单条记录（requests + tenant token + 限流重试）"""
        if not self._authenticate_tenant():
            return False

        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{self.bitable_token}/tables/{table_id}/records/{record_id}"
        body = {"fields": fields}
        RATE_LIMIT_CODES = {800004135, 800004134}

        for attempt in range(max_retries):
            try:
                res = requests.put(url, headers=self._get_headers(), json=body, timeout=15)
                data = res.json()
                if data.get("code") == 0:
                    return True

                err_code = data.get("code", 0)
                if err_code in RATE_LIMIT_CODES and attempt < max_retries - 1:
                    wait_sec = min(2 ** attempt, 8)
                    print(f"    [RETRY] 飞书限流 (code={err_code})，等待 {wait_sec}s 后重试 ({attempt+1}/{max_retries})...")
                    time.sleep(wait_sec)
                    continue
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                print(f"  [ERROR] 更新异常: {e}")

        print(f"  [ERROR] 飞书更新失败 (record_id={record_id[:12]}...)")
        return False

    # v5.4.0: 内部哨兵 - 表示该字段值转换失败/字段不可写, create_record 时跳过
    _FIELD_SKIP = object()

    def _convert_field_value(self, ftype, val: Any) -> Any:
        """按飞书字段类型转换写入值 (create_record 用)

        规则2 (工作区): 写入前必须重新获取字段类型, 再按类型写入。
        类型: 1=text, 2=number, 3=select(option NAME), 4=checkbox, 5=datetime(毫秒时间戳)
        返回 _FIELD_SKIP 表示跳过该字段 (只读字段/转换失败), 避免 ConvFail。
        """
        # 兼容字符串类型名 (lark-cli) 与整数类型 (OpenAPI)
        type_map = {"text": 1, "number": 2, "select": 3, "multi_select": 3,
                    "checkbox": 4, "datetime": 5, "date": 5}
        if isinstance(ftype, str):
            ftype = type_map.get(ftype.lower(), 1)
        # 只读/公式/自动编号字段不写
        if ftype in (7, 8, 15, 1005, 1001, 1002, 1003, 1004, 1005):
            return self._FIELD_SKIP

        try:
            if val is None:
                return self._FIELD_SKIP
            if ftype == 2:  # number
                num = float(val)
                if num != num:  # NaN
                    return self._FIELD_SKIP
                return int(num) if num == int(num) and abs(num) < 1e15 else num
            if ftype == 4:  # checkbox
                if isinstance(val, str):
                    return val.strip().lower() in ("1", "true", "yes", "是")
                return bool(val)
            if ftype == 5:  # datetime -> 毫秒时间戳 (GMT+8 解释字符串)
                import datetime as _dt
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    fv = float(val)
                    if fv <= 0:
                        return self._FIELD_SKIP
                    return int(fv if fv > 1e12 else fv * 1000)
                s = str(val).strip()
                if not s or s.lower() in ("nat", "none", "null"):
                    return self._FIELD_SKIP
                gmt8 = _dt.timezone(_dt.timedelta(hours=8))
                for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        dt = _dt.datetime.strptime(s, fmt).replace(tzinfo=gmt8)
                        return int(dt.timestamp() * 1000)
                    except ValueError:
                        continue
                return self._FIELD_SKIP
            # 1=text / 3=select / 其他一律按文本写 (select 传 option NAME)
            return str(val)
        except (ValueError, TypeError, OverflowError, OSError):
            return self._FIELD_SKIP

    def create_record(self, table_id: str, fields: Dict[str, Any], max_retries: int = 3) -> str:
        """创建单条记录 (requests + tenant token + 限流重试)

        v5.4.0 新增, 供 Discord 自荐视频补录入表使用。
        写入前重新拉取表结构 (规则2), 过滤表中不存在的字段, 并按字段类型转换值。

        返回: 新记录 record_id; 失败返回空串
        """
        if not table_id or not fields:
            return ""
        if not self._authenticate_tenant():
            return ""

        # 规则2: 写入前重新获取字段名与类型
        table_fields = self.get_fields(table_id)
        if not table_fields:
            print("  [ERROR] create_record: 无法获取表字段结构, 中止写入")
            return ""

        payload: Dict[str, Any] = {}
        for name, val in fields.items():
            if name not in table_fields:
                print(f"  [WARN] create_record: 字段表中不存在, 跳过: {name}")
                continue
            converted = self._convert_field_value(table_fields[name], val)
            if converted is self._FIELD_SKIP:
                continue
            payload[name] = converted
        if not payload:
            print("  [ERROR] create_record: 无有效字段可写, 中止")
            return ""

        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{self.bitable_token}/tables/{table_id}/records"
        body = {"fields": payload}
        RATE_LIMIT_CODES = {800004135, 800004134}

        for attempt in range(max_retries):
            try:
                res = requests.post(url, headers=self._get_headers(), json=body, timeout=15)
                data = res.json()
                if data.get("code") == 0:
                    record_id = str((data.get("data") or {}).get("record", {}).get("record_id", "") or "")
                    return record_id
                err_code = data.get("code", 0)
                if err_code in RATE_LIMIT_CODES and attempt < max_retries - 1:
                    wait_sec = min(2 ** attempt, 8)
                    print(f"    [RETRY] 飞书限流 (code={err_code})，等待 {wait_sec}s 后重试 ({attempt+1}/{max_retries})...")
                    time.sleep(wait_sec)
                    continue
                print(f"  [ERROR] 飞书创建记录失败: code={err_code}, msg={data.get('msg', '')[:120]}")
                return ""
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                print(f"  [ERROR] 飞书创建记录异常: {e}")
                return ""
        return ""

    def batch_update_records(self, table_id: str, records: List[Dict[str, Any]]) -> int:
        """批量更新记录（逐条调用 update_record，含限流重试）"""
        if not records:
            return 0

        success_count = 0
        for i, rec in enumerate(records):
            rid = rec.get("record_id", "")
            fields = rec.get("fields", {})
            if rid and fields:
                if self.update_record(table_id, rid, fields):
                    success_count += 1
            if (i + 1) % 20 == 0:
                print(f"    batch_update 进度: {i + 1}/{len(records)}")

        return success_count

    def delete_records(self, table_id: str, record_ids: List[str]) -> int:
        """批量删除记录（requests + tenant token）
        官方 schema: {"records": ["recwNXzPQv"]} 字符串数组 (不是对象数组!)
        单批最多 500 条, 50 req/s
        """
        if not record_ids or not self._authenticate_tenant():
            return 0

        url = f"{FEISHU_API_BASE}/bitable/v1/apps/{self.bitable_token}/tables/{table_id}/records/batch_delete"
        deleted = 0
        # 按 500 条分批 (飞书官方限制)
        for i in range(0, len(record_ids), 500):
            chunk = record_ids[i:i+500]
            body = {"records": chunk}  # 字符串数组, 不是 [{"record_id": rid}]
            try:
                res = requests.post(url, headers=self._get_headers(), json=body, timeout=30)
                data = res.json()
                if data.get("code") == 0:
                    deleted += len(chunk)
                else:
                    print(f"  [ERROR] 批量删除失败 (batch {i//500 + 1}): code={data.get('code')}, msg={data.get('msg')}")
            except Exception as e:
                print(f"  [ERROR] 删除异常 (batch {i//500 + 1}): {e}")
        return deleted

    def _get_field_id(self, table_id: str, field_name: str) -> Optional[str]:
        """通过 requests + bot token 获取字段ID (替代 lark-cli)"""
        fields = self.get_fields(table_id)
        # get_fields 返回 name -> type 字符串, 字段 id 在调用方根据需要用 fields_meta
        return None  # 调用方改用 name 索引, 此函数保留以备兼容

    def init_fields(self, table_id: str = None, platform: str = None) -> Dict[str, bool]:
        if platform == "bilibili":
            table_id = table_id or BILI_TABLE_ID
            self.set_bitable_token(BILI_BITABLE_TOKEN)
            needed = BILI_NEEDED_FIELDS
        elif platform == "youtube":
            table_id = table_id or YT_TABLE_ID
            self.set_bitable_token(BILI_BITABLE_TOKEN)
            needed = YT_NEEDED_FIELDS
        else:
            print("[ERROR] 必须指定 platform (bilibili/youtube)")
            return {}

        print(f"[*] 初始化字段 ({platform}): {table_id}")

        existing_fields = self.get_fields(table_id)
        results = {}

        for field_name, field_config in needed.items():
            if field_name not in existing_fields:
                results[field_name] = self.create_field(
                    table_id, field_name, field_config["type"], field_config
                )
            # ⚠️ 禁止对已存在的字段调用 update_field！(v4.5.1)
            # 飞书 PUT /fields/{id} 会替换 select 字段的全部选项，
            # 导致所有已有记录该字段的值被清空（包括初筛状态、AI审核结果等）。
            # 字段定义只应在创建时设定，已存在的字段不应再动。

        return results

    def update_video_record(self, video_id: str, platform: str, updates: Dict[str, Any]) -> bool:
        table_id = BILI_TABLE_ID if platform.lower() == "bilibili" else YT_TABLE_ID

        cache_key = f"{table_id}:{video_id}"
        record = self._record_id_cache.get(cache_key)

        if not record:
            record = self.get_record_by_field(table_id, "视频ID", video_id)
        if not record:
            record = self.get_record_by_field(table_id, BILI_F_BVID, video_id)
        if not record:
            record = self.get_record_by_field(table_id, YT_F_VIDEO_ID, video_id)

        if not record:
            print(f"  [WARN] 未找到视频记录: {video_id}")
            return False

        self._record_id_cache[cache_key] = record
        return self.update_record(table_id, record.get("record_id"), updates)

    def get_videos_for_human_prescreen(self, platform: str, days_filter: int = None) -> Dict[str, List[Dict]]:
        from config import CONFIG
        if days_filter is None:
            days_filter = CONFIG.get("human_prescreen_days_filter", 7)

        self.set_bitable_token(BILI_BITABLE_TOKEN)

        table_id = BILI_TABLE_ID if platform.lower() == "bilibili" else YT_TABLE_ID
        link_field = BILI_F_FULL_LINK if platform.lower() == "bilibili" else YT_F_LINK
        id_field = BILI_F_BVID if platform.lower() == "bilibili" else YT_F_VIDEO_ID
        title_field = BILI_F_TITLE if platform.lower() == "bilibili" else YT_F_TITLE
        prescreen_field = BILI_F_PRESCREEN if platform.lower() == "bilibili" else YT_F_PRESCREEN
        human_prescreen_field = BILI_F_HUMAN_PRESCREEN if platform.lower() == "bilibili" else YT_F_HUMAN_PRESCREEN
        create_time_field = BILI_F_CREATED_AT if platform.lower() == "bilibili" else YT_F_CREATED_AT

        if platform.lower() == "bilibili":
            priority_keywords = [BILI_HUMAN_HIGH, BILI_HUMAN_MEDIUM, BILI_HUMAN_LOW]
            force_include_keywords = [BILI_HUMAN_SELECTED]
            reject_keywords = [BILI_HUMAN_REJECTED]
            status_passed = BILI_PRESCREEN_PASS
        else:
            priority_keywords = [YT_HUMAN_HIGH, YT_HUMAN_MEDIUM, YT_HUMAN_LOW]
            force_include_keywords = [YT_HUMAN_SELECTED]
            reject_keywords = [YT_HUMAN_REJECTED, BILI_HUMAN_REJECTED]
            status_passed = YT_PRESCREEN_PASS

        records = self.get_records(table_id)

        import datetime

        def _parse_create_time(val):
            if not val:
                return 0
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str):
                for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"]:
                    try:
                        dt = datetime.datetime.strptime(val.strip(), fmt)
                        return int(dt.timestamp() * 1000)
                    except ValueError:
                        continue
            return 0

        if days_filter > 0:
            today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            cutoff_ms = int((today_start - datetime.timedelta(days=days_filter - 1)).timestamp() * 1000)
        else:
            cutoff_ms = 0

        priority_videos = []
        pending_videos = []
        all_passed_videos = []
        forced_include_videos = []
        skipped_by_time = 0

        def _normalize_select(val):
            """飞书 select/multiSelect 字段返回列表，提取第一个元素或转字符串"""
            if isinstance(val, list) and len(val) > 0:
                return str(val[0]).strip()
            return str(val).strip() if val else ""

        for r in records:
            fields = r.get("fields", {})

            prescreen_status = _normalize_select(fields.get(prescreen_field, ""))
            human_prescreen = _normalize_select(fields.get(human_prescreen_field, ""))

            # v5.3.1: 所有视频(含优先级/Selected)统一受时间过滤, 只审当周新增视频
            # 修复 v5.3.0 让 Selected/优先级不受时间过滤, 导致把历史 Selected 全部拉入 AI 审核、
            # 数量虚增(如B站126=5优先级+121历史Selected)的问题
            if days_filter > 0:
                create_time = _parse_create_time(fields.get(create_time_field, 0))
                if create_time and create_time < cutoff_ms:
                    skipped_by_time += 1
                    continue

            if human_prescreen in reject_keywords:
                continue

            if human_prescreen in priority_keywords:
                if prescreen_status == status_passed:
                    priority_videos.append({
                        "record_id": r.get("record_id"),
                        "video_id": fields.get(id_field, ""),
                        "title": fields.get(title_field, ""),
                        "link": fields.get(link_field, ""),
                        "human_prescreen": human_prescreen,
                        "d1_score": fields.get(BILI_F_D1, 0),
                        "d2_score": fields.get(BILI_F_D2, 0),
                        "create_time": fields.get(create_time_field, 0),
                    })
                else:
                    forced_include_videos.append({
                        "record_id": r.get("record_id"),
                        "video_id": fields.get(id_field, ""),
                        "title": fields.get(title_field, ""),
                        "link": fields.get(link_field, ""),
                        "human_prescreen": human_prescreen,
                        "d1_score": fields.get(BILI_F_D1, 0),
                        "d2_score": fields.get(BILI_F_D2, 0),
                        "create_time": fields.get(create_time_field, 0),
                    })
                continue

            if human_prescreen in force_include_keywords:
                forced_include_videos.append({
                    "record_id": r.get("record_id"),
                    "video_id": fields.get(id_field, ""),
                    "title": fields.get(title_field, ""),
                    "link": fields.get(link_field, ""),
                    "human_prescreen": human_prescreen,
                    "d1_score": fields.get(BILI_F_D1, 0),
                    "d2_score": fields.get(BILI_F_D2, 0),
                    "create_time": fields.get(create_time_field, 0),
                })
                continue

            if prescreen_status == status_passed:
                all_passed_videos.append({
                    "record_id": r.get("record_id"),
                    "video_id": fields.get(id_field, ""),
                    "title": fields.get(title_field, ""),
                    "link": fields.get(link_field, ""),
                    "human_prescreen": human_prescreen,
                    "d1_score": fields.get(BILI_F_D1, 0),
                    "d2_score": fields.get(BILI_F_D2, 0),
                    "create_time": fields.get(create_time_field, 0),
                })

                if not human_prescreen:
                    pending_videos.append({
                        "record_id": r.get("record_id"),
                        "video_id": fields.get(id_field, ""),
                        "title": fields.get(title_field, ""),
                        "link": fields.get(link_field, ""),
                        "human_prescreen": "",
                        "d1_score": fields.get(BILI_F_D1, 0),
                        "d2_score": fields.get(BILI_F_D2, 0),
                        "create_time": fields.get(create_time_field, 0),
                    })

        if skipped_by_time > 0:
            print(f"  [DEBUG] 因时间过滤跳过了 {skipped_by_time} 条旧记录")

        return {
            "priority": priority_videos,
            "forced_include": forced_include_videos,
            "pending": pending_videos,
            "all_passed": all_passed_videos,
        }

    def get_creator_pending_videos(self, days: int = 30) -> List[Dict[str, Any]]:
        import re

        self.set_bitable_token(CREATOR_APP_TOKEN)
        records = self.get_records(CREATOR_TABLE_ID)
        cutoff_time = time.time() - days * 24 * 3600

        pending_videos = []
        for r in records:
            fields = r.get("fields", {})
            selected = fields.get("入选", "")
            if not selected or "暂未" not in str(selected):
                continue

            update_time = fields.get("更新时间", 0)
            if isinstance(update_time, str):
                try:
                    update_time = int(update_time)
                except (ValueError, TypeError):
                    update_time = 0
            if update_time > 0 and update_time < cutoff_time:
                continue

            link = fields.get("链接", "") or fields.get("B站分享链接", "") or ""
            bvid = ""

            if isinstance(link, list) and link:
                link = link[0]
            elif isinstance(link, dict):
                link = link.get("text", "") or link.get("link", "")

            if "BV" in str(link):
                match = re.search(r'BV[a-zA-Z0-9]{10}', str(link))
                if match:
                    bvid = match.group(0)

            if not bvid:
                continue

            title = fields.get("标题", "") or ""
            if isinstance(title, list):
                title = title[0] if title else ""
            elif isinstance(title, dict):
                title = title.get("text", "") or str(title)

            pending_videos.append({
                "record_id": r.get("record_id", ""),
                "bvid": bvid,
                "title": str(title)[:100],
                "link": str(link),
                "update_time": update_time,
            })

        return pending_videos

    def is_in_creator_table(self, bvid: str) -> bool:
        import re
        if not bvid:
            return False
        bvid = bvid.strip().upper()
        if not bvid.startswith('BV'):
            return False

        self.set_bitable_token(CREATOR_APP_TOKEN)
        try:
            records = self.get_records(CREATOR_TABLE_ID)
            for r in records:
                fields = r.get("fields", {})
                link = fields.get("链接", "") or fields.get("B站分享链接", "") or ""
                if isinstance(link, list) and link:
                    link = str(link[0])
                elif isinstance(link, dict):
                    link = str(link.get("text", "")) or str(link.get("link", ""))
                else:
                    link = str(link)
                if "BV" in link:
                    match = re.search(r'BV[a-zA-Z0-9]{10}', link, re.IGNORECASE)
                    if match and match.group(0).upper() == bvid:
                        return True
            return False
        except Exception as e:
            print(f"[WARN] 检查创作者表格失败: {e}")
            return False

    def get_creator_video_bvids(self) -> set:
        import re
        self.set_bitable_token(CREATOR_APP_TOKEN)
        bvids = set()
        try:
            records = self.get_records(CREATOR_TABLE_ID)
            for r in records:
                fields = r.get("fields", {})
                link = fields.get("链接", "") or fields.get("B站分享链接", "") or ""
                if isinstance(link, list) and link:
                    link = str(link[0])
                elif isinstance(link, dict):
                    link = str(link.get("text", "")) or str(link.get("link", ""))
                else:
                    link = str(link)
                if "BV" in link:
                    match = re.search(r'BV[a-zA-Z0-9]{10}', link, re.IGNORECASE)
                    if match:
                        bvids.add(match.group(0).upper())
            return bvids
        except Exception as e:
            print(f"[WARN] 获取创作者表格 BV 号失败: {e}")
            return set()


if __name__ == "__main__":
    print("=" * 50)
    print("飞书同步模块测试 (v4.1.0 - 混合模式)")
    print("=" * 50)

    sync = FeishuSync()
    sync.set_bitable_token(BILI_BITABLE_TOKEN)

    print("\n[*] 测试读取 (lark-cli):")
    fields = sync.get_fields(BILI_TABLE_ID)
    print(f"  字段数: {len(fields)}")

    print("\n[*] 测试写入 (requests + bot):")
    records = sync.get_records(BILI_TABLE_ID)
    if records:
        ok = sync.update_record(BILI_TABLE_ID, records[0]["record_id"], {BILI_F_NOTES: "v4.1混合模式测试"})
        print(f"  更新结果: {'PASS' if ok else 'FAIL'}")

    print("\n" + "=" * 50)
