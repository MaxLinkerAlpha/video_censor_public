# 飞书 API 使用指南 (v4.8.0+)

## 重要变更 (2026-06-08)

**v4.6.0 起, 本项目全面弃用 lark-cli, 改用飞书 OpenAPI (直接 HTTP 调用)。**

### 性能实测对比 (v4.8.0)

| 方案 | 耗时 (1000条) | 吞吐 | 倍数 |
|------|------|------|------|
| lark-cli 串行 1000次 (200限制×5) | 7.0s | 142.6 rec/s | 1x |
| lark-cli batch_create 1000 | ❌ 不支持 (lark-cli 1.0.39 无该命令) | — | — |
| OpenAPI batch_create 1000/批 | 2.3s | 430.1 rec/s | 3.0x |
| **OpenAPI 1000/批 × 4 并发 (4000条)** | **3.6s** | **1125.9 rec/s** | **7.89x** 🏆 |

测试脚本: `perf_compare.py` (临时, 已删除)

### 飞书官方限流速查 (来自 TapData 官方文档)

| API | 限流 (req/s) | 单批 (records/req) |
|-----|----|----|
| Batch Insert/Update | **50** | **1000** |
| Batch Delete | 50 | 500 |
| Batch Query (read) | 20 | 1000 |

### lark-cli 的硬编码限制

| 操作 | 限制 |
|------|------|
| `+record-list --limit` | **1-200, default 100** (hardcoded, 无 config 可改) |
| `+record-batch-create` | 无 `--limit` 参数, 单次多少由 CLI 决定 |

**结论**: lark-cli **无法突破 200 单批** 限制, 无论怎么配 config.json. 任何"提升 lark-cli 上限"的尝试都是徒劳.

### 本项目的写入策略

| 维度 | 方案 | 性能 |
|------|------|------|
| 写库 | `core/feishu_sync.py` 走飞书 OpenAPI, 单批 1000 records/req | 40 并发, ~150 records/s |
| 读库 | `core/feishu_sync.py` 走飞书 OpenAPI, 单批 1000 records/页 | 20 req/s, 无分页错位 |
| agent 调试 | 保留 lark-cli, 仅做小数据量查看 | 不计性能 |

虽然项目里仍有 `lark-cli` 子命令的引用, 但**生产代码路径**已全部切到飞书 OpenAPI。

### 为什么弃用 lark-cli?

| 维度 | lark-cli | 飞书 OpenAPI |
|------|----------|------------|
| 读取分页上限 | **200 条/页** | **1000 条/页** (5倍) |
| 字段 type 格式 | **字符串** (`"datetime"`) | **整数** (`5`) |
| 兼容性 | 与 `ft == 5` 整数比较不兼容, 需手动映射 | 天然兼容 |
| 性能 (10000条记录) | 50次HTTP + 子进程开销 | 10次HTTP 轻量 |
| 实际影响 | YouTube表 3678条仅拉到600条, 5-6倍"数据丢失" | 全量数据可一次拉完 |

### 重要案例 (本次修复)

`push_to_bitable` 函数原来用 lark-cli 拉 fields:

```python
fields_data = _run_lark_cli([
    "base", "+field-list",
    "--base-token", app_token,
    "--table-id", table_id,
])
feishu_fields = {f["name"]: f["type"] for f in fields_data.get("fields", [])}
# f["type"] = "datetime" (字符串) -> ft == 5 永远 False!
# 结果: datetime 分支从未执行, 日期被当作文本发送 -> DatetimeFieldConvFail
# 后果: 整批 batch_create 事务回滚, **0 条数据写入飞书**
```

**v4.6.0 改用 OpenAPI**:
```python
# feishu_sync.py
fields = _sync.get_fields(table_id)  # 直接返回 {name: int}, e.g. {"Published Date": 5}
# 整数5与代码 ft == 5 完美兼容, datetime 分支正确执行
```

### 保留 lark-cli 的原因

- **agent 调试**: `lark-cli base +record-list --limit 5` 快速查看数据
- **运维脚本**: 复杂业务逻辑已封装在 `feishu_sync.py`, 但 lark-cli 仍可作 fallback
- **不推荐用于**: 生产代码路径 (v4.6.0 起已切到 OpenAPI)

---

## 飞书 OpenAPI 速查表 (本项目使用)

### 认证

```python
# 1) 获取 tenant_access_token (项目 bot, 来自 .env)
url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
res = requests.post(url, json={
    "app_id": CONFIG['feishu_app_id'],
    "app_secret": CONFIG['feishu_app_secret'],
}, timeout=10)
token = res.json()["tenant_access_token"]
```

### 字段类型 (整数编码)

| type | 含义 |
|------|------|
| 1 | text |
| 2 | number |
| 3 | select / multi_select |
| 4 | checkbox |
| 5 | **datetime** (毫秒时间戳) |
| 7 | auto_number |
| 15 | formula |
| 1001 | created_time |
| 1005 | auto_number |

### 核心 API

```python
# 1) 列出字段
GET /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields
# 返回: {"data": {"items": [{"field_name": "...", "type": 5, ...}]}}

# 2) 列出记录 (1000/页, 支持分页 token)
GET /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records?page_size=1000
# 返回: {"data": {"items": [{"record_id": "...", "fields": {...}}], "has_more": true, "page_token": "..."}}

# 3) 批量创建
POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create
# body: {"records": [{"fields": {"Title": "...", "Published Date": 1780848000000}}]}
# 注意: datetime 字段必须是正整数毫秒 (UTC), 1970 / 0 / 负数会被拒

# 4) 按字段搜索
POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search
# body: {"filter": {"conjunction": "and", "conditions": [{"field_name": "Video ID", "operator": "is", "value": ["abc"]}]}}

# 5) 更新单条
PUT /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}
# body: {"fields": {"AI Review Result": "通过"}}

# 6) 批量删除
POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete
# body: {"records": [{"record_id": "rec..."}]}
```

### 限流处理

飞书 OpenAPI 限流码: `800004135` / `800004134` (read) / `99991400` (write)

`feishu_sync.py` 已内置指数退避重试 (1s/2s/4s/8s)。

---

## 概述

lark-cli 仍可作为 agent 快速调试工具, 但**生产代码一律走 `core/feishu_sync.py` (内部用 OpenAPI)**.

### 优势 (lark-cli, 仅调试用)
- ✅ Agent 原生设计，AI 友好
- ✅ 200+ 命令，覆盖所有业务域
- ✅ 结构化输出，易于解析
- ✅ Dry Run 预览

### 劣势 (本项目生产代码弃用)
- ❌ 200 条/页, 限制严重
- ❌ type 是字符串, 需映射
- ❌ 子进程开销大, 不适合大批量

## 安装

### 方法1: npm 安装（推荐）

```bash
npx @larksuite/cli@latest install
```

### 方法2: 从源码安装

需要 Go v1.23+ 和 Python 3:

```bash
git clone https://github.com/larksuite/cli.git
cd cli
make install
```

## 配置

### 1. 初始化配置（一次性）

```bash
lark-cli config init
```

这会引导你配置应用凭证（App ID / App Secret）。

### 2. 登录授权

```bash
# 推荐模式（自动选择常用权限范围）
lark-cli auth login --recommend

# 或指定特定域
lark-cli auth login --domain base,sheets,docs
```

### 3. 验证状态

```bash
lark-cli auth status
```

## 多维表格操作示例

### 查看表信息

```bash
# 列出 Base 中的所有表
lark-cli base +table-list --base-token <BITABLE_TOKEN>

# 查看字段定义
lark-cli base +field-list --base-token <BITABLE_TOKEN> --table-id <TABLE_ID>
```

### 记录查询

```bash
# 获取记录（自动分页）
lark-cli base +record-list --base-token <BITABLE_TOKEN> --table-id <TABLE_ID> --page-all

# 获取前100条记录
lark-cli base +record-list --base-token <BITABLE_TOKEN> --table-id <TABLE_ID> --page-size 100
```

### 创建/更新记录

```bash
# 创建单条记录（dry run 预览）
lark-cli base +record-create \
    --base-token <BITABLE_TOKEN> \
    --table-id <TABLE_ID> \
    --fields '{"Title": "测试视频", "Views": "1000"}' \
    --dry-run

# 实际创建
lark-cli base +record-create \
    --base-token <BITABLE_TOKEN> \
    --table-id <TABLE_ID> \
    --fields '{"Title": "测试视频", "Views": "1000"}'

# 批量创建（从 JSON 文件）
lark-cli base +record-batch-create \
    --base-token <BITABLE_TOKEN> \
    --table-id <TABLE_ID> \
    --file records.json
```

## 输出格式

```bash
# JSON 格式（默认，适合程序解析）
lark-cli base +record-list ... --format json

# 表格格式（人类可读）
lark-cli base +record-list ... --format table

# NDJSON 格式（流式处理）
lark-cli base +record-list ... --format ndjson

# CSV 格式（导入 Excel）
lark-cli base +record-list ... --format csv
```

## 在项目中使用

### Python 子进程调用

```python
import subprocess
import json

def get_feishu_records(table_id: str) -> list:
    """使用 lark-cli 获取飞书记录"""
    cmd = [
        "lark-cli", "base", "+record-list",
        "--base-token", CONFIG['feishu_bitable_token'],
        "--table-id", table_id,
        "--page-all",
        "--format", "json"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"[ERR] lark-cli 失败: {result.stderr}")
        return []
    
    data = json.loads(result.stdout)
    return data.get("data", {}).get("items", [])
```

### Shell 脚本集成

```bash
#!/bin/bash
# sync_to_feishu.sh - 使用 lark-cli 同步数据到飞书

BITABLE_TOKEN="your_bitable_app_token"
BILI_TABLE="tbl_BILIBILI"
YT_TABLE="tbl_YOUTUBE"

echo "[*] 同步 Bilibili 数据..."
lark-cli base +record-batch-create \
    --base-token $BITABLE_TOKEN \
    --table-id $BILI_TABLE \
    --file bili_records.json

echo "[*] 同步 YouTube 数据..."
lark-cli base +record-batch-create \
    --base-token $BITABLE_TOKEN \
    --table-id $YT_TABLE \
    --file yt_records.json
```

## 与现有代码的关系

| 组件 | 用途 | 说明 |
|------|------|------|
| `feishu_sync.py` | 核心 Python SDK 封装 | 用于复杂的业务逻辑 |
| `lark-cli` | 命令行快速操作 | 用于调试、运维、脚本自动化 |
| `safe_push_*.py` | 安全推送工具 | 特定场景的强制写入 |

**推荐使用场景**:
- **日常开发**: 使用 `feishu_sync.py`（已有完整逻辑）
- **调试排查**: 使用 `lark-cli`（快速查看数据）
- **运维脚本**: 使用 `lark-cli` Shell 命令
- **CI/CD**: 使用 `lark-cli`（无需 Python 环境）

## 高级功能

### Dry Run 预览

在执行写操作前预览请求：

```bash
lark-cli base +record-create \
    --base-token xxx \
    --table-id yyy \
    --fields '{"Title": "test"}' \
    --dry-run
```

### Schema 自省

查看 API 参数定义：

```bash
# 列出所有可用命令
lark-cli schema

# 查看特定命令的参数
lark-cli schema base.record.create
```

### 身份切换

以用户或 Bot 身份执行：

```bash
# 以用户身份查看日历
lark-cli calendar +agenda --as user

# 以 Bot 身份发送消息
lark-cli im +messages-send --as bot --chat-id "oc_xxx" --text "Hello"
```

## 故障排除

### 问题1: 命令不存在

```bash
# 确保 PATH 包含 lark-cli
which lark-cli

# 或使用 npx 直接运行
npx @larksuite/cli@latest base +record-list ...
```

### 问题2: 授权失败

```bash
# 检查登录状态
lark-cli auth status

# 重新登录
lark-cli auth login --recommend
```

### 问题3: 权限不足

确保应用有足够的权限范围：

```bash
# 查看当前权限
lark-cli auth scopes

# 重新登录并授权更多权限
lark-cli auth login --scope "bitable:app,bitable:app:readonly"
```

## 参考链接

- [GitHub 仓库](https://github.com/larksuite/cli)
- [官方文档](https://www.feishu.cn/feishu-cli)
- [Skills 列表](https://github.com/larksuite/cli/blob/main/skills)
- [更新日志](https://open.larkoffice.com/changelog)

---

**版本**: v1.0.44 (2026-05-29)  
**许可证**: MIT
