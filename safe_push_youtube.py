#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safe_push_youtube.py - 安全推送 YouTube 数据到飞书
"""

import os
import sys
import json
import requests
import pandas as pd
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.config import CONFIG


def get_tenant_token():
    app_id = CONFIG.get('feishu_app_id', '')
    app_secret = CONFIG.get('feishu_app_secret', '')
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    res = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
    data = res.json()
    if data.get("code") == 0:
        return data["tenant_access_token"]
    print(f"  [ERR] 获取token失败: {data}")
    return None


def safe_push_youtube():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # YouTube Excel 在 content_censor 目录下
    excel_path = os.path.join(base_dir, "..", "content_censor", "YouTube_local_db.xlsx")
    
    if not os.path.exists(excel_path):
        print(f"[ERR] YouTube Excel不存在: {excel_path}")
        # 尝试其他位置
        alt_path = os.path.join(base_dir, "YouTube_local_db.xlsx")
        if os.path.exists(alt_path):
            excel_path = alt_path
            print(f"[*] 找到替代路径: {alt_path}")
        else:
            return
    
    df = pd.read_excel(excel_path)
    print(f"[*] 读取到 {len(df)} 条 YouTube 记录")
    
    token = get_tenant_token()
    if not token:
        return
    
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    bitable_token = CONFIG.get('feishu_bitable_token', '')
    table_id = CONFIG.get('youtube_table_id', '')
    
    # YouTube 字段类型映射（实际列名 + 实际飞书类型）
    FIELD_TYPES = {
        "序号": "skip",           # AUTO_NUMBER
        "Number": "skip",         # AUTO_NUMBER
        
        # 文本字段 - 直接写入字符串
        "Title": "text",
        "Link": "text",
        "Duration": "text",
        "Video ID": "text",
        "Engagement Rate": "text",
        "Live Recording": "text",
        "Has Captions": "text",
        "Tags": "text",
        "Description": "text",
        "Channel ID": "text",
        
        # 数字字段但在飞书中定义为 text - 转为字符串
        "Views": "text_as_number",     # 飞书是 text 类型
        "Likes": "text_as_number",     # 飞书是 text 类型
        "Comments": "text_as_number",  # 飞书是 text 类型
        "Duration (Min)": "text",      # 飞书是 text 类型
        "Published At": "text",        # 飞书是 text 类型（不是 datetime!）
        
        # 日期字段
        "Published Date": "datetime",
        
        # Select 字段 - 需要特殊处理或跳过
        "Channel": "skip",             # select 类型，选项未知
        "Language": "skip",            # select 类型，选项未知
        "Rating": "skip",              # select 类型
        
        # 初筛相关字段 - 全部跳过
        "初筛状态": "skip",
        "Prescreen Status": "skip",
        "字幕获取状态": "skip",
        "Subtitle Status": "skip",
        "AI审核结果": "skip",
        "AI Review Result": "skip",
        "人工初筛": "skip",
        "Human Prescreen": "skip",
        "人工确认": "skip",
        "Manual Confirm": "skip",
        "评分等级": "skip",
        "D1时长分": "skip",
        "D1 Duration": "skip",
        "D2时效分": "skip",
        "D2 Timeliness": "skip",
        "D3互动分": "skip",
        "D3 Engagement": "skip",
        "D4吸引力分": "skip",
        "D4 Appeal": "skip",
        "综合评分": "skip",
        "Total Score": "skip",
        "字幕文本": "skip",
        "备注": "skip",
        "Notes": "skip",
        "count": "skip",
        "标题翻译": "skip",
        "去重": "skip",
        "Date Created": "skip",
    }
    
    records = []
    for idx, row in df.iterrows():
        fields_dict = {}
        
        for col in df.columns:
            if col not in FIELD_TYPES:
                continue
                
            field_type = FIELD_TYPES[col]
            
            if field_type == "skip":
                continue
            
            val = row[col]
            
            # 处理空值
            if pd.isna(val) or val is None:
                if field_type == "number":
                    fields_dict[col] = 0
                elif field_type == "datetime":
                    pass  # 不写入
                else:
                    fields_dict[col] = ""
                continue
            
            # 根据字段类型转换
            if field_type == "text":
                text = str(val).strip()
                text = ''.join(c for c in text if ord(c) >= 32 or c in '\n\t')
                if len(text) > 2000:
                    text = text[:2000]
                fields_dict[col] = text
                
            elif field_type == "text_as_number":
                try:
                    num = float(val)
                    fields_dict[col] = str(int(num)) if num == int(num) else str(num)
                except (ValueError, TypeError):
                    fields_dict[col] = "0"
                
            elif field_type == "number":
                try:
                    num = float(val)
                    if num != num:
                        fields_dict[col] = 0
                    else:
                        fields_dict[col] = int(num) if num == int(num) else num
                except (ValueError, TypeError):
                    fields_dict[col] = 0
                    
            elif field_type == "datetime":
                try:
                    if isinstance(val, (pd.Timestamp, datetime)):
                        ts = val.timestamp()
                        if ts > 0:
                            fields_dict[col] = int(ts * 1000)
                    elif isinstance(val, str):
                        dt = pd.to_datetime(val)
                        if not pd.isna(dt):
                            fields_dict[col] = int(dt.timestamp() * 1000)
                    elif isinstance(val, (int, float)) and val > 0:
                        if val > 1e12:
                            fields_dict[col] = int(val)
                        else:
                            fields_dict[col] = int(val * 1000)
                except Exception:
                    pass
        
        if fields_dict:
            records.append({"fields": fields_dict})
        
        if idx < 2:
            print(f"\n[DEBUG] 记录 {idx+1}:")
            for k, v in fields_dict.items():
                print(f"  {k}: {v} ({type(v).__name__})")
    
    if not records:
        print("[WARN] 没有可写入的记录")
        return
    
    print(f"\n[*] 准备写入 {len(records)} 条 YouTube 记录...")
    
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}/tables/{table_id}/records/batch_create"
    
    try:
        res = requests.post(url, headers=headers, json={"records": records}, timeout=30)
        data = res.json()
        
        if data.get("code") == 0:
            print(f"  [OK] 成功写入 {len(records)} 条 YouTube 记录！")
        else:
            print(f"  [ERR] 写入失败: code={data.get('code')}, msg={data.get('msg')}")
    except Exception as e:
        print(f"  [ERR] 请求异常: {e}")


if __name__ == "__main__":
    safe_push_youtube()
