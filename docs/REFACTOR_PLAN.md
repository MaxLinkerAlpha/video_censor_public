# 重构计划书（面向开发者/AI）

> **版本**: v3.4.0 | **日期**: 2026-04-13
> **面向**: AI 助手 / 开发者

---

> ## ⚠️ 已过时 (v5.3.4, 2026-08-31)
>
> 本文档为 v3.4.0 时代的模块化重构计划/记录，其中的"人工介入点：人工初筛阶段"、旧评分公式等描述已被新设计取代。
> **当前设计以 [README.md](../README.md) v5.3.4 与 `core/run_workflow.py` 为准**：
> - 全流程入口：`run.bat` → `python core/run_workflow.py`
> - 人工初筛已由 **ScriptPrescreen 脚本自动去重**替代（阶段2.5，`core/prescreen.py --script`）
>
> 本文档仅保留作历史参考，请勿据此执行操作。

---

## 一、核心设计理念

### 评分目标：扶持小博主，扶持新视频

- ❌ 不使用播放量（会偏向大博主）
- ❌ 不使用粉丝数（会偏向成熟博主）
- ✅ 使用互动率（反映内容质量）
- ✅ 使用时效性（鼓励新内容）
- ✅ 同一博主只选一个视频
- ✅ **人工介入点：仅在人工初筛阶段**（节省 API 成本）

---

## 二、代码架构原则

| 原则 | 应用 |
|------|------|
| KISS | 不做过度模块拆分 |
| YAGNI | 不预留未使用的扩展接口 |
| SSOT | 配置集中到 config.py |
| 黄金法则 | console_utils.py 一次性封装 |
| 单一职责 | scoring.py 独立负责评分 |

---

## 三、文件结构

```
refactor/
├── console_utils.py     # [完成] Windows 控制台 UTF-8 修复
├── config.py           # [完成] 配置集中管理（SSOT）
├── scoring.py          # [完成] 评分算法（单一职责）
├── feishu_sync.py      # [完成] 飞书同步
├── prescreen.py        # [完成] 初筛模块
├── scheduler.py        # [完成] 调度器
├── workflow_orchestrator.py  # [完成] 顶层调度
├── video_subtitle_censor7.py  # [完成] 主脚本（原始版本）
├── bilibili_youtube_once_for_all_fetcher.py  # [完成] 视频采集
├── selection_history.json    # [运行时] 入选历史记录
├── scheduler_state.json     # [运行时] 断点续传状态
│
└── refactor_v2/         # [v3.4.0新增] 模块化重构版本
        ├── __init__.py
        ├── main.py          # 主入口脚本（流程编排）
        ├── ai_client.py     # AI服务层
        ├── reporter.py      # 报告生成与输出
        ├── video_processor.py  # 视频处理核心
        └── platforms/       # 平台抓取层
            ├── __init__.py
            ├── bilibili_client.py  # B站字幕获取
            ├── youtube_client.py   # YouTube字幕获取
            └── video_parser.py     # URL解析路由
```

---

## 四、重构进度

### 已完成

| 阶段 | 任务 | 状态 |
|------|------|------|
| 1 | 提取 console_utils.py | ✅ |
| 2 | 提取 config.py | ✅ |
| 3 | 重写 scoring.py | ✅ |
| 4 | 实现同博主去重 | ✅ |
| 5 | 整合评分到主流程 | ✅ |
| 6 | 统一调度器 + 状态追踪 + 断点续传 | ✅ |
| 7 | 人工初筛集成 | ✅ |
| 8 | 优先级评分加成 (v3.2.1) | ✅ |
| 9 | **模块化拆分 (v3.4.0)** | ✅ |

### v3.4.0 模块化拆分详情

将 `video_subtitle_censor7.py`（~1930行）拆分为7个独立模块：

| 新模块 | 来源代码行数 | 包含内容 |
|--------|-------------|----------|
| platforms/bilibili_client.py | 136-148, 273-691 | B站字幕获取 + 限速锁 |
| platforms/youtube_client.py | 150-193, 733-881 | YouTube字幕获取 + 风控 |
| platforms/video_parser.py | 693-731 | URL解析路由 |
| ai_client.py | 195-271, 883-973 | AI调用 + 模型选择 + 解析 |
| reporter.py | 1301-1463 | 报告生成 + 终端打印 |
| video_processor.py | 1029-1299, 1671-1726 | 评分计算 + 筛选排名 |
| main.py | 1569-1669, 1728-1930 | 主流程编排 |

### 待优化

| 任务 | 说明 | 状态 |
|------|------|------|
| 历史入选次数统计 | 从飞书多维表格同步 | ⏳ |
| 评分阈值调优 | 根据实际数据调整 | ⏳ |

---

## 五、验证清单

- [x] `python -m py_compile` 所有文件通过
- [x] 评分功能正常
- [x] 同博主去重生效
- [x] 新人修正系数正确
- [x] 人工初筛字段读取正常
- [x] run.bat 暂停机制正常
- [x] 不足8个自动补充逻辑正常
- [x] 优先级评分加成生效
- [x] **模块化拆分完成 (v3.4.0)**
- [x] 所有模块导入测试通过

---

## 六、暂不纳入

| 项目 | 原因 |
|------|------|
| D4 热度得分 | 扶小不扶大 |
| 粉丝数/播放量维度 | 扶小不扶大 |
| platforms/ 拆分 | ~~违背 KISS~~ → **v3.4.0已实现** |
| services/ 拆分 | ~~违背 YAGNI~~ → **v3.4.0已实现（ai_client.py）** |

---

## 七、模块化拆分原则（v3.4.0）

### 拆分目标
- 遵循 `re-refactor.md` 建议
- 消除"上帝脚本"（~1930行的 video_subtitle_censor7.py）
- 单一职责：每个模块只做一件事

### 拆分方案
```
原: video_subtitle_censor7.py (1930行)
     │
     ├── platforms/bilibili_client.py   (420行) - B站字幕
     ├── platforms/youtube_client.py    (250行) - YouTube字幕
     ├── platforms/video_parser.py       (80行) - URL解析
     ├── ai_client.py                    (280行) - AI调用
     ├── reporter.py                     (220行) - 报告输出
     ├── video_processor.py             (350行) - 视频处理
     └── main.py                         (350行) - 流程编排
```

### 依赖关系
```
main.py
  ├── platforms/video_parser.py
  ├── platforms/bilibili_client.py
  ├── platforms/youtube_client.py
  ├── ai_client.py
  ├── video_processor.py
  ├── reporter.py
  └── core/ (复用现有模块)
      ├── config.py
      ├── scoring.py
      ├── scheduler.py
      └── feishu_sync.py
```

### 重构优势
1. **容错隔离**：B站爬虫挂了，去修 `bilibili_client.py`，不会误改 AI 评分逻辑
2. **降低认知负荷**：每次只需关注当前领域的逻辑
3. **消除全局变量**：限速锁等封装到类属性中

---

*面向 AI 助手/开发者，详细实现计划 v3.4.0*
