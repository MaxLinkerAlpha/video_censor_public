# 项目文件分析报告

> **生成时间**: 2026-05-07 (v4.0 整理后更新)
> **项目路径**: `refactor/`
> **文件总数**: ~40 个（不含 `__pycache__` 和被 .gitignore 排除的文件）

---

> ## ⚠️ 已过时 (v5.3.4, 2026-08-31)
>
> 本文档生成于 v4.0（2026-05-07），其中的架构描述（如 `video_subtitle_censor7.py` 为"字幕+AI审核核心"、
> `workflow_orchestrator.py` 为唯一入口、core/ 仅 16 个 py 等）已不反映当前设计。
> **当前设计以 [README.md](../README.md) v5.3.4 与 `core/run_workflow.py` 为准**：
> - 全流程入口：`run.bat` → `python core/run_workflow.py`（含 Cookie 检测 → Fetcher → Prescreen → ScriptDedup → orchestrator human-prescreen）
> - 人工初筛已由 **ScriptPrescreen 脚本自动去重**替代（阶段2.5，`core/prescreen.py --script`）
> - `_deprecated_video_subtitle_censor7.py` 已废弃，功能迁移至 `video_processor.py` + `platforms/`
>
> 本文档仅保留作历史参考，文件清单与清理建议请以实际目录为准。

---

## 一、总体统计

| 分类 | 数量 | 总大小(约) | 必须保留 | 可归档 | 建议删除 |
|------|------|-----------|---------|--------|---------|
| 核心代码 (core/) | 16 py | ~120 KB | 16 | - | - |
| 平台适配 (platforms/) | 4 py | ~28 KB | 4 | - | - |
| 工具脚本 (tools/) | 4 py | ~45 KB | 4 | - | - |
| 运行时数据 (data/) | 7+ | ~355 KB | 6 | 1+ | - |
| 历史归档 (archives/) | ~10 | ~100 KB | - | 10 | - |
| 文档 (docs/) | 6 md | ~80 KB | 6 | - | - |
| 配置/其他 | ~5 | ~20 KB | 4 | 1 | - |

---

## 二、核心代码 (core/) — 全部必须保留

| 文件 | 大小(约) | 用途 | 被谁引用 |
|------|----------|------|----------|
| `workflow_orchestrator.py` | 14 KB | 工作流调度器 v4.0 | main.py, run.bat |
| `bilibili_youtube_once_for_all_fetcher.py` | 16 KB | B站/YouTube 抓取器 | orchestrator |
| `video_subtitle_censor7.py` | 15 KB | 字幕获取+AI审核核心 | video_processor |
| `prescreen.py` | 11 KB | B站初筛 (D1/D2) | orchestrator |
| `prescreen_youtube.py` | 8 KB | YouTube 初筛 | orchestrator |
| `feishu_sync.py` | 8 KB | 飞书多维表格同步 | 几乎所有模块 |
| `scheduler.py` | 10 KB | 定时任务调度器 | main.py |
| `scoring.py` | 6 KB | D1-D4 + M1 评分算法 | prescreen, video_processor |
| `config.py` | 5 KB | 配置管理 (.env 解析) | 几乎所有模块 |
| `console_utils.py` | 4 KB | 控制台工具库 (代理检测) | fetcher, 多处 |
| `youtube_rate_limiter.py` | 5 KB | YouTube降频模块 (v4.0新增) | video_subtitle_censor7 |
| `ai_client.py` | 8 KB | AI 模型调用封装 (硅基流动) | video_subtitle_censor7 |
| `video_processor.py` | 10 KB | 视频处理编排 (字幕+评分) | main.py, orchestrator |
| `reporter.py` | 6 KB | 报告生成器 | video_processor |
| `__init__.py` | <1 KB | 包初始化 | - |
| `CHANGELOG.md` | 3 KB | 版本变更记录 | 文档用途 |

---

## 三、平台适配层 (platforms/) — 全部必须保留

| 文件 | 大小 | 用途 | 被谁引用 |
|------|------|------|----------|
| `bilibili_client.py` | 9 KB | B站 API 客户端 (字幕/视频信息) | video_subtitle_censor7.py, video_processor.py |
| `youtube_client.py` | 11 KB | YouTube Playwright 客户端 | video_subtitle_censor7.py |
| `video_parser.py` | 7 KB | 视频元数据解析工具 | bilibili_client.py, youtube_client.py |
| `__init__.py` | <1 KB | 包初始化 | - |

---

## 四、工具脚本 (tools/) — 独立工具

| 文件 | 大小(约) | 用途 |
|------|----------|------|
| `rollback_fetcher.py` | 10 KB | 抓取回滚与快照管理 |
| `smart_proxy_rotator.py` | 12 KB | 代理诊断与监控 (v4.0重构) |
| `sync_report_to_feishu.py` | 8 KB | 报告同步到飞书 (v4.0新增) |
| `retry_failed_youtube_v2.py` | 8 KB | YouTube 失败重试 (v2版) |

---

## 五、根目录文件 — 入口和配置

### 5.1 必须保留的入口/配置文件

| 文件 | 大小(约) | 用途 |
|------|----------|------|
| `main.py` | 13 KB | **主程序入口**，完整工作流 |
| `run.bat` | 1 KB | Windows 完整流程启动脚本 |
| `run_from_prescreen.bat` | 1 KB | 从人工初筛开始的快捷方式 |
| `.env` | 3 KB | 环境变量配置 (**含敏感信息**) |
| `config.yaml` | 10 KB | Clash代理配置 (**含密码**) |
| `.gitignore` | 2 KB | Git忽略规则 |
| `README.md` | 15 KB | 项目说明文档 (面向人类) |
| `bilibili_cookies.json` | 8 KB | B站Cookie备份 (**含敏感信息**) |
| `youtube_cookies.json` | 2 KB | YouTube Cookie备份 (**含敏感信息**) |

---

## 六、运行时数据 (data/)

| 文件 | 大小 | 用途 | 能否删除 |
|------|------|------|----------|
| `.prescreen_state.json` | <1 KB | 初筛增量进度 | 删除后下次全量重跑 |
| `scheduler_state.json` | <1 KB | 调度器状态 | 删除后重置调度 |
| `selection_history.json` | 2 KB | 入选历史 (M1 新人系数) | **不可删**，影响评分 |
| `ai_parse_errors.log` | 2 KB | AI 解析错误日志 | 可清空但建议保留 |
| `Bilibili_local_db.xlsx` | ~150 KB | B站本地数据库 | **不可删**，增量比对依赖它 |
| `YouTube_local_db.xlsx` | ~200 KB | YouTube 本地数据库 | **不可删**，同上 |
| `.rollback_snapshots/` | 动态 | 回滚快照 | 可定期清理旧快照 |
| `.rollback_excel_backups/` | 动态 | Excel 回滚备份 | 同上 |

---

## 七、历史归档 (archives/) — 已清理

### 7.1 deprecated_scripts/ (5个) — 归档的一次性脚本

已从根目录和core/移入的历史脚本：

- `fix_yt_prescreen_status.py` - YouTube初筛状态修复
- `retry_failed_youtube.py` - YouTube重试v1
- `retry_single.py` - 单条重试
- `ai_retry.py` - AI重试逻辑
- `resume_sync.py` - 断点续传同步

### 7.2 result_logs/ (~10个) — 历史结果

历史运行结果，保留最近3次即可。

---

## 八、文档 (docs/) — 全部保留

| 文件 | 大小(约) | 说明 |
|------|----------|------|
| `README.md` | 15 KB | **项目说明文档** (面向人类，本文件在根目录) |
| `Handoff.md` | 20 KB | **AI交接文档** (面向AI助手) |
| `FILE_ANALYSIS.md` | 18 KB | 本文件 - 文件分析报告 |
| `WORKFLOW_PLAN.md` | 22 KB | 工作流设计文档 |
| `REFACTOR_PLAN.md` | 15 KB | 重构计划文档 |
| `LESSONS.md` | 8 KB | 经验教训库 |

---

## 八、清理建议汇总

### 立即执行（释放约 50 个文件）

```powershell
# 1. 删除根目录重复的 Excel
Remove-Item "Bilibili_local_db.xlsx"  # 根目录那个

# 2. 删除临时测试脚本
Remove-Item "_run_bili_only.py", "test_ytdlp.py", "test_ytdlp_v2.py", "test_d1_d4_feishu_sync.py"
Remove-Item "run_from_prescreen.bat"

# 3. 清理 archives/test_scripts/ 整个目录
Remove-Item -Recurse -Force "archives\test_scripts\"

# 4. 清理 archives/youtube_retry_scripts/ 整个目录（或移入更深层归档）
Remove-Item -Recurse -Force "archives\youtube_retry_scripts\"

# 5. 清理旧的 result_logs（保留最近一周）
Get-ChildItem "archives\result_logs\" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Force
```

### 可选清理（谨慎操作）

```powershell
# 归档旧版重试脚本到子目录
Move-Item "retry_failed_youtube.py" "archives\"
Move-Item "fix_yt_prescreen_status.py" "archives\"

# 清理未引用的核心模块
Move-Item "core\resume_sync.py" "archives\"
```

### 清理后预期效果

| 指标 | 清理前 | 清理后 |
|------|--------|--------|
| Python 文件数 | ~40 | ~22 |
| 总文件数 | ~70 | ~25 |
| 占用空间 | ~1.2 MB | ~0.6 MB |
| archives/ 大小 | ~600 KB | ~100 KB |

---

## 九、文件依赖关系图（简化）

```
main.py [主入口]
 └─> core/workflow_orchestrator.py [调度器 v4.0]
      ├─> fetcher (bili_youtube_once_for_all_fetcher.py)
      │    ├─> console_utils.py (代理检测)
      │    └─> feishu_sync.py
      ├─> prescreen.py / prescreen_youtube.py
      │    └─> scoring.py (D1/D2评分)
      ├─> video_subtitle_censor7.py [字幕+审核核心]
      │    ├─> platforms/bilibili_client.py → video_parser.py
      │    ├─> platforms/youtube_client.py → video_parser.py
      │    ├─> core/ai_client.py [AI调用+自动切换]
      │    ├─> scoring.py (D3评分)
      │    ├─> youtube_rate_limiter.py [YT降频] (v4.0新增)
      │    └─> feishu_sync.py
      └─> feishu_sync.py [最终同步]

独立工具 (tools/):
 ├─> smart_proxy_rotator.py [代理诊断] (不集成到主流程)
 ├─> rollback_fetcher.py [回滚恢复]
 └─> sync_report_to_feishu.py [报告同步]

辅助模块 (core/):
 ├─> ai_client.py [独立模块，被video_subtitle_censor7调用]
 ├─> video_processor.py [视频处理编排，被main调用]
 └─> reporter.py [报告生成器，被video_processor调用]
```

**粗线 = 高频调用路径**
