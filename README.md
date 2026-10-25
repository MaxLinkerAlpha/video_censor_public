# Video_Censor - 视频内容审查系统

<h1 align="center">⚠️ 由于分享需要，部分做了脱敏处理，<br>未经测试，可能存在部分BUG ⚠️</h1>

<h3 align="center">DESENSITIZED FOR SHARING · NOT FULLY TESTED · MAY CONTAIN BUGS</h3>

<p align="center">© 2026 Max Linker Alpha. All Rights Reserved. 本仓库仅用于作品展示，未经许可不得复制、修改或分发。</p>

---

> **版本**: v5.4.6 | **状态**: 分享脱敏未测试版 | **最后更新**: 2026-09-20

## 项目简介

Video_Censor 是一个**自动化视频内容审查系统**，专门用于从 **Bilibili** 和 **YouTube** 抓取视频字幕，并通过 AI 模型进行智能内容审核与评分。

### 核心能力

- [x] **多平台支持**: Bilibili + YouTube 双平台视频抓取
- [x] **智能审核**: 基于 AI 大模型的内容安全检测（支持多模型自动切换）
- [x] **评分体系**: D1(时长) + D2(时效) + D3(互动) + D4(AI吸引力) + M1(新人系数) + 优先级加权 多维度评分
- [x] **飞书集成**: 审查结果自动同步到飞书多维表格（安全类型转换 + 全OpenAPI 1000条/页）
- [x] **断点续传**: 中断后可从断点恢复，无需重新运行
- [x] **增量初筛**: YouTube 初筛仅处理新增记录，性能提升 ~10x（v3.2.0）
- [x] **自动备份回滚**: 数据变更前自动备份 Excel，一键恢复到任意历史版本（v2.0）
- [x] **平台选择**: 支持全流程或单平台/单阶段执行
- [x] **请求降频**: 内置 YouTube 降频模块，适应平台速率限制
- [x] **飞书OpenAPI集成**: v4.6.0起**弃用lark-cli**, 全面改用飞书OpenAPI (单次1000条/页, 无lark-cli的200条限制)

---

## 📚 文档索引

| 文档 | 说明 |
|------|------|
| **[FEISHU_CLI_GUIDE.md](docs/FEISHU_CLI_GUIDE.md)** | 飞书 CLI 完整使用指南（安装、配置、示例） |
| **[LESSONS.md](docs/LESSONS.md)** | 踩坑复盘与工程经验 |
| **[CHANGELOG.md](core/CHANGELOG.md)** | 版本更新历史 |

---

## 快速开始

### 环境要求

- **Python**: 3.9+
- **操作系统**: Windows 10/11 (推荐), Linux, macOS
- **网络**: 需要**代理**访问 YouTube（Bilibili 不需要代理）
- **依赖安装**:

```bash
pip install -r requirements.txt
```

主要依赖：
- `requests` - HTTP 请求
- `openpyxl` - Excel 文件处理
- `playwright` - YouTube 字幕获取（可选）
- `lxml` - HTML 解析

### 可选：飞书 CLI（推荐）

用于快速操作飞书多维表格，提升调试和运维效率：

```bash
# 一键安装
npx @larksuite/cli@latest install

# 初始化配置
lark-cli config init

# 登录授权
lark-cli auth login --recommend

# 验证安装
lark-cli base +table-list --base-token $BITABLE_TOKEN
```

> 详细使用指南见 [docs/FEISHU_CLI_GUIDE.md](docs/FEISHU_CLI_GUIDE.md)

### 配置步骤

#### 1. 配置环境变量

复制并编辑 `.env` 文件：

```bash
# 必填：硅基流动 API Key（用于 AI 审核）
SILICONFLOW_API_KEY=your-api-key-here

# 必填：Bilibili Cookie（用于 B站 视频抓取）
BILIBILI_COOKIE=your-bilibili-cookie-here

# 可选：代理配置（YouTube 必须）
HTTP_PROXY=http://127.0.0.1:17891
HTTPS_PROXY=http://127.0.0.1:17891
```

#### 2. 配置飞书多维表格

在 `.env` 中配置飞书相关参数（**所有凭证必须通过 .env 配置，禁止硬编码到源代码**）：

```bash
# 飞书应用凭证 (安全敏感)
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx       # 飞书应用ID
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxx        # 飞书应用密钥
FEISHU_BITABLE_TOKEN=xxxxxxxxxxxxxxxx     # 多维表格Token (B站和YouTube共用)

# 数据表ID
BILIBILI_TABLE_ID=tblxxxxxxxxxx          # B站视频表ID
YOUTUBE_TABLE_ID=tblxxxxxxxxxx           # YouTube视频表ID
CREATOR_TABLE_ID=tblxxxxxxxxxx            # 创作者表ID (可选)

# YouTube API (安全敏感)
YT_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxx    # YouTube Data API Key
```

#### 3. 初始化 Playwright（仅 YouTube）

```bash
playwright install chromium
```

---

## 使用方法

### 方式一：完整工作流（推荐新手）

```bash
# Windows - 全平台一键运行（推荐入口，v5.0.0+）
run.bat

# 或手动运行（等价命令，实际执行 core/run_workflow.py）
python core/run_workflow.py
```

> **说明 (v5.0.0+)**: `run.bat` 已改为调用 `python core/run_workflow.py` 作为**唯一全流程入口**，依次执行：
> **Cookie 检测 → 视频采集 (Fetcher) → Stage 1.5 标记 (Discord 自荐 + 中文精选来源注册, v5.4.0 前移至此, 失败不阻断) → 机器初筛 Prescreen (Bili/YT) → 脚本去重 ScriptDedup (Bili/YT) → orchestrator `--stage human-prescreen` (Bili → YT) → 来源视频优先级翻转 + <8 复活补跑 (v5.4.0)**，其中 orchestrator 内部包含 字幕获取 + AI 审核 + 飞书同步（v5.3.5 起同步时自动写入「语言_Language」AI 语言分类字段）。
> v5.4.0 起 Discord 自荐/中文精选视频**不再前置写高优先级**，而是以普通身份走完整流程（机器初筛 → 去重 → AI 审核+评分），AI 审核完成后才统一翻转为 High Priority；评分时按来源注册表 (`core/source_registry.py` → `data/source_registry.json`) 直接给 +10 加权。
> v5.4.2 起 ScriptDedup 的"本批"边界改用**运行锚点**（`core/run_anchor.py` → `data/run_anchor.json`，记录上次运行的 start/end），不再依赖固定 7 天窗口 —— 因此**提前或延后启动工作流都不会影响入选数量**。锚点由 `run_workflow.py` 在完整成功时写入。
> v5.4.3 修复失败视频的飞书状态回写：此前字幕获取失败的结果字典缺 `video_id`，导致 `Subtitle Status`（failed/pending）被**静默跳过**；现已补齐 `video_id` + `error_category`（`fetch_youtube_subtitle` 签名不变，仅把分类写回结果字典）。
> 判定口径：`no_subtitle / region_locked / video_unavailable` → `Subtitle Status=failed`（视频级终态，仅写该字段）；
> `ip_blocked / rate_limit / ...` → `pending`（换节点后可重试）。
> v5.4.4 修复「同一频道在同一语言下多次入选」：① ScriptDedup 分组时**未知语言不再自成一组**（优先归入该频道本批的已知语言组，剩余进入不占配额的"未知语言补充位"）；② 来源视频翻转**不再覆盖已判为"拒绝"的记录**（`[FLIP-SKIP]`）；③ Stage 3 收尾新增**兜底校验**：按最终「语言_Language」+ 频道复查，某语言通过 AI 数 > 8 时同频道只留总分最高 1 条（只作用于**本批窗口**，不会回改历史各周）。
> 手动执行兜底校验：`python core/prescreen.py --enforce-lang-dedup [--dry-run] [--days N] [--no-window]`
> v5.4.5 修复 B站「发布日期」被写成"抓取当天"：`parseDate()` 原先不认 B站中文日期文本（`3天前` / `9月16日`），
> 28/42 张卡片落到最后的兜底 `return`，而该分支返回 `new Date()`（运行当天）—— 静默伪造日期。现已补全格式分支，
> 并把兜底改为**返回空串**（解析失败不写该字段）；`X小时前` 取区间中点以减少跨午夜偏差。
> 实测重跑 162 条与官方 API `pubdate` **逐条一致**。注意：该字段被机器初筛用于 ">21 天" 时效过滤，
> 但 **D2 时效分来自 `bilibili_client` 官方 API，不受影响**。
> **YouTube 侧不受此问题影响**（同日核查近 35 天 252 条正向标记）：`Published Date` / `Published At` / `Duration`
> 与官方 API **0 不一致** —— 因为 YouTube 走官方 API 的 `publishedAt`，不解析网页文本。
> 同日另修正了一类数据问题：44 条「`Human Prescreen` 为正向标记、但 `Subtitle Status` / `AI Review` 为空、
> `Total Score = 0`」的记录（v5.4.3 修复前的静默跳过所致）—— 其中 42 条实测确实无字幕，已补写
> `Subtitle Status=failed` + `AI Review Result=unsafe` 并置 `Rejected`；2 条字幕可恢复的已补跑（总分 0.0 / 5.95，均 <8）同样置 `Rejected`。
> 复核：近 35 天正向标记 252 → 208，**「挂正向标记但 AI 未完成」= 0 条**。
> **v5.4.6 修复 YouTube 评分 D2/D3 恒为 0**（近 35 天 208/208 条 D2=D3=0，评分长期退化成 `(D1+D4)/2`）：
> 根因是 v3.9.0 设计的"YouTube 从飞书回填统计数据"实现**在重构中被删除**
> （旧实现在 `_deprecated_video_subtitle_censor7.py`，且未调 `set_bitable_token()` 从未真正生效；
> `tools/yt_enrich.py` 是当时的补偿工具）。现改为在 `calculate_video_score` 中调
> YouTube Data API v3 **实时补全** `publish_time`/`views`/`likes`/`comments`/`channel_id`，
> 并写回 `video_info` 复用；同时给 `calc_d3_engagement_score` 加**最小样本门槛 `观看 ≥ 100`**
> （否则 `views=5` + 1 个赞就能拿到 D3 满分，总分虚高到 19.5）。
> 按用户决定**仅对今后新批次生效，不重算历史数据**。
> 旧入口 `python main.py` 已非主流程，完整自动化请使用 `run.bat` / `python core/run_workflow.py`。

### 方式二：分阶段执行（高级用户）

```bash
# 从脚本去重后的审核阶段开始（续跑常用）
run_from_prescreen.bat

# 或指定平台
run_from_prescreen.bat --platform youtube    # 仅处理 YouTube
run_from_prescreen.bat --platform bilibili   # 仅处理 Bilibili
run_from_prescreen.bat --platform all        # 处理所有平台（默认）

# 指定单个阶段
python core/workflow_orchestrator.py --stage fetch            # 仅抓取视频
python core/workflow_orchestrator.py --stage prescreen        # 仅机器初筛
python core/prescreen.py --script --platform bilibili         # 仅脚本去重 (ScriptPrescreen v5.0.0+, B站)
python core/prescreen.py --script --platform youtube          # 仅脚本去重 (ScriptPrescreen v5.0.0+, YouTube)
python core/workflow_orchestrator.py --stage subtitle         # 仅获取字幕+审核
python core/workflow_orchestrator.py --stage feishu           # 仅同步飞书
python core/workflow_orchestrator.py --stage human-prescreen  # 从字幕+AI审核+飞书同步的最后一步开始（可加 --auto 续跑）

# 标记类阶段 (v5.4.0 前移为 Stage 1.5: Fetcher 之后 / Prescreen 之前, 失败不阻断主流程)
python core/workflow_orchestrator.py --stage discord-video-mark          # Discord videos-n-streams 自荐采集 → YT表来源注册 + Notes 标注 (不写优先级)
python core/workflow_orchestrator.py --stage discord-video-mark --dry-run  # 试运行: 只打印发现的消息与链接, 不写飞书不记历史
python core/workflow_orchestrator.py --stage curated-cn-mark             # 中文精选表 → B站表来源注册 + 备注标注 (不写优先级)
python core/workflow_orchestrator.py --stage curated-cn-mark --dry-run   # 试运行: 只打印匹配情况, 不写飞书
```

> **注 (v5.0.0+)**: 脚本去重 (ScriptPrescreen) 通过 `core/prescreen.py --script` 调用，**不是** orchestrator 的 stage。

#### Stage 1.5 标记阶段说明 (v5.4.0)

| 阶段 | 脚本 | 数据源 | 匹配目标 | 写入动作 |
|------|------|--------|----------|----------|
| Discord 自荐标记 | `core/discord_video_mark.py` | Discord 频道 `<DISCORD_VIDEO_CHANNEL_ID>` (videos-n-streams) 最近 7 天消息 | YT 表 `Video ID` | 来源注册表登记 + Notes 追加"来源: Discord videos-n-streams 自荐"; 库中未命中的视频通过 YouTube Data API 补录入表。**不写 Human Prescreen** |
| 中文精选来源标记 | `core/curated_cn_mark.py` | 精选表 `tbl_CURATED` (链接/B站分享链接 提取BV, b23.tv短链自动解析, 失败按标题兜底) | B站表 `BV号` | 来源注册表登记 + 备注追加"来源: 中文精选表"。**不写 人工初筛** |

v5.4.0 起标记阶段只登记来源（写入 `data/source_registry.json`），视频以普通身份参与机器初筛 → ScriptDedup → AI 审核全流程：
- **评分加权**：`video_processor.py` 评分时按注册表给来源视频 `High Priority` (+10) 权重；
- **优先级翻转**：AI 审核完成后 `workflow_orchestrator._flip_source_priority` 将真正完成审核的来源视频翻转为 `高优先级/High Priority`；
- **复活补跑**：最终 AI 完成数 < 8 时，`_resurrect_source_videos` 复活被 ScriptDedup 淘汰的来源视频（按 D1+D2 降序）补跑 AI，通过后同样翻转。

两个阶段均支持 `--dry-run`；也可直接独立运行脚本：`python core/discord_video_mark.py --dry-run` / `python core/curated_cn_mark.py --dry-run`。
Discord 阶段需要代理 (自动检测 17890/17891/4780 http 与 37890 socks5) 和 `DISCORD_USER_TOKEN`（项目根目录 .env，缺省回退 add-on/.env）；命中消息记入 `data/discord_history.json` 增量去重。

### 方式三：完全自动化（定时任务/Cron）

```bash
# 推荐：完整自动化流程（Cookie 检测→Fetcher→Prescreen→ScriptDedup→human-prescreen）
run.bat
# 或
python core/run_workflow.py

# 续跑：采集/初筛/去重已完成，从 human-prescreen 阶段继续（字幕+AI审核+飞书同步）
python core/workflow_orchestrator.py --stage human-prescreen --auto
```

> **注意 (v5.0.0+)**: `python core/workflow_orchestrator.py --full --auto` **不是**完整自动化流程——
> 它缺少 ScriptPrescreen 脚本去重步骤，且在没有人工标记（Selected/优先级）的视频时会停在
> **"没有找到优先级视频"**。完全自动化请使用 `run.bat` / `python core/run_workflow.py`；
> 续跑请使用 `python core/workflow_orchestrator.py --stage human-prescreen --auto`。

### 方式四：回滚与备份管理

系统在每次数据获取前**自动备份** Excel 文件，也可手动操作：

```bash
# 查看所有可用备份
python tools/rollback_manager.py --list

# 手动创建备份
python tools/rollback_manager.py --backup

# 一键恢复到最新备份
python tools/rollback_manager.py --restore-latest

# 恢复到指定备份
python tools/rollback_manager.py --restore Bilibili_local_db_v20260601_xxxxxx_backup.xlsx

# 清理旧备份（默认保留最近3个）
python tools/rollback_manager.py --cleanup

# 自定义保留数量
python tools/rollback_manager.py --cleanup --keep 5
```

**备份存储位置**: `.backups/` 目录（项目根目录下）

**备份命名规则**: `{文件名}_v{时间戳}_backup.xlsx`

---

## 工作流详解

### 完整流程图

```
[启动] run.bat → python core/run_workflow.py   (v5.0.0+ 唯一全流程入口)
     |
     v
[Cookie 检测] <--- tools/check_cookies.py (v5.3.1+, 失效即中止并提示更新)
     |
     v
[阶段1: 视频采集] <--- core/bilibili_youtube_once_for_all_fetcher.py
     |
     +---> Bilibili API 抓取
     +---> YouTube Playwright 抓取
     |
     v
[阶段2: 机器初筛] <--- core/prescreen.py --platform bilibili / youtube
     |
     +---> D1 时长筛选 (>3分钟)
     +---> D2 内容质量评估
     |
     v
[阶段2.5: ScriptPrescreen 脚本去重] <--- core/prescreen.py --script (v5.0.0+)
     |
     +---> 只处理: Prescreen=Passed 且 Human=空 且 当周新增
     +---> 同一 up/youtuber 多视频只留最新
     +---> 去重后入选不足8条时, 从丢弃中按发布日期最新优先补充
     +---> 自动写回 Human Prescreen = Selected / Rejected + Notes
     |
     v
[阶段3: AI审核+评分] <--- core/workflow_orchestrator.py --stage human-prescreen (Bili → YT, 可加 --auto)
     |
     +---> 字幕获取+AI内容安全检测 (video_processor.py / platforms/)
     +---> 综合评分 D1~D4 + M1 + 优先级加权 (scoring.py)
     +---> 飞书同步: 写入评分/审核结论/状态 (feishu_sync.py)
     |
     v
[人工最后审核] 打开飞书查看 AI 结果 (workflow 完成提示: open Feishu to review AI results)
```

### 人工初筛决策矩阵

> **v5.0.0+ 重要变更**: 人工初筛已由 **ScriptPrescreen 脚本自动去重**替代（阶段2.5），不再需要人工交互标记。
> 脚本只处理 **Prescreen=Passed 且 Human=空 且 当周新增** 的记录，自动写回 `人工初筛`/`Human Prescreen` 与 `备注`/`Notes`：
> 同一 up/youtuber 多视频只留最新一条；去重后入选不足8条时，从丢弃中按发布日期最新优先补充。
> 保留 → `入选`/`Selected`；丢弃 → `拒绝`/`Rejected`。以下矩阵说明字段语义与评分逻辑（保留作参考）。

阶段3的"人工初筛"涉及两个状态的交叉决策：**机器初筛结果** (`初筛状态`/`Prescreen Status`) 与 **人工初筛标记** (`人工初筛`/`Human Prescreen`)。

#### 人工初筛选项定义

| 选项 | B站字段值 | YouTube字段值 | 分类 | 优先级加权 | 含义 |
|------|----------|-------------|------|-----------|------|
| 高优先级 | `高优先级` | `High Priority` | **优先级** | +10.0 | 机器初筛通过的优先处理视频 |
| 中优先级 | `中优先级` | `Medium Priority` | **优先级** | +5.0 | 同上 |
| 低优先级 | `低优先级` | `Low Priority` | **优先级** | +2.0 | 同上 |
| 入选 | `入选` | `Selected` | **强制入选** | +0.0 | 强制进入下一阶段，不加优先级分 |
| 拒绝 | `拒绝` | `Rejected` / `拒绝` | **拒绝** | 不处理 | 跳过该视频 |

> 注意："入选" / "Selected" **不是优先级**，而是一个独立的"强制入选"类别。它确保视频无论机器初筛结果如何都会被送入 AI审核+评分阶段，但**不获得优先级加分**。

#### 强行入选 (Forced Include)

**强行入选** 是指视频被送入 AI审核+评分阶段处理，但其进入方式不符合常规的"机器初筛通过 + 人工标记优先级"路径。以下两种情况都会产生强行入选：

1. **标记为"入选"/"Selected"** — 无论机器初筛是否通过，该视频强制进入 AI审核+评分阶段
2. **标记为优先级但机器初筛未通过** — 人工标记了"高/中/低优先级"，但机器初筛状态不是"通过"/"Passed"

#### 交叉决策矩阵

| 机器初筛结果 (`初筛状态`) | 人工初筛标记 (`人工初筛`) | 分类结果 | 说明 |
|---|---|---|---|
| `通过` / `Passed` | `高/中/低优先级` / `High/Medium/Low Priority` | **优先级视频** | 正常路径，进入 AI审核+评分阶段，按对应值加权 |
| `通过` / `Passed` | `入选` / `Selected` | **强行入选** | 强制进入 AI审核+评分阶段，不加权 |
| `通过` / `Passed` | `拒绝` / `Rejected` | **跳过** | 不进入 AI审核+评分阶段 |
| `通过` / `Passed` | (空) | **待标记** | 暂不处理，等待标记（v5.0.0+ 由 ScriptPrescreen 自动标记） |
| 非`通过` 或 空 | `高/中/低优先级` | **强行入选** | 人工绕过机器初筛，进入 AI审核+评分阶段但按对应值加权 |
| 非`通过` 或 空 | `入选` / `Selected` | **强行入选** | 人工绕过机器初筛，进入 AI审核+评分阶段但不加权 |
| 非`通过` 或 空 | `拒绝` / `Rejected` | **跳过** | 不进入 AI审核+评分阶段 |
| 非`通过` 或 空 | (空) | **跳过** | 两筛皆无标记，不处理 |

#### 优先级 vs 强制入选的评分差异

```
优先级视频  → 综合评分 = 内容分 × M1 + 优先级加权(+10/+5/+2)
强制入选    → 综合评分 = 内容分 × M1 + 0（不加优先级分）
```

> 强入选的视频虽然进入 AI审核+评分阶段参与评分，但因为未满足"机器初筛通过 + 人工标记优先级"的正常路径，在最终排名时不享受优先级加分。

### 关键特性说明

#### 0. 本地 Excel 数据库 (SSOT)

> **重要**: 所有脚本必须使用 `config.py` 中定义的路径常量，**禁止硬编码**！

| 平台 | 路径常量 | 实际位置 | 说明 |
|------|----------|----------|------|
| **Bilibili** | `BILIBILI_EXCEL_PATH` | `Bilibili_local_db.xlsx`（项目根目录） | B站视频数据库 |
| **YouTube** | `YOUTUBE_EXCEL_PATH` | `YouTube_local_db.xlsx`（项目根目录） | YouTube 视频数据库 |

**为什么放在项目根目录？**
- 与代码同目录，便于 Git 管理
- 避免与其他归档文件混淆
- 统一的工作目录结构

#### 1. 断点续传机制

系统会在以下位置保存进度：
- `data/.prescreen_state.json` - Bilibili 初筛进度
- `.yt_prescreen_state.json` - YouTube 初筛进度（项目根目录下）
- `data/scheduler_state.json` - 调度器状态
- `data/.feishu_sync_log.json` - 飞书同步日志

中断后重新运行会**自动跳过已完成**的视频。

#### 2. 增量初筛模式 (v3.2.0)

YouTube 初筛已升级为**增量模式**，大幅提升性能：

| 模式 | 处理范围 | 性能 | 适用场景 |
|------|----------|------|----------|
| **增量模式（默认）** | 仅新增记录 | ~30秒 | 日常运行 |
| **全量模式** | 所有未通过记录 | ~5分钟 | 首次运行/重跑 |

**工作原理**:
```
第1次运行: 处理 190 条新增 → 保存进度 (日期:2026-06-01, 序号:6028)
第2次运行: 检测到 0 条新数据 → 跳过，10秒完成
有新数据时: 只处理日期 > 上次日期 或 序号 > 上次序号的记录
```

**强制全量运行**:
```bash
python core/prescreen_youtube.py --full
```

#### 3. AI 模型自动切换

当主模型失败时，系统会按优先级尝试备用模型：

```python
# .env 配置示例
SILICONFLOW_MODEL_HEAVY=Pro/deepseek-ai/DeepSeek-V3.2,Pro/moonshotai/Kimi-K2.6,...
```

切换逻辑详见 [ai_client.py](core/ai_client.py)。

#### 4. YouTube 降频策略

内置 `youtube_rate_limiter` 模块，自动控制请求频率：
- 基础间隔：2-5秒随机延迟
- 批量处理：每10个视频暂停10-30秒
- 失败退避：指数级重试间隔（最多3次）

**无需额外配置代理轮换**，降频策略可应对绝大多数速率限制场景。

#### 5. 代理诊断工具（运维用）

如遇持续速率限制，可使用独立诊断工具：

```bash
python tools/smart_proxy_rotator.py --test
python tools/smart_proxy_rotator.py --monitor
```

详见 [smart_proxy_rotator.py](./tools/smart_proxy_rotator.py)。

#### 6. 飞书写入安全性 (v4.4.0)

系统已修复所有飞书写入错误，确保类型安全：

| 错误代码 | 原因 | 修复方案 |
|----------|------|----------|
| `NumberFieldConvFail` | 尝试写入 AUTO_NUMBER 字段 | 跳过 ft==7 (自动编号) 字段 |
| `DatetimeFieldConvFail` | 空值或格式异常的日期 | 增强 pd.isna() 检查 + 多种格式支持 |
| `TextFieldConvFail` | 向 select 字段写入非法值 | 跳过或转换 select 类型字段 |

**安全推送工具**（调试用）:
```bash
# 强制推送 Bilibili 数据到飞书
python safe_push_to_feishu.py --bili

# 强制推送 YouTube 数据到飞书
python safe_push_youtube.py

# 单条记录测试（调试用）
python test_single_record.py
```

#### 7. 飞书字段类型参考表 (v4.5.0)

> **血泪教训总结**: 以下字段类型来自飞书 API 实际返回值，任何写入飞书的代码**必须**严格遵循此表，否则会触发静默失败或 API 报错。

##### YouTube 表格字段类型

| 字段名 | 类型ID | 类型名称 | 写入规则 | ⚠️ 易错点 |
|--------|--------|----------|----------|-----------|
| **Channel** | **3** | **select(单选)** | `batch_create`: 传选项名(`"ChannelA"`) | `batch_update`: 传选项名或ID | **禁止传 option ID 给 batch_create！** 曾导致整批记录显示乱码 |
| **Language** | **3** | **select(单选)** | `batch_create`: 传选项名(`"en"`) | `batch_update`: 传选项名或ID | 同上，传 `"optXXXXXXXX"` 给 batch_create → UI显示 `optXXXXXXXX` |
| **Rating** | 3 | select(单选) | 传选项名 (`A`/`B`/`C`/`D`/`E`) | — |
| **Prescreen Status** | 3 | select(单选) | 传选项名 (`Passed`/`Rejected`/`Pending`) | — |
| **Human Prescreen** | 3 | select(单选) | 传选项名 (`High Priority`/`Selected`/`Rejected`) | — |
| **Subtitle Status** | 3 | select(单选) | 传选项名 (`success`/`failed`/`pending`) | — |
| **AI Review Result** | 3 | select(单选) | 传选项名 (`safe`/`unsafe`/`pending`) | — |
| **AI审核结果** | 3 | select(单选) | 传选项名 (`通过`/`不通过`/`待审核`) | B站表专用 |
| **初筛状态** | 3 | select(单选) | 传选项名 (`通过`/`拒绝`/`待处理`) | B站表专用 |
| **人工确认** / `Manual Confirm` | 4 | checkbox(复选框) | 传 `true`/`false` | — |
| **Published Date** | **5** | **datetime(日期时间)** | 传毫秒时间戳 (int, ×1000) | 唯一的真正日期字段 |
| **Number** / `序号` | 7 / 1005 | auto_number | **不可写入!** 跳过 | 写入必报 NumberFieldConvFail |
| **Title** | 1 | text(文本) | 直接传字符串 | 最大2000字符 |
| **Link** | 1 | text(文本) | 直接传字符串 | — |
| **Video ID** | 1 | text(文本) | 直接传字符串 | — |
| **Channel ID** | 1 | text(文本) | 直接传字符串 | — |
| **Views** | **1** | **text(文本)** | **必须转字符串!** `str(int(val))` | **不是number类型!** 曾误当数字写入 |
| **Likes** | **1** | **text(文本)** | **必须转字符串!** | 同上 |
| **Comments** | **1** | **text(文本)** | **必须转字符串!** | 同上 |
| **Duration** | 1 | text(文本) | 直接传字符串 | — |
| **Duration (Min)** | 1 | text(文本) | 直接传字符串 | — |
| **Engagement Rate** | 1 | text(文本) | 直接传字符串 | — |
| **Published At** | **1** | **text(文本)** | 直接传字符串 | **不是datetime类型!** 名字有误导性 |
| **Tags** | 1 | text(文本) | 直接传字符串 | — |
| **Description** | 1 | text(文本) | 直接传字符串 | 最大2000字符 |
| **Live Recording** | 1 | text(文本) | 直接传字符串 | — |
| **Has Captions** | 1 | text(文本) | 直接传字符串 | — |
| **Notes** / `备注` | 1 | text(文本) | 直接传字符串 | — |
| **D1 Duration** | 2 | number(数字) | 传 int 或 float | NaN → 0 |
| **D2 Timeliness** | 2 | number(数字) | 传 int 或 float | — |
| **D3 Engagement** | 2 | number(数字) | 传 int 或 float | — |
| **D4 Appeal** | 2 | number(数字) | 传 int 或 float | — |
| **Total Score** / `综合评分` | 2 | number(数字) | 传 int 或 float | — |

##### 类型ID 速查表

| 类型ID | 名称 | `batch_create` 写入 | `batch_update` 写入 | 踩坑记录 |
|--------|------|---------------------|---------------------|----------|
| **1** | text | `"value"` | `"value"` | 最安全，直接写字符串 |
| **2** | number | `42` / `3.14` | `42` / `3.14` | NaN/None → 0 |
| **3** | **select** | **option NAME** (如 `"en"`)| option NAME 或 ID | **最高危!** `batch_create` 传ID → 创建同名乱码选项 |
| 4 | checkbox | `true` / `false` | `true` / `false` | — |
| **5** | datetime | `1717200000000` (ms戳) | 同左 | 空值 → 不传或 `""` |
| **7** | auto_number | **不可写!** | **不可写!** | 写入 → NumberFieldConvFail |
| 15 | formula | 不可写 | 不可写 | 自动计算 |
| 1005 | attachment | 不可写 | 不可写 | 文件附件 |

> **🔴 核心警示 - Select 字段的双面人行为**:
>
> | API | select 字段行为 | 示例 |
> |-----|----------------|------|
> | **`batch_create`** | **只接受 option NAME** | `{"Language": "en"}` ✅ |
> | | 传 option ID (`optXXXXXXXX`) → 飞书会当作**新选项名**创建 | `{"Language": "optXXXXXXXX"}` ❌ → UI 显示 `optXXXXXXXX` |
> | **`batch_update`** | 接受 option **NAME 或 ID** | `{"Language": "en"}` ✅ / `{"Language": "optXXXXXXXX"}` ✅ |
>
> **结论: 永远传 option NAME，不传 option ID。** 这样两种 API 都兼容，且不会产生乱码。

##### 历史踩坑记录

| 时间 | 问题 | 影响 | 根因 | 修复 |
|------|------|------|------|------|
| v4.5.1 | **Language/Channel 显示为 `optXXXXXXXX` 等乱码** | 972条记录 select 字段全是 option ID 字符串 | `batch_create` 对 select 字段传了 option ID 而非 NAME | `batch_update` 改回 option NAME；明确规范：永远传 NAME |
| v4.4.0 | Channel 全部为空 (222条) | Number>=6000记录无频道信息 | safe_push_youtube 将 select 标记为 skip | 批量通过 Channel ID 反查 + YouTube API 补全 |
| v4.4.0 | Language 缺失 (1352条, 36.7%) | 近1/3记录无语言标识 | 同上，skip 了 select 字段 | fix_yt_language.py 批量设为 'en' |
| v4.3.0 | Views/Likes 写入报错 | YouTube 数据无法写入 | 当成 number 类型实际是 text | 改为 str(int(val)) 转换 |
| v4.3.0 | Published At 写入报错 | 日期数据丢失 | 当成 datetime 实际是 text | 改为字符串直传 |
| v4.2.0 | 序号写入报错 | B站数据全部写入失败 | 尝试写 AUTO_NUMBER 字段 | 跳过 ft==7 |

---

## 项目结构

```
video-censor/
├── core/                          # 核心业务逻辑
│   ├── run_workflow.py            # 端到端工作流执行器（v5.0.0，run.bat 实际入口）
│   ├── workflow_orchestrator.py   # 工作流调度器（stage 执行）
│   ├── run_anchor.py              # [v5.4.2] 运行锚点：用上次运行起止时刻划定候选池边界
│   ├── source_registry.py         # [v5.4.0] 视频来源注册表（SSOT，来源推荐/复活判定）
│   ├── feishu_fields.py           # 飞书字段名+字段值 SSOT（唯一数据源）
│   ├── prescreen.py              # B站初筛
│   ├── prescreen_youtube.py      # YouTube初筛
│   ├── feishu_sync.py            # 飞书同步
│   ├── scoring.py                # 评分算法
│   ├── curated_cn_mark.py        # 中文精选来源标记
│   ├── discord_video_mark.py     # Discord 视频自荐标记
│   ├── spider_discord.py         # Discord 论坛频道采集
│   ├── language_classifier.py    # 多语言字幕分类
│   ├── config.py                 # 配置管理
│   ├── scheduler.py              # 任务调度器
│   ├── console_utils.py          # 控制台工具
│   ├── youtube_rate_limiter.py   # YouTube降频模块
│   ├── ai_client.py              # AI模型调用封装
│   ├── video_processor.py        # 视频处理编排
│   ├── reporter.py               # 报告生成器
│   └── version.py                # 版本号定义
│
├── platforms/                     # 平台适配层
│   ├── bilibili_client.py        # B站API客户端
│   ├── youtube_client.py         # YouTube客户端
│   └── video_parser.py           # URL解析器
│
├── docs/                          # 项目文档
│   ├── FEISHU_CLI_GUIDE.md       # 飞书 CLI 使用指南
│   └── (更多文档...)
│
├── tools/                         # 独立工具脚本
│   ├── rollback_manager.py       # [v2.0] 增强版回滚管理器（自动备份+一键恢复）
│   ├── rollback_fetcher.py       # 抓取回滚工具（旧版，已废弃）
│   ├── smart_proxy_rotator.py    # 代理诊断与监控
│   ├── sync_report_to_feishu.py   # 报告同步到飞书
│   ├── retry_failed_youtube_v2.py# YouTube失败重试
│   ├── refresh_cookies.py        # Cookie刷新工具
│   └── yt_enrich.py              # YouTube数据回填工具
│
├── .backups/                     # Excel 自动备份目录（运行时生成）
│   ├── Bilibili_local_db_v*_backup.xlsx
│   ├── YouTube_local_db_v*_backup.xlsx
│   └── .metadata.json            # 备份元数据
│
├── data/                          # 运行时数据
│   ├── .prescreen_state.json     # 初筛状态
│   ├── scheduler_state.json      # 调度状态
│   ├── selection_history.json    # 入选历史
│   ├── Bilibili_*.xlsx           # B站本地数据库
│   └── YouTube_*.xlsx            # YouTube本地数据库
│
├── archives/                      # 历史归档（可清理）
│   ├── deprecated_scripts/       # 归档的一次性脚本
│   └── result_logs/              # 历史结果
│
├── docs/                          # 文档
│   ├── README.md                 # 项目说明（本文件）
│   ├── Handoff.md                # AI交接文档
│   ├── FILE_ANALYSIS.md          # 文件分析报告
│   ├── WORKFLOW_PLAN.md          # 工作流设计
│   ├── REFACTOR_PLAN.md          # 重构计划
│   └── LESSONS.md                # 经验教训库
│
├── main.py                       # 主程序入口
├── run_bili_ai.py                # B站AI审核独立入口
├── sync_pending_results.py       # 待同步结果批量同步
├── sync_youtube_stats.py         # YouTube统计数据同步
├── patch_feishu_from_report.py   # 从报告补录飞书数据
├── run.bat                       # Windows启动脚本
├── run_from_prescreen.bat        # 从初筛开始的快捷方式
├── .env                          # 环境变量配置（含敏感信息！）
├── config.yaml                   # Clash代理配置
├── .gitignore                    # Git忽略规则
├── bilibili_cookies.json         # B站Cookie备份
└── youtube_cookies.json          # YouTube Cookie备份
```

---

## 评分体系详解 (v3.9.0)

### 双分制设计

飞书表格中同时展示两个分数，职责分离：

| 飞书字段 | 含义 | 数据来源 | 用途 |
|----------|------|----------|------|
| B站: `综合评分` / YouTube: `Total Score` | **排序评分**（含优先级加权） | 动态维度平均 × M1 + 优先级加成 | 决定排名先后 |
| B站: `D4吸引力分` / YouTube: `D4 Appeal` | **内容评分**（纯内容质量） | AI审查后的吸引力得分(0~10) | 衡量视频本身价值 |

### 评分维度

| 维度 | 全称 | 说明 | 分值范围 |
|------|------|------|----------|
| **D1** | 时长得分 | 视频时长是否在最佳区间(10~20分钟) | 0~10 |
| **D2** | 时效得分 | 发布时间新鲜度(3天内满分，21天外排除) | 0~10 |
| **D3** | 互动得分 | 评论+点赞的综合互动率（YouTube无弹幕） | 0~10 |
| **D4** | AI吸引力得分 | AI看完字幕后的内容质量判断(**=内容评分**) | 0~10 |
| **M1** | 新人修正系数 | 新创作者加分/老创作者减分(防止马太效应) | 0.9x~1.2x |

### 优先级加权

人工初筛时标记的优先级会直接加到排序评分中：

| 人工初筛标签 | 分类 | 加分值 | 来源 |
|-----------|------|--------|------|
| 高优先级 / High Priority | **优先级** | **+10.0** | 创作者表自动识别 或 人工手动标注 |
| 中优先级 / Medium Priority | **优先级** | **+5.0** | 人工标注 |
| 低优先级 / Low Priority | **优先级** | **+2.0** | 人工标注 |
| 入选 / Selected | **强制入选** | **+0.0** | 人工标注（强制进入，不加分） |
| normal / 未标注 | — | **+0.0** | 默认 |

### 计算公式

```
B站视频:
  内容原始分 = (D1 + D2 + D3 + D4) / 4              (四维平均)
  排序评分 = 内容原始分 × M1 + 优先级加成             写入"综合评分"

YouTube视频 (v3.9.0升级):
  内容原始分 = 动态维度平均                          有数据的维度参与平均
               完整数据时: (D1+D2+D3+D4)/4           与B站一致
               缺失D2/D3时: (D1+D4)/2                 向后兼容旧视频
  排序评分 = 内容原始分 × M1 + 优先级加成             写入"Total Score"
```

> **v3.9.0关键变更**: YouTube不再跳过D2/D3。评分前自动从飞书回填Views/Likes/Comments/Published At等统计数据。两平台在数据完整时使用完全相同的四维公式。

### 评级标准

| 等级 | 排序评分范围 | 含义 | 建议 |
|------|-------------|------|------|
| **A** | >= 10.0 | 优质 | 优先处理 |
| **B** | >= 8.0 | 良好 | 推荐 |
| **C** | >= 6.0 | 一般 | 可考虑 |
| **D** | >= 4.0 | 较差 | 不推荐 |
| **E** | < 4.0 | 极差 | 拒绝 |

### 飞书字段映射

| 数据含义 | B站表字段名 | YouTube表字段名 |
|----------|------------|----------------|
| 排序评分 | `综合评分` | `Total Score` |
| 内容评分(D4) | `D4吸引力分` | `D4 Appeal` |
| 时长分 | `D1时长分` | `D1 Duration` |
| 时效分 | `D2时效分` | `D2 Timeliness` |
| 互动分 | `D3互动分` | `D3 Engagement` |
| 等级 | `评分等级`(A~E) | `Rating`(A~E) |
| 优先级 | `人工初筛` | `Human Prescreen` |

### 飞书字段规范 (v4.3.0)

> **核心原则**: 所有飞书字段名和字段值的定义集中在 [core/feishu_fields.py](./core/feishu_fields.py) 中，作为唯一数据源（SSOT）。任何写入飞书的代码必须引用该模块的常量，禁止硬编码字段名或字段值。

#### 字段名语言规范

| 平台 | 字段名语言 | 字段值语言 | 示例 |
|------|----------|----------|------|
| **B站** | 中文 | 中文 | 字段名: `AI审核结果`，值: `通过`/`不通过` |
| **YouTube** | 英文 | 英文 | 字段名: `AI Review Result`，值: `safe`/`unsafe` |

#### 关键字段值对照表

| 数据含义 | B站字段值 | YouTube字段值 |
|----------|----------|-------------|
| 审核通过 | `通过` | `safe` |
| 审核不通过 | `不通过` | `unsafe` |
| 字幕获取成功 | `成功` | `success` |
| 字幕获取失败 | `失败` | `failed` |
| 初筛通过 | `通过` | `Passed` |
| 初筛拒绝 | `拒绝` | `Rejected` |
| 高优先级 | `高优先级` | `High Priority` |
| 中优先级 | `中优先级` | `Medium Priority` |
| 低优先级 | `低优先级` | `Low Priority` |
| 强制入选 | `入选` | `Selected` |

> **重要**: 飞书 select 字段不接受选项列表之外的值。如果写入的值不在选项中，API 不会报错，但值不会被更新（静默失败）。因此所有写入点必须通过 `feishu_fields.py` 的映射函数（如 `map_censor_result_to_bili()`、`map_subtitle_status_to_yt()` 等）确保值合法。

---

## 常见问题排查

### Q1: YouTube 提示 "429 Too Many Requests" 或 IP 封禁

**原因**: 请求频率过高触发 YouTube 速率限制

**解决方案**:
1. 系统已内置降频模块，通常无需干预
2. 如仍受限，切换代理节点后等待 24 小时
3. 运行诊断工具检查节点健康度:
   ```bash
   python smart_proxy_rotator.py --test
   ```

### Q2: Bilibili Cookie 过期

**症状**: 无法抓取 B站视频，提示登录错误

**解决方案**:
1. 登录 https://www.bilibili.com
2. 按 F12 打开开发者工具 → Application → Cookies
3. 复制完整 Cookie 到 `.env` 的 `BILIBILI_COOKIE` 字段
4. 或运行时按提示更新 Cookie

### Q3: 飞书同步失败 "FieldNameNotFound"

**原因**: 飞书表格字段名与代码不匹配

**解决方案**:
1. 检查 `core/feishu_sync.py` 中的字段映射
2. 确保飞书表格列名与代码一致（注意空格和大小写）
3. 参考 [.env.example](.env.example) 了解正确的字段名格式

### Q4: AI 模型返回错误或超时

**原因**: API 限流或模型不可用

**解决方案**:
1. 系统**自动切换**到下一个备用模型
2. 检查 `.env` 中的 `SILICONFLOW_MODEL_*` 配置
3. 确认 API Key 有效且余额充足
4. 查看 `data/ai_parse_errors.log` 获取详细错误信息

### Q5: 如何只处理某个平台的视频？

**方法**:
```bash
# 方式1: 使用命令行参数
run_from_prescreen.bat --platform youtube

# 方式2: 在 video_urls.txt 中只保留目标平台 URL
```

---

## 开发指南

### 添加新的审核维度

1. 在 `core/scoring.py` 添加新维度的计算函数
2. 在 `core/video_processor.py` 调用该函数
3. 调整权重分配（确保总和为100%）
4. 在 `core/feishu_fields.py` 添加新字段定义
5. 更新飞书同步字段（如有需要）

### 添加新平台支持

1. 在 `platforms/` 创建 `xxx_client.py`
2. 实现 `fetch_subtitle(video_id)` 接口
3. 在 `core/prescreen_xxx.py` 添加初筛逻辑
4. 在 `core/workflow_orchestrator.py` 注册新平台
5. 更新 `.env` 和飞书配置

### 代码规范

- 所有文件使用 **UTF-8 编码**
- 函数添加**类型注解** (Type Hints)
- 遵循 **KISS/YAGNI** 原则，避免过度设计
- 异常捕获使用**具体类型**，禁止裸 `except:`
- 敏感信息通过**环境变量**传递，禁止硬编码

---

## 版本历史

### v5.3.9 (2026-09-07) - ScriptDedup 恢复按语言分组去重
- 🔴 **Bug 修复**: v5.3.8 误将 ScriptDedup 去重从"按语言分组"改为"全局按频道"，破坏了语言维度语义 —— 恢复按语言分组去重
- ✅ **规则明确**: 去重按语言维度，每个语言组内每频道最多 1 条（同 UP 跨语言可各留 1 条，因不同语言面向不同受众）；该语言组内已标记 UP（自荐/精选）的其他候选排除
- ✅ **补足语义**: 语言组有效入选 < 8 时，从同频道多余视频按日期最新优先补充（允许同频道重复）
- 📌 **经验教训**: 去重分组维度（语言 vs 全局）是业务语义，修改前必须先与用户对齐，不能想当然"全局去重更严格就是更好"

### v5.3.8 (2026-09-07) - Discord 自荐标记修正 + ScriptDedup 全局去重
- 🔴 **Bug 修复 1**: Discord videos-n-streams 自荐视频被标记为 `Selected`（零加权"强行入选"），与"自荐优先"意图不符 —— 修正为 `High Priority` (+10 加权)，自荐视频现正确体现为"优先级视频"
- 🔴 **Bug 修复 2**: ScriptDedup 按语言分组去重，同 UP 跨语言组各留 1 条造成同 UP 重复入选；且已标记 UP 的其他视频仍被脚本选入 —— 改为全局按 UP 去重，已标记 UP 的未标记视频直接排除
- ✅ **retry-youtube-failed 过滤器扩大**：从"仅 Selected"扩大为全部正向标记（High/Medium/Low Priority + Selected），确保自荐失败视频可重试
- ✅ **数据修复**：本周 12 条自荐视频 Selected → High Priority，11 条有分数记录 Total Score +10 并升 Rating A（`tools/fix_discord_marks.py`，写入前重取字段类型校验）
- 📌 **经验教训**: ① "强行入选"(Selected) 与"优先级"(Priority) 是评分体系中两个不同维度，不能混用；② 去重必须在全局维度（跨语言组）进行，且已标记记录代表其 UP 已入选

### v5.3.7 (2026-09-07) - 语言分类多重兜底机制
- 🔴 **Bug 修复**: 德/俄/波兰语视频被 AI 误分类为英文 —— 字幕轨选择"取第一个手动轨不看语言"，德语视频挂英文轨时取到英文文本，AI 分类（原设计"AI 结果为准"）覆盖了声明语言代码
- ✅ **当日 9 条错配记录已纠正**（5 德文 + 4 俄文，含 1 条波兰文），按飞书 Language 代码字段仲裁
- ✅ **多重兜底第 1 层（源头）**：`youtube_client._ordered_transcripts()` 字幕轨优先匹配视频声明音频语言（preferred 匹配手动轨 > preferred 匹配自动轨 > 其余手动 > 其余自动）；`fetch_youtube_subtitle` / `fetch_and_process_single` 新增 `preferred_lang` 参数，orchestrator 重试探测路径同步传递
- ✅ **多重兜底第 2/3 层（仲裁）**：`language_classifier.arbitrate_language()` 多源仲裁 —— 视频声明语言(hint) + AI 文本分类 + 频道历史主导语言 三源投票（2:1 多数胜；全异时 频道历史 > hint > AI）
- ✅ **频道历史索引**：`main._get_channel_language_context()` 惰性构建（线程安全，进程内缓存一次）——Channel ID → 主导语言（历史 ≥3 条且单一语言占比 ≥80% 才有效，AI 分类结果优先、声明语言代码推断兜底）
- 📌 **经验教训**: ① 字幕轨文本语言 ≠ 视频内容语言，UP 主常为多语言受众挂英文轨；② "AI 结果为准"需多源仲裁约束，声明语言 + 频道历史是更稳定的先验信号；③ 频道语言高度稳定（少数双语频道除外），历史标记占比 ≥80% 才采信，避免少数派干扰

### v5.3.4 (2026-08-31) - 文档同步最新设计
- ✅ **全流程入口统一为 `run.bat` → `core/run_workflow.py`**：Cookie 检测 → Fetcher → Prescreen (Bili/YT) → ScriptDedup (Bili/YT) → orchestrator `--stage human-prescreen` (Bili → YT)，orchestrator 内部含 字幕+AI审核+飞书同步
- ✅ **人工初筛由 ScriptPrescreen 脚本自动去重替代 (v5.0.0+)**：只处理 Prescreen=Passed 且 Human=空 且当周新增，同 up 多视频只留最新（入选不足8条按日期最新优先补充），自动写回 Human Prescreen=Selected/Rejected + Notes
- ✅ **明确 `--full --auto` 非完整自动化**：缺 ScriptDedup 步骤，无人工标记时停在"没有找到优先级视频"；完全自动化用 run.bat / `python core/run_workflow.py`，续跑用 `--stage human-prescreen --auto`
- ✅ **工作流流程图更新**：新增 Stage 2.5 ScriptPrescreen，阶段3 改为 AI审核+评分，最终人工在飞书查看 AI 结果

### v4.5.1 (2026-06-01) - 修复 select 字段 batch_create 传 option ID 导致乱码的致命 Bug
- 🔴 **致命 Bug 修复**: `batch_create` 对 select 字段必须传 option **NAME** (如 `"en"`)，传 option **ID** (如 `"optXXXXXXXX"`) 会被当作新选项名创建，导致 UI 显示乱码
- ✅ **972 条记录修复**: 所有 Language (972条) 和 Channel (971条) 的乱码值被修正为可读的 option NAME
- ✅ **推送脚本修正**: `push_yt_0525plus_with_prescreen.py` 中 select 字段改为传 NAME
- ✅ **README 字段规范升级**: 类型ID速查表新增 `batch_create` vs `batch_update` 列，核心警示框强调 select 字段双面行为
- 📌 **铁律**: 永远传 option **NAME**，不要传 option **ID**（两 API 都兼容 NAME）
- ✅ **修复 Channel 100% 缺失 (222条)**: Number>=6000 的 YouTube 记录 Channel 全部为空，通过已有记录反查(181条) + YouTube API 反查(41条) 全部补全
- ✅ **修复 Language 36.7% 缺失 (1352条)**: 批量将空 Language 设为 `en`（英文），飞书表 3684 条现在 100% 有值
- ✅ **新增飞书字段类型参考表**: README 新增完整的 YouTube 表格字段类型映射（含类型ID、写入规则、易错点、历史踩坑记录）
- ✅ **新增诊断/修复工具链**: `check_yt_language.py` / `fix_yt_language.py` / `check_yt_high_number.py` / `fix_yt_6000_records.py` / `resolve_unknown_channels.py`
- ✅ **YouTube Channel ID 反查机制**: 支持从已有飞书记录/本地Excel/YouTube API 三级回退查找频道名

### v4.4.0 (2026-06-01) - 飞书写入修复 + 增量初筛 + 自动备份回滚
- ✅ **修复飞书 NumberFieldConvFail 错误**: 跳过 AUTO_NUMBER/FORMULA 字段，增强 NaN 和异常值处理
- ✅ **修复飞书 DatetimeFieldConvFail 错误**: 日期字段安全转换，支持 Timestamp/字符串/数值多种输入格式
- ✅ **修复飞书 TextFieldConvFail 错误**: YouTube select 类型字段（Channel/Language）正确跳过或转换
- ✅ **YouTube 初筛升级为增量模式 (v3.2.0)**: 仅处理新增记录，基于日期+序号智能过滤，性能提升 ~10x（从 ~5分钟 → ~30秒）
- ✅ **新增增强版回滚管理器 (v2.0)**: `tools/rollback_manager.py`，支持自动备份、一键恢复、备份列表查看、旧备份清理
- ✅ **数据获取前自动备份**: fetcher 运行时自动调用 `auto_backup()` 备份 Excel 到 `.backups/` 目录
- ✅ **统一 Excel 数据库路径**: config.py 新增 `BILIBILI_EXCEL_PATH` / `YOUTUBE_EXCEL_PATH` 常量，SSOT 管理
- ✅ **新增安全推送工具**: `safe_push_to_feishu.py` / `safe_push_youtube.py` 用于强制写入和调试

### v4.3.0 (2026-05-26) - 飞书字段SSOT + 字段值回归Bug修复 + 优先级权重统一
- **新增 feishu_fields.py SSOT**: 集中定义所有飞书字段名、字段值、映射函数，消除项目中散落的硬编码魔法字符串
- **修复字段值回归Bug**: B站"AI审核结果"字段被写入英文值"safe"而非中文"通过"，导致飞书select字段静默写入失败
- **修复接口迁移Bug**: run_bili_ai.py 期望 `load_videos_from_feishu` 返回 `(priority, forced)` 但实际返回 `(bili, youtube)`
- **统一优先级权重**: PRIORITY_WEIGHT 同时包含中英文键（"高优先级"/"High Priority"），修复YouTube标签权重为0的问题
- **新增 core/version.py**: 版本号集中管理，消除各文件硬编码版本号
- **标记 video_subtitle_censor7.py 为已废弃**: 功能已迁移至 video_processor.py

### v4.2.0 (2026-05-25) - 飞书同步修复 + 定义文档化
- ✅ **飞书搜索改用 bot token**：`get_record_by_field()` 从 lark-cli 改为 requests + tenant_access_token，彻底解决搜索限流 (800004135)
- ✅ **添加 record_id 缓存**：`update_video_record()` 缓存已查到的 record_id，避免重复搜索同一视频
- ✅ **新增人工初筛决策矩阵文档**：README 明确记录机器初筛 × 人工初筛的交叉决策规则
- ✅ **分类"入选"为"强制入选"**：优先级标签只含高/中/低，"入选"/"Selected" 独立为"强制入选"类别，不加分
- ✅ **定义"强行入选"概念**：明确两种强行入选场景

### v3.9.0 (2026-05-18) - 飞书字段名规范 + YouTube评分四维统一
- ✅ **YouTube评分公式升级为四维**：从`(D1+D4)/2`升级为动态维度平均，完整数据时`(D1+D2+D3+D4)/4`与B站一致
- ✅ **YouTube数据回填机制**：评分前自动从飞书读取Views/Likes/Comments/Published At/Channel ID等统计数据
- ✅ **修复YouTube中文字段名bug**：`video_subtitle_censor7.py` 的YouTube分支错误使用中文，已全部改为英文（Subtitle Status / D1 Duration / D2 Timeliness / D3 Engagement / D4 Appeal）
- ✅ **修复YouTube审核结果值不一致**：`rejected` 统一为 `unsafe`
- ✅ **修复B站D分读取字段名**：`feishu_sync.py` 中 `"D1得分"` → `"D1时长分"`
- 📌 **飞书字段命名铁律**：B站全中文、YouTube全英文

### v3.8.0 (2026-05-18) - 评分双分制 + 优先级加权修复
- ✅ **修复优先级加权未写入飞书bug**：`综合评分`/`Total Score` 现在包含优先级加成（高+10/中+5/低+2）
- ✅ **引入评分双分制**：排序评分（含所有维度+优先级）与内容评分（D4纯AI判断）分离
- ✅ **YouTube字段名统一**：`sync_report_to_feishu.py` 升级为新格式（D1 Duration/D2 Timeliness/D3 Engagement/D4 Appeal）

### v3.7.0 (2026-05-11) - 审核结果规范化
- ✅ **修复B站字段语言不一致**：AI审核结果统一使用中文（通过/不通过）
- ✅ **消除pending状态**：所有视频必须有明确的审核结论（基于评分兜底）
- ✅ **B站字幕状态中文化**：字幕获取状态改为"成功"/"失败"
- ✅ **飞书API分页容错增强**：自动重试+跳过失败页继续获取（v3.6.0）
- ✅ **Cookie检查提示**：bat启动前提醒用户确认Cookies有效性
- ✅ **新增补录工具**：`patch_feishu_from_report.py` 从报告直接同步到飞书

### v4.0 (2026-05-07)
- ✅ 新增平台/阶段参数化 (`--platform`, `--stage`)
- ✅ 集成 YouTube 降频模块 (`youtube_rate_limiter`)
- ✅ 重构代理工具为独立诊断工具 (`smart_proxy_rotator`)
- ✅ 增强断点续传和错误恢复机制
- ✅ 清理冗余文件和归档旧代码

### v3.5 (2026-05-06)
- ✅ 人工初筛交互菜单（撤销+重跑）
- ✅ AI 模型自动切换和回退链
- ✅ 飞书字段验证和错误诊断

### v3.4 (2026-05-05)
- ✅ 模块化重构（core/, platforms/ 分层）
- ✅ 飞书多维表格同步
- ✅ 断点续传机制

### v3.0 (2026-04-20)
- ✅ 初始版本，基础功能实现

---

## 许可证

MIT License

---

## 贡献指南

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

---

## 联系方式

- **项目地址**: [GitHub](https://github.com/MaxLinkerAlpha/video_censor_public)
- **问题反馈**: [Issues](https://github.com/MaxLinkerAlpha/video_censor_public/issues)

---

## 致谢

- [硅基流动](https://siliconflow.cn) - AI 模型 API 服务
- [飞书](https://feishu.cn) - 多维表格数据存储
- [Playwright](https://playwright.dev) - YouTube 字幕获取引擎
