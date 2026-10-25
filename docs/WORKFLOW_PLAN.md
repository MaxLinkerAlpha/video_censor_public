# 工作流规划

> **版本**: v3.4.0 | **日期**: 2026-04-13

---

> ## ⚠️ 已过时 (v5.3.4, 2026-08-31)
>
> 本文档描述的是 **v3.4.0 旧版人工初筛流程**（阶段3 人工暂停 + 交互式标记），已不反映当前设计。
> **当前设计以 [README.md](../README.md) v5.3.4 与 `core/run_workflow.py` 为准**：
> - 全流程入口：`run.bat` → `python core/run_workflow.py`（Cookie 检测 → Fetcher → Prescreen(Bili/YT) → ScriptDedup(Bili/YT) → orchestrator `--stage human-prescreen`(Bili → YT)，内部含 字幕+AI审核+飞书同步）
> - 人工初筛已由 **ScriptPrescreen 脚本自动去重**替代（阶段2.5），无需人工交互标记
>
> 本文档仅保留作历史参考，请勿据此执行操作。

---

## 一、工作流架构

```
阶段1 → 阶段2 → 【人工初筛】 → 阶段4 → 阶段5 → 完成
采集    初筛     pause        字幕    飞书同步
fetcher prescreen             censor7
```

| 阶段 | 名称 | 执行者 | 暂停 |
|------|------|--------|------|
| 1 | 视频采集 | 全自动 | 否 |
| 2 | 初筛(D1+D2) | 全自动 | 否 |
| 3 | **人工初筛** | **人工** | **是** |
| 4 | 字幕+评分 | 全自动 | 否 |
| 5 | 飞书同步 | 全自动 | 否 |

---

## 二、评分公式

`最终评分 = (D1 + D2 + D3) / 3 × M1 × 优先级加成`

| 维度 | 说明 |
|------|------|
| D1 | 时长得分（10-20min最佳，超45min拒绝） |
| D2 | 时效得分（3天内最新，21天外不考虑） |
| D3 | 互动得分 (弹幕+评论+点赞)/播放量 |
| M1 | 新人修正（分平台统计） |

**人工优先级加成 (v3.2.1)**：
- 高优先级 ×1.5
- 中优先级 ×1.25
- 低优先级 ×1.1

---

## 三、人工初筛逻辑

### 第一轮选择
- 获取 人工初筛="高优先级"/"入选" 的视频
- 直接进入字幕+评分阶段

### 第二轮补充（不足8个时）
- 获取 初筛状态="通过" AND 人工初筛!="拒绝" 的视频
- 按 D2+D1 综合得分降序排列
- 优先选择 人工初筛 为空 的视频
- 直到达到 8 个或无可选视频

---

## 四、使用方法

### 双击运行
```
run.bat
```

### 命令行
```bash
# 运行完整工作流
python workflow_orchestrator.py --full

# 仅运行指定阶段
python workflow_orchestrator.py --stage prescreen
python workflow_orchestrator.py --stage subtitle

# 查看状态
python workflow_orchestrator.py --status
```

---

## 五、版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v3.4.0 | 2026-04-13 | **模块化重构**：拆分 video_subtitle_censor7.py 为7个独立模块 |
| v3.2.1 | 2026-04-13 | 优先级评分加成、自动管理video_urls.txt |
| v3.2.0 | 2026-04-08 | Bilibili字幕获取、Cookie失效检测 |
| v3.1.0 | 2026-04-07 | 人工初筛阶段、run.bat暂停 |
| v3.0.0 | 2026-03-31 | 初始版本 |

---

## 六、相关文档

- [README.md](README.md) - 用户文档
- [REFACTOR_PLAN.md](REFACTOR_PLAN.md) - 开发者文档
