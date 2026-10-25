#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safe_push_to_feishu.py - 安全推送工具（完全控制类型转换）
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


def safe_push_bilibili():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(base_dir))
    excel_path = os.path.join(project_root, "Bilibili_local_db.xlsx")
    
    if not os.path.exists(excel_path):
        print(f"[ERR] Excel不存在: {excel_path}")
        return
    
    df = pd.read_excel(excel_path)
    print(f"[*] 读取到 {len(df)} 条记录")
    
    token = get_tenant_token()
    if not token:
        return
    
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    bitable_token = CONFIG.get('feishu_bitable_token', '')
    table_id = CONFIG.get('bilibili_table_id', '')
    
    # 字段类型映射（硬编码，确保正确）
    FIELD_TYPES = {
        "序号": "skip",           # AUTO_NUMBER - 跳过
        "标题": "text",
        "BV号": "text",
        "完整链接": "skip",       # FORMULA - 跳过
        "时长(MM:SS)": "text",
        "UP主": "text",
        "播放量(原数据)": "text",
        "播放量(k)": "number",
        "播放量(万)": "number",
        "弹幕数": "text",
        "发布日期": "datetime",
        # 以下字段初筛阶段为空，跳过
        "初筛状态": "skip",
        "字幕获取状态": "skip",
        "AI审核结果": "skip",
        "人工初筛": "skip",
        "评分等级": "skip",
        "D1时长分": "skip",
        "D2时效分": "skip",
        "D3互动分": "skip",
        "D4吸引力分": "skip",
        "综合评分": "skip",
        "字幕文本": "skip",
        "备注": "skip",
        "去重": "skip",            # FORMULA
        "人工确认": "skip",
        "写入时间": "skip",         # CREATED_AT
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
                    pass  # 不写入日期字段
                else:
                    fields_dict[col] = ""
                continue
            
            # 根据字段类型转换
            if field_type == "text":
                fields_dict[col] = str(val).strip()
                
            elif field_type == "number":
                try:
                    num = float(val)
                    if num != num:  # NaN check
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
                    pass  # 日期字段写入失败则不写
        
        if fields_dict:  # 只添加非空记录
            records.append({"fields": fields_dict})
        
        if idx < 2:  # 打印前3条用于调试
            print(f"\n[DEBUG] 记录 {idx+1}:")
            for k, v in fields_dict.items():
                print(f"  {k}: {v} ({type(v).__name__})")
    
    if not records:
        print("[WARN] 没有可写入的记录")
        return
    
    print(f"\n[*] 准备写入 {len(records)} 条记录...")
    
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{bitable_token}/tables/{table_id}/records/batch_create"
    
    try:
        res = requests.post(url, headers=headers, json={"records": records}, timeout=30)
        data = res.json()
        
        if data.get("code") == 0:
            print(f"  [OK] 成功写入 {len(records)} 条记录！")
        else:
            print(f"  [ERR] 写入失败: code={data.get('code')}, msg={data.get('msg')}")
            if data.get("msg") == "NumberFieldConvFail":
                print("\n[诊断] 数字字段转换失败，检查以下字段的值:")
                for col in ["播放量(k)", "播放量(万)"]:
                    if col in df.columns:
                        sample = df[col].head(5).tolist()
                        print(f"  {col}: {sample}")
    except Exception as e:
        print(f"  [ERR] 请求异常: {e}")


if __name__ == "__main__":
    safe_push_bilibili()
