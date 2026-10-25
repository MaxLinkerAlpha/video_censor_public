# Handoff - AI 交接文档

> **目标读者**: AI 助手 | **版本**: v4.0.1 | **更新**: 2026-05-08

---

## META

```yaml
project: Video_Censor
version: v4.0
status: production
stack:
  lang: python3.9+
  ai_api: siliconflow (deepseek/kimi/glm/qwen)
  storage: feishu_bitable + local_xlsx
  proxy: clash_meta (port:17891)
key_files:
  entry:
    - main.py                    # 完整流程入口
    - core/workflow_orchestrator.py # 分阶段调度器
  config:
    - .env                       # 环境变量（含密钥）
    - config.yaml                # Clash代理配置
    - core/config.py             # 配置解析器
  core_logic:
    - core/video_subtitle_censor7.py  # 字幕+审核主逻辑
    - core/prescreen.py               # B站初筛
    - core/prescreen_youtube.py       # YouTube初筛
    - core/scoring.py                 # 评分算法
    - core/feishu_sync.py             # 飞书同步
  platforms:
    - platforms/bilibili_client.py    # B站API
    - platforms/youtube_client.py     # YouTube Playwright
  tools:
    - tools/smart_proxy_rotator.py       # 代理诊断（独立工具）
    - tools/rollback_fetcher.py         # 回滚工具
    - tools/sync_report_to_feishu.py     # 报告同步
    - tools/retry_failed_youtube_v2.py  # YouTube重试
  core_extra:
    - core/ai_client.py                # AI模型调用
    - core/video_processor.py           # 视频处理编排
    - core/reporter.py                  # 报告生成器
```

---

## ARCHITECTURE

### 模块依赖图

```
main.py
 └─> core/workflow_orchestrator.py [调度器]
      ├─> core/bilibili_youtube_once_for_all_fetcher.py [采集]
      │    ├─> core/console_utils.py [代理检测]
      │    └─> core/feishu_sync.py [飞书]
      ├─> core/prescreen.py [B站初筛] / prescreen_youtube.py [YT初筛]
      │    └─> core/scoring.py [D1/D2评分]
      ├─> core/video_subtitle_censor7.py [字幕+审核]
      │    ├─> platforms/bilibili_client.py → video_parser.py
      │    ├─> platforms/youtube_client.py → video_parser.py
      │    ├─> core/ai_client.py [AI调用]
      │    ├─> core/scoring.py [D3评分]
      │    ├─> core/youtube_rate_limiter.py [YT降频]
      │    └─> core/feishu_sync.py
      └─> core/feishu_sync.py [最终同步]

core/ai_client.py [核心模块]
 └─> 被 video_subtitle_censor7.py 调用
```

### 数据流

```
video_urls.txt / 飞书表格
  ↓ [采集]
data/Bilibili_*.xlsx + data/YouTube_*.xlsx
  ↓ [初筛选]
data/.prescreen_state.json (增量状态)
  ↓ [人工初筛]
飞书表格更新 (优先级字段)
  ↓ [字幕获取+审核]
字幕文本 + AI审核结果
  ↓ [综合评分]
Total_Review_Report.md (本地报告)
  ↓ [飞书同步]
飞书多维表格 (评分/结论/状态)
```

---

## CONFIGURATION

### 必填环境变量 (.env)

```bash
# ===== AI服务 =====
SILICONFLOW_API_KEY=sk-xxx          # 必须：硅基流动API Key
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1/chat/completions

# ===== 飞书应用凭证 (安全敏感) =====
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx  # 必须：飞书应用ID
FEISHU_APP_SECRET=xxxxxxxxxxxxxxx   # 必须：飞书应用密钥
FEISHU_BITABLE_TOKEN=xxxxxxxxxxxxx # 必须：多维表格Token (B站/YouTube共用)
BILIBILI_TABLE_ID=tblxxxxxxxxxx    # 必须：B站视频表ID
YOUTUBE_TABLE_ID=tblxxxxxxxxxx     # 必须：YouTube视频表ID
CREATOR_TABLE_ID=tblxxxxxxxxxx     # 可选：创作者表ID

# ===== YouTube API (安全敏感) =====
YT_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxx # 必须：YouTube Data API Key

# ===== Bilibili =====
BILIBILI_COOKIE=buvid_xx=xxx;...    # 必须：完整Cookie字符串
BILIBILI_MAX_WORKERS=3              # 并发数(建议2-3)
BILIBILI_REQUEST_INTERVAL=2         # 请求间隔(秒)

# ===== YouTube =====
YT_ENABLE_PLAYWRIGHT=1              # 启用Playwright
HTTP_PROXY=http://127.0.0.1:17891   # 代理地址(YT必须)
HTTPS_PROXY=http://127.0.0.1:17891

# ===== AI模型配置（逗号分隔，按优先级排列）=====
SILICONFLOW_MODEL_CHEAP=deepseek-ai/DeepSeek-V4-Flash,Qwen/Qwen3.6-35B-A3B
SILICONFLOW_MODEL_HEAVY=Pro/deepseek-ai/DeepSeek-V3.2,Pro/moonshotai/Kimi-K2.6,...
SILICONFLOW_CHEAP_THRESHOLD=5000     # 字符阈值(<=cheap用便宜模型)
```

> **⚠️ 安全提醒**: 所有敏感信息（API Keys、Tokens、Secrets、Cookies）**必须**存储在 `.env` 文件中，禁止硬编码到源代码。`.env` 已在 `.gitignore` 中排除，不会被提交到 Git。

### 关键配置文件路径

| 文件 | 用途 | 敏感 |
|------|------|------|
| `.env` | 所有环境变量 | **是** (含API Key/Cookie) |
| `config.yaml` | Clash代理节点列表 | 是 (含密码) |
| `bilibili_cookies.json` | B站Cookie备份 | **是** |
| `youtube_cookies.json` | YouTube Cookie备份 | **是** |
| `core/config.py` | 配置解析逻辑 | 否 |
| `video_urls.txt` | 待处理视频URL列表 | 否 |

### 运行时数据文件

| 文件 | 用途 | 可删除? |
|------|------|---------|
| `data/.prescreen_state.json` | 初筛进度 | 删后全量重跑 |
| `data/scheduler_state.json` | 调度器状态 | 删后重置 |
| `data/selection_history.json` | 入选历史(M1系数) | **不可删** |
| `data/.feishu_sync_log.json` | 同步日志 | 可清空 |
| `data/Bilibili_*.xlsx` | B站本地DB | **不可删** |
| `data/YouTube_*.xlsx` | YouTube本地DB | **不可删** |

---

## WORKFLOWS

### 完整流程命令

```bash
# Windows快捷方式
run.bat                              # 完整流程(交互式)
run_from_prescreen.bat               # 从人工初筛开始

# Python直接调用
python main.py                       # 完整流程
python main.py --days 3              # 仅处理最近3天视频

# 分阶段执行
python core/workflow_orchestrator.py --stage fetch           # 仅采集
python core/workflow_orchestrator.py --stage prescreen       # 仅机器初筛
python core/workflow_orchestrator.py --stage subtitle        # 仅字幕+审核
python core/workflow_orchestrator.py --stage feishu          # 仅飞书同步
python core/workflow_orchestrator.py --stage human-prescreen # 从人工初筛开始

# 平台选择
python core/workflow_orchestrator.py --stage human-prescreen --platform all        # 全平台
python core/workflow_orchestrator.py --stage human-prescreen --platform bilibili  # 仅B站
python core/workflow_orchestrator.py --stage human-prescreen --platform youtube   # 仅YouTube

# 自动化模式(无交互)
python core/workflow_orchestrator.py --full --auto
```

### 断点续传机制

**触发条件**: 任意阶段中断后重新运行

**实现方式**:
1. 检查 `data/.prescreen_state.json` → 跳过已初筛视频
2. 检查 `data/scheduler_state.json` → 跳过已完成任务
3. 检查 `data/.feishu_sync_log.json` → 跳过已同步记录

**重置方法**:
```bash
# 删除状态文件强制全量重跑
del data\.prescreen_state.json
del data\scheduler_state.json
```

---

## KEY MODULES DETAILS

### 1. workflow_orchestrator.py (调度器)

```python
class WorkflowOrchestrator:
    """v4.0 - 支持平台/阶段选择"""
    
    def run_stage_fetch(self): ...
    def run_stage_prescreen(self): ...
    def run_stage_subtitle(self): ...
    def run_stage_feishu(self): ...
    def run_human_prescreen_menu(self): ...  # 交互菜单
    
    # CLI参数
    # --stage: fetch|prescreen|subtitle|feishu|human-prescreen
    # --platform: all|bilibili|youtube
    # --full: 完整流程
    # --auto: 自动模式
    # --status: 显示当前状态
```

**关键方法**:
- `_diagnose_proxy()`: 代理检测（返回bool）
- `_interactive_menu_after_fetch()`: 采集后撤销/重跑菜单
- `_build_video_url(platform, video_id)`: URL构建

### 2. video_subtitle_censor7.py (字幕+审核核心)

```python
def process_video(video_url: str) -> Dict:
    """
    主处理函数，返回:
    {
        'success': bool,
        'video_id': str,
        'platform': str,
        'has_subtitle': bool,
        'subtitle_text': str,
        'censor_result': str,      # AI审核结果
        'scores': {                # 评分明细
            'd1': float,           # 时长
            'd2': float,           # 内容相关性
            'd3': float,           # 质量
            'total': float,
            'grade': str           # A/B/C/D
        },
        'error': str
    }
    """
```

**依赖**: `ai_client.py`, `scoring.py`, `platforms/*`, `youtube_rate_limiter.py`

### 3. ai_client.py (AI模型调用)

```python
def call_api(
    prompt: str,
    system_prompt: str = None,
    model: str = None,           # None则自动选择
    max_tokens: int = 2000,
    temperature: float = 0.7
) -> str:
    """
    自动模型切换逻辑:
    1. 如果 len(prompt) <= cheap_threshold → 用 MODEL_CHEAP[0]
    2. 否则 → 用 MODEL_HEAVY[0]
    3. 失败 → 尝试列表中下一个模型
    4. 全部失败 → 抛异常
    """
```

**模型回退链示例**:
```
Heavy模型失败顺序:
Pro/deepseek-ai/DeepSeek-V3.2
  → Pro/moonshotai/Kimi-K2.6
  → Pro/zai-org/GLM-5.1
  → deepseek-ai/DeepSeek-V4-Flash (兜底)
  → Qwen/Qwen3.6-27B (最后手段)
```

### 4. scoring.py (评分算法)

```python
def calculate_video_score(video_info: Dict, platform: str) -> Tuple[float, Dict]:
    """
    D1 (时长): 30%权重
      - <3min: 0分
      - 3-10min: 线性增长 (3→60, 10→100)
      - >10min: 100分

    D2 (内容相关性): 25%权重
      - AI评估与目标主题的相关程度 (0-100)

    D3 (质量深度): 35%权重
      - AI评估内容质量、信息密度 (0-100)

    M1 (新人系数): 10%权重
      - 新UP主(粉丝<1万): +20分加成
      - 老UP主: 0分

    Total = D1*0.3 + D2*0.25 + D3*0.35 + M1*0.1
    Grade: A(>=80) / B(>=65) / C(>=50) / D(<50)
    """
```

### 5. feishu_sync.py (飞书同步)

```python
class FeishuSync:
    """飞书多维表格操作封装"""
    
    TABLE_CONFIG = {
        'bilibili': {
            'token': BILI_BITABLE_TOKEN,
            'table_id': BILI_TABLE_ID,
            'fields': {
                'BV号': 'video_id',
                '标题': 'title',
                '字幕获取状态': 'subtitle_status',  # pending/success/failed
                '综合评分': 'total_score',
                '评分等级': 'grade',                # A/B/C/D
                'AI审核结果': 'censor_result',      # safe/unsafe/pending
                # ...
            }
        },
        'youtube': {
            'token': BILI_BITABLE_TOKEN,  # 同一Base
            'table_id': YT_TABLE_ID,
            'fields': {
                'Video ID': 'video_id',
                'Title': 'title',
                'Subtitle Status': 'subtitle_status',
                'Total Score': 'total_score',
                'Rating': 'grade',
                'AI Review Result': 'censor_result',
                # 注意: YouTube用英文字段名！
            }
        }
    }
    
    def validate_required_fields(platform='both') -> bool: ...
    def get_videos_for_human_prescreen(platform, days_filter=7) -> Dict: ...
    def update_record(table_id, record_id, fields) -> bool: ...
```

**重要**: B站和YouTube的字段名不同！B站中文，YouTube英文。

### 6. youtube_rate_limiter.py (降频模块)

```python
class YouTubeRateLimiter:
    """
    内置到 video_subtitle_censor7.py 的YouTube请求控制
    
    策略:
    - 基础延迟: random.uniform(2, 5) 秒
    - 批量暂停: 每10个视频 pause(10-30秒)
    - 失败退避: retry * 2^attempt 秒 (最多3次)
    - 并发限制: 同时最多1个YouTube请求
    """
    
    def wait_before_request(self): ...
    def on_success(self): ...
    def on_failure(self, attempt): ...
    def should_retry(self, attempt) -> bool: ...
```

**注意**: 此模块已集成到主流程，**无需手动调用**。

---

## COMMON TASKS

### 任务1: 添加新视频来源

**步骤**:
1. 编辑 `video_urls.txt` 或在飞书表格添加记录
2. 运行 `run_from_prescreen.bat`
3. 系统自动检测新视频并处理

### 任务2: 更新Bilibili Cookie

**时机**: 提示"Cookie过期或无效"

**步骤**:
1. 浏览器登录 bilibili.com
2. F12 → Application → Cookies → 复制全部
3. 粘贴到 `.env` 的 `BILIBILI_COOKIE=` 行
4. 备份到 `bilibili_cookies.json`

### 任务3: 切换AI模型

**编辑 `.env`**:
```bash
# 更换主力模型
SILICONFLOW_MODEL_HEAVY=NewModelName,BackupModel1,BackupModel2

# 更换轻量模型
SILICONFLOW_MODEL_CHEAP=CheapModel1,CheapModel2
```

**可用模型前缀** (硅基流动):
- `deepseek-ai/` - DeepSeek系列
- `Pro/deepseek-ai/` - DeepSeek Pro版
- `Qwen/` - 通义千问系列
- `moonshotai/` - Kimi系列
- `zai-org/` - 智谱GLM系列

### 任务4: 修复飞书字段不匹配

**症状**: `FieldNameNotFound` 错误

**排查步骤**:
1. 运行字段验证:
   ```python
   from core.feishu_sync import FeishuSync
   fs = FeishuSync()
   fs.validate_required_fields()
   ```
2. 对比错误信息与 `feishu_sync.py` 中的 `TABLE_CONFIG`
3. 修正字段名（注意空格/大小写）

### 任务5: 处理YouTube IP封禁

**症状**: 反复429错误或连接超时

**解决方案(按优先级)**:

1. **等待自动恢复** (推荐)
   - 降频模块会自动降低频率
   - 通常24小时后解封

2. **切换代理节点**
   ```bash
   python tools/smart_proxy_rotator.py --test    # 测试可用节点
   python tools/smart_proxy_rotator.py --best    # 推荐最佳节点
   ```
   - 在Clash客户端切换到推荐节点
   - 确认端口仍为17891（或更新.env）

3. **手动调整降频参数**
   ```python
   # 在 youtube_rate_limiter.py 中修改
   BASE_DELAY_MIN = 5    # 默认2 → 改为5
   BASE_DELAY_MAX = 10   # 默认5 → 改为10
   ```

### 任务6: 回滚到某个时间点

```bash
# 查看可用快照
python tools/rollback_fetcher.py --list

# 回滚到指定快照
python tools/rollback_fetcher.py --restore snapshot_20260506_120000

# 回滚Excel文件
python tools/rollback_fetcher.py --restore-excel backup_20260506.xlsx
```

---

## DEBUGGING

### 日志位置

| 日志类型 | 路径 | 内容 |
|----------|------|------|
| AI错误日志 | `data/ai_parse_errors.log` | AI模型调用失败详情 |
| 代理监控 | `data/.proxy_monitor.log` | 代理健康度历史 |
| 飞书同步 | `data/.feishu_sync_log.json` | 同步成功/失败记录 |
| 初筛状态 | `data/.prescreen_state.json` | 已处理视频ID列表 |
| 控制台输出 | stdout | 实时运行日志 |

### 常见错误码

| 错误 | 原因 | 解决方案 |
|------|------|----------|
| `429 Too Many Requests` | YouTube限流 | 等待或切换IP |
| `FieldNameNotFound` | 飞书字段不匹配 | 检查字段映射 |
| `Cookie失效` | B站登录过期 | 更新Cookie |
| `API Key无效` | 硅基流动认证失败 | 检查余额/API Key |
| `Playwright未安装` | 缺少浏览器引擎 | `playwright install chromium` |
| `代理连接失败` | Clash未启动/端口错误 | 启动Clash并确认端口 |

### 调试模式

```bash
# 单视频测试
python -c "
from core.video_subtitle_censor7 import process_video
result = process_video('https://www.bilibili.com/video/BV1xx')
print(result)
"

# AI模型测试
python -c "
from ai_client import call_api
resp = call_api('测试提示词')
print(resp)
"

# 飞书连接测试
python -c "
from core.feishu_sync import FeishuSync
fs = FeishuSync()
print(fs.validate_required_fields())
"
```

---

## FILE CLEANUP GUIDE

### 可安全删除的文件

```powershell
# 根目录重复文件
Remove-Item "Bilibili_local_db.xlsx"         # 与data/重复
Remove-Item "_run_bili_only.py"                  # 临时调试脚本
Remove-Item "test_ytdlp.py", "test_ytdlp_v2.py"  # 废弃测试
Remove-Item "test_d1_d4_feishu_sync.py"          # 旧测试脚本

# archives目录（整个可清理或保留最新版本）
Remove-Item -Recurse "archives\test_scripts\"     # 7个废弃测试脚本
Remove-Item -Recurse "archives\youtube_retry_scripts\"  # 8个旧重试脚本

# 一次性修复脚本（可归档）
Move-Item "fix_yt_prescreen_status.py" "archives\"
Move-Item "retry_failed_youtube.py" "archives\"
Move-Item "retry_single.py" "archives\"
Move-Item "ai_retry.py" "archives\"
```

### 绝对不能删除的文件

```
core/*.py                          # 所有核心模块（含ai_client, video_processor, reporter）
platforms/*.py                    # 平台适配层
tools/*.py                        # 独立工具脚本
main.py                           # 主入口
run.bat, run_from_prescreen.bat   # 启动脚本
.env, config.yaml                 # 配置文件
data/*.xlsx                       # 本地数据库
data/selection_history.json       # 入选历史
docs/*.md                         # 文档
```

详细分析见: [docs/FILE_ANALYSIS.md](./docs/FILE_ANALYSIS.md)

---

## BEST PRACTICES

### 代码修改检查清单

- [x] UTF-8编码声明 (`# -*- coding: utf-8 -*-`)
- [x] 函数添加Type Hints
- [x] 异常捕获使用具体类型 (禁止裸 `except:`)
- [x] **敏感信息通过环境变量读取 (禁止硬编码Key/Password)** ✅ v4.0.1已完成
- [x] 路径使用 `pathlib.Path` 或 `os.path.join()` (禁止硬编码分隔符)
- [x] 文件读写使用 `encoding='utf-8'`
- [x] 遵循KISS原则 (避免过度抽象)
- [ ] 单一函数长度<100行

### 性能优化建议

1. **B站并发数**: 建议2-3（过高触发风控）
2. **YouTube间隔**: 不要低于2秒（内置降频已处理）
3. **AI并发**: 默认10线程（可根据API限额调整）
4. **飞书同步**: 并发5线程（过高触发频率限制）

### 安全注意事项

- ⚠️ `.env` 包含敏感信息，**切勿提交到Git**
- ⚠️ `config.yaml` 包含代理密码，**勿公开分享**
- ⚠️ Cookie文件包含登录凭证，定期更新
- ✅ Git仓库应包含 `.gitignore` 排除规则:
  ```
  .env
  config.yaml
  *_cookies.json
  data/*.xlsx
  __pycache__/
  *.pyc
  .prescreen_state.json
  scheduler_state.json
  ```

---

## VERSION HISTORY

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v4.0 | 2026-05-07 | 平台/阶段参数化; YT降频; 代理诊断工具; 文件清理 |
| v3.5 | 2026-05-06 | 交互式初筛菜单; AI模型回退链; 飞书验证 |
| v3.4 | 2026-05-05 | 模块化重构(core/platforms); 飞书同步; 断点续传 |
| v3.0 | 2026-04-20 | 初始版本 |

---

## QUICK REFERENCE CARD

```bash
# 常用命令速查
run_from_prescreen.bat                                    # 从初筛开始(最常用)
python core/workflow_orchestrator.py --status              # 查看当前状态
python tools/smart_proxy_rotator.py --test                      # 测试代理
python tools/rollback_fetcher.py --list                          # 查看快照
python tools/sync_report_to_feishu.py                            # 手动同步报告到飞书

# 关键文件路径
Config: .env | core/config.py
Entry: main.py | core/workflow_orchestrator.py
Core: core/video_subtitle_censor7.py | core/ai_client.py
Data: data/ | Total_Review_Report.md
Docs: README.md | docs/

# 端口/服务
Proxy: 127.0.0.1:17891 (Clash)
Feishu: API (via feishu_sync.py)
AI: api.siliconflow.cn (via core/ai_client.py)

# 字段名速记
B站: BV号 | 标题 | 字幕获取状态 | 综合评分 | 评分等级 | AI审核结果
YouTube: Video ID | Title | Subtitle Status | Total Score | Rating | AI Review Result
```

---

**END OF HANDOFF DOCUMENT**
