# 修复日志 (CHANGELOG)

本文档记录所有代码修复和变更历史。

---

## [2026-09-20] v5.4.6 - YouTube 评分 D2/D3 恒为 0（v3.9.0 实现丢失）+ D3 小样本门槛

### 问题
用户要求排查"补跑路径缺统计数据回填"。深入探查后发现范围**远不止补跑路径**：
**全部 YouTube 视频的 D2(时效) 与 D3(互动) 恒为 0**，评分长期退化成 `(D1+D4)/2`。

实测（近 35 天「字幕成功 + 正向标记」208 条）：

| | D2 分布 | D3 分布 |
| --- | --- | --- |
| **YouTube** | `{0.0: 208}` ← 全 0 | `{0.0: 208}` ← 全 0 |
| B站（对照） | 29 种取值 | 6 种取值 |

而飞书里 `Views` 数据齐全（206/208 条 > 0）—— **不是没数据，是不读数据**。

### 根因（实现丢失，非设计）
四重证据表明"YouTube 必须算 D2/D3"是既定设计：

| 证据 | 内容 |
| --- | --- |
| `CHANGELOG.md` v3.4.0 | 「YouTube视频：`(D1+D4)/2`（**跳过D2/D3**）」+「**解决YouTube视频全部0分的问题**」→ 当时的权宜之计 |
| `core/scoring.py:6-9` | v3.9.0:「YouTube视频**启用**D2时效分和D3互动分（**数据从飞书回填**）」「两平台评分公式统一为四维（数据完整时）」 |
| `core/_deprecated_video_subtitle_censor7.py:1095-1136` | **实现存在过**：注释 `# v3.9.0: YouTube视频从飞书回填统计数据（抓取阶段已写入飞书但字幕阶段丢失）` |
| `tools/yt_enrich.py` | 专门的补偿工具「YouTube视频数据回填工具 v1.0」：调 API 取统计数据 → 算 D1/D2/D3 → 写飞书 |

**两个缺陷叠加导致长期失效**：
1. 旧实现 `FeishuSync()` **未调用 `set_bitable_token()`** → 查询必然失败 →
   被 `except` 吞掉只打 WARN（"将使用默认值，D2/D3可能为0"）→ **代码在也跑不通**
2. 重构到 `video_processor.py` 时**整段回填逻辑被删除**，`yt_enrich.py` 也再未被任何代码调用

### 修复
| 文件 | 改动 |
| --- | --- |
| `platforms/youtube_client.py` | 新增 `fetch_youtube_stats()`：调 YouTube Data API v3 `videos.list` 取 `publishedAt`/`viewCount`/`likeCount`/`commentCount`/`channelId`；带代理、2 次重试、`threading.Lock` 保护的进程内缓存（同一视频本流程内被评分 3 次）；确定性失败（视频不存在/无 key）缓存空结果，网络异常不缓存以保留重试机会 |
| `core/video_processor.py` | `calculate_video_score` 新增 YouTube 统计数据补全：`publish_time` 为空时调用 `fetch_youtube_stats`，并**写回 `video_info`** 供后续 2 次评分复用；失败**显式 WARN**（每视频一次）并降级为 0，禁止静默 |
| `core/scoring.py` | `calc_d3_engagement_score` 新增最小样本门槛 `MIN_VIEWS_FOR_ENGAGEMENT=100`：观看数不足时不计互动分 |

### 为什么必须同时加 D3 门槛
只回填不加门槛会**修一个 bug 引入另一个**：互动率的分母是播放量，小样本下
`views=5` + 1 个赞 → 互动率 >10% → **D3 直接满分 10** → 总分被推到 **19.5**。
实测这就是"小样本比率虚高"（LESSONS 已记录此反模式）。

### 验证（全部实测）
1. **D3 门槛**：`views=5/14/99` → D3=0（"样本不足"）；`views=100` → 10.0；
   `views=1000, likes=50, comments=10` → 7.0（6%）；正常档位不受影响。
2. **`fetch_youtube_stats` 实测**（真实调用）：
   `视频A` → `publish_time=<时间戳>`、`views=<N> likes=<N> comments=<N>`、
   `channel_id=UCxxxxxxxx`。
3. **端到端**：`视频A` → D1=10 / **D2=1.8** / **D3=0**(样本不足) / D4=9 → total=8.32；
   `视频B` → D1=10 / **D2=10.0** / **D3=3.0** / D4=9 → total=19.6（含来源 +10）。
   补全后 `video_info` 正确写回（`publish_time`/`views` 不再为 0）。
4. **重复评分**：同一 `video_info` 第 2 次评分耗时 0.000s、不再打印补全日志、结果一致。
5. `python -m py_compile` 通过；`tools/check_undefined_names.py` 报 0 处。

### 影响与范围
- 修复前后评分会变化（模拟：上升 128 条 / 下降 76 条，跨过 8 分线的 24 进 27 出）。
  按用户决定**仅对今后新批次生效，不重算历史已评分数据**。
- D3 门槛对 B站 同样生效（`calc_d3_engagement_score` 为两平台共用）；
  B站 播放量普遍 >100，实际影响有限。

---

## [2026-09-20] v5.4.5 - B站「发布日期」被兜底分支写成"抓取当天"

### 问题
用户反馈：**B站填写的视频发表时间不对**。实测 2026-09-20 批次 **23/23 全部**写成 `2026/09/20`
（= 抓取当天），而真实发布时间分布在 2024-09-27 ~ 2026-09-17。

对照官方 API（`x/web-interface/view` → `pubdate`）：20/20 抽样**全部不一致**。

### 根因
`core/bilibili_youtube_once_for_all_fetcher.py` 的 JS `parseDate()` 只认
`分钟前 / 小时前 / 昨天 / 前天 / MM-DD / YYYY-MM-DD`，而 B站搜索页当前实际输出的是**中文格式**。
实测 42 张卡片的原始日期文本：

| 页面文本 | 数量 | 旧 `parseDate` | 结果 |
| --- | --- | --- | --- |
| `· 3天前` | 11 | ❌ 只写了 `前天`，无 `X天前` | 落到 else |
| `· 9月16日` / `9月15日` / `9月14日` | 17 | ❌ 只写了 `MM-DD`，无 `M月D日` | 落到 else |
| `· 5小时前` / `9小时前` / `12小时前` | 4 | ✅ | 正确 |
| `· 昨天` | 2 | ✅ | 正确 |

→ 28/42 张卡片走 `else`，而该分支的返回值是
`` `${d.getFullYear()}/${...}` ``，即 **`new Date()` = 运行当天**。
它不报错、不返回空，而是**静默伪造当天日期** —— 这是本次事故的本质。

### 影响面
- 受影响：**仅 2026-09-20 这一批 23 条**（本地 Excel 序号 1018~1040 + 飞书同批记录）。
  按"创建时间分组 → 发布日期唯一值数"检测：09/20 批 = 1 个唯一值（异常），
  09/14 及更早各批 = 5~33 个唯一值（正常）。
- **D2 时效分/总分未受污染**：`calculate_video_score` 的 `publish_time` 来自
  `platforms/bilibili_client.py` 的官方 API `pubdate`，不读该字段。
- **机器初筛的 ">21 天" 时效规则被绕过**：`core/prescreen.py:evaluate_video` 读的正是
  `发布日期`，错误值让它以为视频是当天发布 → 12 条真实为 2024/2025 的老视频通过了初筛
  （其中 3 条被 ScriptDedup 标成「入选」，但 D2=0 → 总分 0，不会真正入选）。
  按用户决定，本次**只修正发布日期，不改动「人工初筛」状态**。

### 修复
| 文件 | 改动 |
| --- | --- |
| `core/bilibili_youtube_once_for_all_fetcher.py` | `parseDate()` 重写：补全 `YYYY年M月D日` / `M月D日` / `M-D`（无年份时若晚于今天则视为去年）、`X秒前` / `X分钟前` / `X小时前` / `刚刚` / `今天` / `昨天` / `前天` / `X天前` / `X周前` / `X个月前`；`X小时前` 取 **X+0.5 小时**（B站向下取整，取区间中点以最小化跨午夜误差） |
| 同上 | **解析失败返回空串**，不再回退到"运行当天"——宁可为空也不伪造 |
| 同上 | 卡片日期文本的兜底正则同步补全（`\d+天前` / `\d{1,2}月\d{1,2}日` / `\d+周前` / `\d+个月前` 等） |
| 同上 | `push_to_bitable`：`ft == 5`（datetime）且值为空时**跳过该字段**，避免写空串触发 `DatetimeFieldConvFail` |

### 验证（全部实测）
1. **端到端对照**：重跑 `scrape_bilibili`（162 条）→ 逐条对比官方 API `pubdate`
   → **一致 162 / 不一致 0 / 解析为空 0**（修复前抽样 20 条为 20 条不一致）。
2. **日期分布检查**：修复后 162 条的发布日期呈自然分布（09/17×11、09/10×10、09/14×9 …），
   不再集中在同一天。
3. **数据修正**：先 dry-run 确认 23/23 均可从 API 取到真实日期，再 apply ——
   本地 Excel 更新 23 行、飞书写入 23/23，**回读校验 0 条不一致**。
4. 修正后复检：09/20 批次 23 条 → 发布日期唯一值 **16 个**（恢复正常），异常告警消失。
5. `python -m py_compile` 通过。

### 注意
- 本次**未引入 API 校准**（用户明确要求"只需发布日期、不需要精细到几小时"），
  所以 `X小时前` 这一类在跨午夜时仍存在 ±1 天的固有歧义（本次 5/162 条曾偏移 1 天，
  取区间中点后降为 0）。B站若再次改版日期文本，需同步补 `parseDate` 分支；
  最彻底的方案是抓取后用官方 API `pubdate` 校准（可作为后续备选）。
- 本地 Excel 备份：`.backups/Bilibili_local_db_v5_4_5_backup_20260920_182457.xlsx`。

---

## [2026-09-20] v5.4.5 补充 - YouTube 侧同类检查 + 「入选」标记完整性数据修正

### 检查结论（近 35 天 252 条正向标记，逐条对比 YouTube 官方 API）

**发布日期类问题在 YouTube 侧不存在**：

| 字段 | 不一致 | 说明 |
| --- | --- | --- |
| `Published Date` | **0 / 218** | ✅ |
| `Published At` | **0 / 218** | ✅ |
| `Duration` | **0 / 218** | ✅ |
| `Channel` | 0 | 官方 `channelTitle` 自带尾随空格, 无害 |

原因：YouTube 走官方 API 的 `publishedAt`（UTC ISO），
`bilibili_youtube_once_for_all_fetcher.py:534` 直接 `strptime + timedelta(hours=8)`，
**不解析网页文本**，因此没有 B站 那种"页面格式改版 → 兜底伪造当天"的失败模式。

属正常现象的项：
- `Views` / `Likes` / `Comments` 差异 206 / 138 / 61 条 —— 飞书存的是**抓取时刻快照**，
  API 是**当前值**。199 条在涨（正常），7 条下降（YouTube 会周期性清理无效播放量）。
- `Language` 44 条与 API 的 `defaultAudioLanguage` 不同 —— 飞书值来自
  「字幕轨语言 + AI 二次仲裁」（v5.3.7 设计），不是 API 声明语言的镜像。

### 发现的问题：「入选」标记挂在不完整的记录上（44 条 = 17.5%）

`Total Score = 0`、`Subtitle Status` 为空或 failed、`AI Review Result` 也为空，
但 `Human Prescreen` 却是 `Selected` / `High Priority`。

根因是 **v5.4.3 修复的那个 bug 的历史遗留**：字幕获取失败时结果字典缺 `video_id`，
`_sync_youtube_failed_status` 静默跳过 → 飞书里"什么都没写"，
而 ScriptDedup 已给了「入选」标记、AI 环节又没跑成。

逐条实测字幕可获取性（`youtube-transcript-api` + API `caption`）：
**42 条视频真的没有字幕**（终态），**2 条可以取到**（`视频C` / `视频D`）。

### 数据修正（无代码变更）

1. **补跑 2 条可恢复视频**：`视频C` → 总分 0.0（114 分钟超长被拒）；
   `视频D` → 总分 5.95（D1=4.9 / D4=7.0）。**均 < 8，不参与最终入选。**
2. **42 条真无字幕**：补写 `Subtitle Status=failed` + `AI Review Result=unsafe` +
   `AI Review Report=WARN 该视频无字幕，无法进行内容审核`，并把 `Human Prescreen` 置 `Rejected`。
3. 上述 2 条补跑视频（总分 < 8）一并置 `Rejected`。

合计写入 **44 条，成功 44/44**；回读校验通过。
复核：近 35 天正向标记 **252 → 208**，**「挂正向标记但 AI 未完成」= 0 条**。

### 附注
- 移出后仍有 1 条总分=0 的正向标记（`视频E`，54 分钟超长 → D1=0 → 总分 0），
  但其 **AI 审核已完成**（`字幕=success / AI=safe`），属"评分不达标"而非"流程未完成"，
  按与 B站 侧一致的口径**未改动**。
- 历史批次的同频道重复入选（09/07 俄文组 `Alex Movang` 3 条、09/14 德文组 `Herr Allright` 2 条）
  均为该语言组 ≤8 时按规则字面允许的重复，按用户决定**不回改历史**。
- 补跑路径（`fetch_and_process_single` 单条）不含统计数据回填，D2/D3 会是 0，
  导致补跑的分数被系统性低估 —— 本次 2 条结论不受影响（D1 本身已决定拒/低分），
  但若将来用于"抢救可能入选的视频"需注意此偏差。

---

## [2026-09-20] v5.4.4 - 同一频道在同一语言下多次入选（三个成因 + 兜底收敛）

### 问题
用户反馈：**某语言明显 >8 条，同一频道仍被入选多次**。实测（2026-09-20 批次）：

| 语言（AI 判定） | 条数 | 重复频道 | 依据 |
| --- | --- | --- | --- |
| 英文 (YouTube) | 18 > 8 | 频道A **3** 条、频道B **2** 条 | Notes: `en` 组 "Kept: latest of 2/13" + `N/A` 组 "Kept: latest of 3" / "Kept: fill to 8" |
| 中文 (Bilibili) | 10 > 8 | 频道C **2** 条 | 1 条「高优先级」的 Notes **只有 Dropped、没有 Kept** |

### 根因（三者叠加）

**M1｜语言分组键是"视频声明语言"，`N/A` 自成一组并独占配额**
`ScriptDedup` 用 `Language` 字段（en/de/ru/zh/**N/A**）分组（`prescreen.py:_platform_config`），
每组独立执行「每频道 1 条」+「有效入选 <8 就补足到 8」。
→ 同一频道只要有一部分视频声明语言是 `N/A`，就能在**另一个组**再拿一份配额
（频道A 3 条同属 Channel ID `UCxxxx…`，却分属 `en`(1)/`N/A`(2) 两组）。
注：该阶段 AI 判定的「语言_Language」尚未写入，代码只能读到声明语言 —— 属结构性错配。

**M2｜"补足到 8" 不做作者去重（设计允许，但紧贴边界）**
`有效入选 < 8` 时从同频道被丢弃的视频里按日期补足，单作者可占满整组
（俄文 8 条中单一频道占 4；YouTube zh 组 4 条全是频道D）。
因补足目标就是 8，组内结果精确落在 8，一旦后续通过数增长就变成" >8 且同频道多条"。

**M3｜来源视频翻转无视"已被去重淘汰"**
`_flip_source_priority` 只判断「AI 已完成 + 在来源注册表」，**不看当前 Human Prescreen**，
直接覆盖写「高优先级」→ 已被判定为 Rejected 的视频在 AI 跑完后被翻回入选，绕过语言去重。
检测「Notes 只有 Dropped 无 Kept 却仍是正向标记」：YouTube 0 条、**B站 1 条**（即 频道C）。

### 修复
| 文件 | 改动 |
| --- | --- |
| `core/prescreen.py` | **M1**：新增 `UNKNOWN_LANGUAGE_KEY`；分组改为两遍 —— 未知语言先归入"该频道本批出现过的已知语言组"，若该频道本批全是未知语言则进入**未知语言补充位**（不占语言配额，只允许"任何语言组都还没有代表"的作者补 1 条） |
| `core/prescreen.py` | **兜底校验**：新增 `enforce_final_language_dedup()`（+`--enforce-lang-dedup` / `--dry-run` / `--days` / `--no-window` CLI）—— 按「语言_Language」+ 频道复查，某语言通过 AI 数 > 8 时同频道只留 Total Score 最高 1 条，其余置拒绝并追加 Notes |
| `core/prescreen.py` | 新增 `resolve_batch_cutoff()`：兜底校验**只作用于"本批"**（优先运行锚点=上次运行结束，无锚点回退 `days_filter` 天，`0`=不限）—— 规则是"每周批内"约束，不得回改历史各周产出 |
| `core/workflow_orchestrator.py` | **M3**：`_flip_source_priority` 索引改为 `vid -> (record_id, 当前人工初筛)`，当前为 `拒绝/Rejected` 时 **`[FLIP-SKIP]` 跳过翻转**；并在 Stage 3 收尾（翻转/复活之后）调用兜底校验 |

### 验证（全部实测）
1. **M1 桩测试**：覆写候选池、其余走真实代码，复刻 频道A 场景（en 1 条 + N/A 2 条）
   → 保留 频道A **1** 条、频道B **1** 条、纯未知语言频道 **1** 条，**无任何同频道多条**。
2. **兜底校验 dry-run**（窗口=09-14 10:45）：识别出 英文 18 条/频道A 3 条、频道B 2 条，
   中文 10 条/频道C 2 条；俄文 8、德文 8 判为 `<= 8` 不收敛（符合规则字面）。
3. **兜底校验 apply**：实际收敛 **4 条**（`bilibili:中文` 1 + `youtube:英文` 3）。
4. **收敛后复检**：同窗口 dry-run 显示 中文 10→**9**、英文 18→**15**，**重复频道 0 个、收敛 0 条**。
5. `python -m py_compile` 通过；`tools/check_undefined_names.py` 报 0 处。

### 注意
- 兜底校验的窗口设计很关键：**首版实现扫了全表**，dry-run 显示会收敛 194 条（B站）/ 51 条（YT 英文）
  的历史记录 —— 属于误伤。加入"本批窗口"后收敛数正确降到 4 条。**改数据前必须先 dry-run 看规模。**
- 历史各周是否也要按此规则回改，需用户单独决策（可用 `--no-window` 显式执行）。

---

## [2026-09-20] v5.4.3 - 失败结果缺 video_id 导致飞书失败状态标记被静默跳过

### 问题
`workflow_orchestrator._sync_youtube_failed_status`（v5.3.4 引入）本应把失败视频写回飞书：
`no_subtitle / region_locked / video_unavailable` → `Subtitle Status = failed`（终态）；
`ip_blocked / rate_limit / ...` → `pending`（待重试）。

但 `fetch_and_process_single` 的字幕获取**失败分支**返回的字典里**没有 `video_id` 字段**
（`platforms/youtube_client._youtube_base_result` 也不含 `video_id`），
于是 `_sync_youtube_failed_status` 在 `if not video_id: continue` 处**静默跳过全部 no_subtitle 失败**，
且 `except Exception: pass` 让它完全无声。

2026-09-20 事故表现：本轮 8 条 `no_subtitle` 视频在飞书里 `Subtitle Status` 一直是**空**，
（同期 `rahhdTkIkwg` 因失败发生在 AI 阶段、结果里带 video_id，所以被正确标为 `failed`），
排查时看起来像"流程什么都没做"。

### 修复
| 文件 | 改动 |
| --- | --- |
| `platforms/youtube_client.py` | 新增 `_stamp_error_category()`；`fetch_youtube_subtitle` 的 4 个失败返回路径统一把 `error_category` 写回结果字典（不改变函数签名，避免破坏 4 处调用方） |
| `main.py` | `fetch_and_process_single` 失败分支补 `video_id` + `error_category` |
| `core/workflow_orchestrator.py` | `_retry_youtube_smart` 的 `[SKIP]` 结果补 `video_id` |

### 验证
对一条已知无字幕视频（`TEBThZNTstI`）做真实调用，断言失败结果必须带定位字段：
```
success = False | video_id = 'TEBThZNTstI' | error_category = 'no_subtitle'
platform = 'youtube' | has_subtitle = False
```
断言通过（修复前 `video_id` 缺失、`error_category` 也不存在）。
另：`python -m py_compile` 通过；`tools/check_undefined_names.py` 报 0 处。

### 数据修复（本轮收尾）
- 8 条无字幕终态视频补写 `Subtitle Status = failed`
  （写前核对：现存 21 条同类记录均**只写** `Subtitle Status`，其余字段留空 → 保持一致）
- `rahhdTkIkwg`（Discord 自荐来源视频）单独补跑成功：
  `19.50 分`（含来源加权 +10，D4 = 9.0/AI 86 分，安全），`Human Prescreen` 翻转为 `High Priority`
- 判定依据：8 条视频 `Has Captions = No`（抓取阶段 YouTube 元数据即为无字幕），
  且在**节点正常**（对照视频 2/2 成功）的情况下**仍**稳定报 `no_subtitle` → 属视频级终态

### 读飞书字段名注意
YouTube 表实际字段名是英文：`Video ID` / `Title` / `Prescreen Status` / `Human Prescreen` /
`Subtitle Status` / `AI Review Result` / `AI Review Report` / `Total Score` / `Language`（≠`语言_Language`）。
写代码时务必用 `core/feishu_fields.py` 的常量，不要手写字面量（本轮排查时曾因手写中文字面量而误判"字段不存在"）。

---

## [2026-09-20] v5.4.2 - 运行锚点：修复"上次产出被误算作本周已标记"的边界 BUG

### 问题
ScriptDedup 用**单一固定 7 天滚动窗口**（`cutoff = today - (days_filter-1)`）同时约束
"候选池"与"已标记统计"。当上次工作流的运行日恰好 == `today - 6` 时，上次的产出
（Human=Selected）会落进窗口内，被当成"本周已有标记 UP"，使其 UP 的**本批候选被整批排除**。

2026-09-20 事故实测数据:

| 指标 | 数值 |
| --- | --- |
| "本周已有正向标记" | 45 条 / 35 个 UP —— **全部是 09-14（上次运行）写入** |
| 本周新增覆盖频道 | 52 个，其中 **25 个在上周已入选** |
| 重叠频道贡献 | **102/145 条（70.3%）** 被整批排除 |
| 后果 | ScriptDedup 保留 42 → **11**；最终仅 10 条通过 AI 审核 |

上周（09-14）未触发纯属边界巧合：那次窗口下界 09-08 > 上次产出日 09-07。
即：两次运行间隔 ≤6 天就会触发，间隔 ≥7 天才安全 —— 与用户"可能提前或延后启动"的实际节奏不兼容。

### 方案（运行锚点，摆脱固定天数）
- **新增 `core/run_anchor.py`**：记录上次运行的起止时刻（`data/run_anchor.json`）
  - 线程锁 + 原子替换写（`os.replace`），避免并发半写
- **`core/prescreen.py`**：ScriptDedup 拆分为两个**独立**下界
  - `pool_cutoff_ms` = 上次运行**开始**时刻 → 上次中断未处理的记录也能被本次接住
  - `marked_cutoff_ms` = 上次运行**结束**时刻 → 排除上次运行自身的产出
  - 锚点缺失（首次运行）时回退到固定天数窗口并打印 WARN
- **`core/run_workflow.py`**：仅在**完整成功**时写入锚点；失败时保持旧锚点，
  使下次窗口仍覆盖本次未处理的记录，避免漏处理

### 验证（修复后重跑 ScriptDedup）
| 平台 | 已标记 | 保留 | 有效入选 |
| --- | --- | --- | --- |
| YouTube | 45 → **11** | 11 → **31** | 56 → **42** |
| Bilibili | 13 → **6** | 11 → **3** | 19 → **9** |

锚点日志: `运行锚点: 候选池下界=2026-09-14 10:21, 已标记下界=2026-09-14 10:45`

### 数据修复
- 重置被误拒的 **97 条**（YouTube 86 + B站 11）的"人工初筛"字段后重跑
  （仅重置"机器初筛通过 + 人工初筛=拒绝"的记录；机器初筛拒绝的不动）

### Number 批次校验（附）
- ScriptDedup 新增输出「本批 Number 区间」（飞书自动编号），用于人工/自动核验批次边界
- 实测（YouTube）: 上周 09-14 = `13899~14059`（161 条连续）；本周 09-20 = `14060~14204`
  （145 条连续），两批**无缝衔接**（14059 → 14060），佐证批次划分正确
- 读取要点（曾踩坑）:
  - `Number` 字段类型为 **1005（自动编号）**，API 返回的是**字符串**，必须
    `str(...).strip().isdigit()` 后再转 int（用 `isinstance(n, int)` 会全部漏掉）
  - **search 接口不带 filter 时返回的 `Number` 是位置序号（1,2,3…）假值**；
    必须带 filter，或走 `GET /records`，才是真实自动编号

---

## [2026-09-20] v5.4.1 - 来源翻转/复活阶段缺导入导致 NameError + Cookie 检测假阳性

### 问题 1: 来源优先级翻转/复活阶段整段失效（静默）
v5.4.0 新增的 `_flip_source_priority` / `_resurrect_source_videos` 直接引用了
`BILI_TABLE_ID` / `YT_TABLE_ID`，但这两个名字此前只在其它函数的**局部 import** 中出现，
模块级并未导入 → 运行到该分支抛 `NameError` 并被 `except` 吞掉：

- 日志: `[ERROR] 来源优先级翻转失败 (建索引异常): name 'BILI_TABLE_ID' is not defined`
- 日志: `[WARN] 来源视频翻转/复活阶段异常 (不阻断): NameError: name 'YT_HUMAN_REJECTED' is not defined`
- 后果: 本周 Discord 自荐 / 中文精选视频 AI 审核完成后**未被翻转**为高优先级

### 修复 1
- `core/workflow_orchestrator.py`:
  - 模块导入块补 `YT_HUMAN_REJECTED`
  - `_flip_source_priority` / `_resurrect_source_videos` 内显式
    `from feishu_sync import BILI_TABLE_ID, YT_TABLE_ID`
- `tools/check_undefined_names.py`（新增工具，用 symtable 静态排查"引用未定义全局名"，
  已验证可检出上述 4 处隐患，用于确认无同类残留）

### 问题 2: Cookie 检测假阳性 → Stage 3 (B站) 19 条误判"无字幕"
`tools/check_cookies.py` 原为纯离线检查（文件存在 / 关键项 / expirationDate / 文件年龄），
B站 SESSDATA 的 expirationDate 常在数月后、文件也较新，故**Cookie 已失效仍报"有效"**。
2026-09-20 周常工作流因此漏检，Stage 3 (B站) 19 条视频全部误判"无字幕"，
其中 13 条写入飞书错误数据（字幕状态=失败 / AI审核结果=不通过 / 报告=WARN 无字幕）。

### 修复 2
- `tools/check_cookies.py` v5.3.10：新增**服务端实时校验**（B站 `nav` 接口 `isLogin`）作为权威判据；
  网络不可达时仅 WARN 不拦截，避免把假阳性改成"网络抖动即阻断"的假阴性
- 事故数据已清理并按修复后 Cookie 重跑 Stage 3 (B站)：19/19 成功写入
  （仅 1 条为真实无字幕）

### 保障
- B站 Cookie 判定改为"离线 + 服务端"双重校验
- 后续新增模块级常量引用时，可用 `tools/check_undefined_tmp.py` 做静态排查

---

## [2026-09-14] v5.4.0 - 来源视频全流程化：后置翻转优先级 + 复活补跑

### 问题
Discord 自荐 / 中文精选视频此前在标记阶段（Stage 2.1）**前置写入** Human Prescreen = 高优先级，
导致：AI 审核失败或无字幕的视频以"高优先级 + AI 字段为空"状态直接进入最终审核池，
绕过了"必须经过 AI 审核和评分"的要求。

### 方案（来源注册表架构）
- **新增 `core/source_registry.py`**：本地来源注册表 SSOT（`data/source_registry.json`），
  记录 video_id → 来源类型（discord/curated），线程安全、幂等注册
- **`core/discord_video_mark.py` / `core/curated_cn_mark.py`**：删除前置写优先级逻辑，
  改为来源注册 + Notes/备注追加来源标注；Discord 未命中视频经 YouTube Data API 补录入表
- **`core/run_workflow.py`**：标记阶段从 Stage 2.1（Prescreen 之后）前移至 Stage 1.5
  （Fetcher 之后、Prescreen 之前），补录视频当轮即可参与全流程
- **`core/video_processor.py`**：评分时按来源注册表直接给 High Priority 权重（+10），
  同时修复优先级映射文件路径错误（原 `core/core/...` 永远加载失败）
- **`core/workflow_orchestrator.py`**：
  - `_flip_source_priority`：AI 审核完成后，仅对真正完成审核（有字幕 + censor 结果）的
    来源视频翻转为 高优先级/High Priority
  - `_resurrect_source_videos`：最终 AI 完成数 < 8 时，复活被 ScriptDedup 淘汰的来源视频
    （机器初筛拒绝 + 当周写入 + D1+D2 降序）补跑 AI，通过后同样翻转
- **`core/prescreen.py`**：ScriptDedup 写 Notes 改为合并模式，不再覆盖来源标注行

### 保障
- 评分加权（+10）与字段翻转解耦：评分时即加权，字段后置翻转
- AI 未完成的来源视频不会获得高优先级标记，杜绝"高优先级但 AI 字段为空"
- 复活补跑同样必须通过 AI 审核

---

## [2026-09-07] v5.3.9 - ScriptDedup 恢复按语言分组去重

### 问题
v5.3.8 误将 ScriptDedup 去重从"按语言分组"改为"全局按频道"，破坏了原设计的
语言维度语义。用户规则为：去重按语言维度 —— 每个语言组内每频道最多 1 条
（同 UP 跨语言可各留 1 条，因不同语言面向不同受众）。

### 修复
- `core/prescreen.py` ScriptDedup:
  - 恢复按语言分组去重（撤销 v5.3.8 的全局去重）
  - 每个语言组内：每频道留最新 1 条；该语言组内已标记 UP（自荐/精选）的其他
    候选视频排除（该 UP 在本语言已有代表视频）
  - 该语言组有效入选（已标记数 + 脚本保留数）< 8 时，从同频道多余视频按日期
    最新优先补充（规则：总数<8 时允许同频道重复）
  - `_fetch_passed_unmarked` 返回值改为按语言维度的正向标记映射
    (`marked_authors_by_lang` / `marked_positive_by_lang`)
- `core/version.py`: 5.3.8 → 5.3.9

### 影响面
- 仅 ScriptDedup 去重分组维度：全局 → 按语言；已标记 UP 排除作用域同步改为语言组内
- 不改飞书数据

---

## [2026-09-07] v5.3.8 - Discord 自荐标记修正 + ScriptDedup 全局去重

### 问题
1. Discord videos-n-streams 自荐视频被标记为 `Selected`（零加权"强行入选"），
   与"自荐优先"意图不符 —— 本周 12 条自荐视频未体现为"优先级视频"。
2. ScriptDedup 按语言分组去重，同 UP 跨语言组各留 1 条（如 en/ru 各 1 条）造成同 UP 重复入选；
   且已标记 UP（Discord 自荐/中文精选）的其他视频仍被脚本选入，进一步重复。

### 修复
- `core/discord_video_mark.py`: 写入值 `Selected` → `High Priority` (+10 加权)
- `core/prescreen.py` ScriptDedup:
  - 改为全局按 UP 去重（不再按语言分组）
  - `_fetch_passed_unmarked` 额外返回本周正向标记 UP 集合与数量，
    已标记 UP 的未标记视频直接丢弃（该 UP 本周已有代表视频）
  - 有效入选总数 = 本周正向标记数 + 脚本保留数，不足 8 时按日期最新优先补充
- `core/workflow_orchestrator.py`: `retry-youtube-failed` 过滤器从"仅 Selected"
  扩大为全部正向标记（High/Medium/Low Priority + Selected），确保自荐失败视频可重试
- `tools/fix_discord_marks.py`: 一次性数据修复脚本（dry-run/pilot/full 三模式，
  写入前重取字段类型校验，select 传 NAME）—— 已修复本周 12 条自荐视频为 High Priority 并重算分数

### 数据修复记录
- 本周 12 条 Discord 自荐视频：Human Prescreen Selected → High Priority
- 11 条有分数记录：Total Score +10（如 9.5 → 19.5），Rating 统一升为 A
- 1 条审核失败（5eskxR8YOFU）：仅改标记，不写分数

### 影响面
- ScriptDedup 去重结果：本周从"56 条 Selected（含 5 个 UP 重复）"修正为
  去重后全局同 UP 最多 1 条
- 下次全流程运行时 Discord 自荐将以 High Priority 正确体现为优先级视频

---

## [2026-09-01] v5.3.6 - Stage 2.1 硬超时兜底修复

### 问题
Stage 2.1（Discord 自荐 / 中文精选标记）在 run_workflow.py 中虽已设计为"失败不阻断"（仅 WARN），
但 run_stage 调用时未传 timeout —— 若脚本内部网络请求意外挂死（DNS 卡死、代理半开连接等
requests timeout 覆盖不到的场景），整个工作流会永久卡在该阶段。

### 修复
- `core/run_workflow.py`: 可选阶段 run_stage 增加 `timeout=900` 硬超时兜底；
  超时按失败处理 → 仅打印 WARN 继续后续阶段。同步更新模块 docstring 说明阶段分类。
- `core/version.py`: 5.3.5 → 5.3.6

### 影响面
- 仅 run_workflow.py 的 optional_stages 执行路径；orchestrator 独立 stage 入口
  已有 run_script timeout=3600 保护，未改动。

---

## [2026-09-01] v5.3.5 - 字幕语言AI分类 + Discord自荐采集 + 中文精选高优标记

### 新功能 A: 字幕语言 AI 分类字段「语言_Language」(select, 10选项)

1. **新增 `core/language_classifier.py`**
   - `classify_language_code(code)`: 纯函数, 语言代码 → 10 分类名 (zh/zh-*/cmn/yue→中文, en*→英文, de*→德文, pl→波兰文, ja→日文, ko→韩文, fr*→法文, ru*→俄文, es*/es-419→西班牙文, zxx/未知→其它, 空/None→None)
   - `classify_by_ai(subtitle_text, title, hint_code)`: 智谱 glm-4-flash (openai 兼容客户端, 凭证来自 add-on/.env 的 AI_API_KEY/AI_BASE_URL, load_dotenv 显式路径), 输入=字幕前1500字+标题+hint代码参考, 严格 JSON `{"language": "..."}` 10选1, 3 次重试; **AI 结果为准** (即使与 hint 不同); AI 全失败回退 `classify_language_code(hint_code)`; 永不抛异常
   - `--verify` 抽样验证模式: 3 条硬编码 Video ID, fetch字幕→AI分类→仅写这 3 条 (禁止批量回填)
   - `__main__` 自检: 32 个映射用例 + 4 个 mock 用例全通过

2. **`platforms/youtube_client.py`**: `_youtube_base_result` 增加 `subtitle_language: ''` 默认键; 原声/自动字幕两处 return 前把 `transcript.language_code` 存入结果 `data['subtitle_language']`

3. **`core/feishu_fields.py`**: 新增 `LANG_FIELD="语言_Language"` + `LANG_CN/LANG_EN/LANG_DE/LANG_PL/LANG_JA/LANG_KO/LANG_FR/LANG_RU/LANG_ES/LANG_OTHER` + `LANG_OPTIONS` (10选项); `BILI_FIELDS`/`YT_FIELDS` schema 各加该字段 (type=select)

4. **飞书字段自动创建**: `core/feishu_sync.py` 的 `BILI_NEEDED_FIELDS`/`YT_NEEDED_FIELDS` 各加语言字段 → `init_fields` 重新拉取表结构后缺失即用 create field API 创建 (select+10预置选项); `main.py` 的 `sync_results_to_feishu` 前置 `_ensure_language_fields()` 双保险 (human-prescreen 主流程不经过 stage feishu)

5. **`main.py` 集成**: 分类发生在 `fetch_and_process_single` 成功后 (避免同步阶段积压) —— B站固定写 "中文" 不调 AI; YouTube 调 `classify_by_ai(字幕前1500字, title, subtitle_language)`; `sync_results_to_feishu` 写入 `语言_Language` (缺失时兜底补分类)

### 新功能 B: Discord videos-n-streams 自荐采集

6. **新增 `core/discord_video_mark.py`**: 拉取普通文字频道 `<DISCORD_VIDEO_CHANNEL_ID>` 最近 100 条消息 (GET /channels/{id}/messages, 需代理: 自动检测 http 17890/17891/4780 与 socks5 37890; `DISCORD_USER_TOKEN` 从 refactor/.env 读, 回退 add-on/.env); 按 timestamp 过滤最近 7 天; 正则提取 `watch?v=`/`youtu.be/` 链接按视频ID去重; 与 YT 表 Video ID 匹配, 命中且 Human Prescreen 为空 → 写 `Selected` + Notes 追加"来源: Discord videos-n-streams 自荐"; 未命中打印清单 (下次重试); 历史去重 `data/discord_history.json` (key=消息ID), 仿 spider_discord.py 边界停止逻辑; 仅命中消息记入历史; 支持 `--dry-run`

### 新功能 C: 中文精选表高优先级标记

7. **新增 `core/curated_cn_mark.py`**: 读取精选表 (base=FEISHU_BITABLE_TOKEN, table=tbl_CURATED, 135条, 字段 标题/链接/B站分享链接/UP/入选); 从 链接/B站分享链接 提取 `BV[0-9A-Za-z]{10}`; b23.tv 短链 requests allow_redirects 直连解析 (超时10s, 不走代理), 失败按标题去B站表兜底; 与 B站表 BVID 匹配, 命中且 人工初筛 为空 → 写 `高优先级` + 备注追加"来源: 中文精选表"; 支持 `--dry-run`, 输出统计 (命中/未命中/短链解析失败)

### 工作流集成

8. **`core/workflow_orchestrator.py`**: 新增 `--stage discord-video-mark` / `--stage curated-cn-mark` 入口 (支持 `--dry-run`)
9. **`core/run_workflow.py`**: 在 Stage 2 Prescreen 之后、Stage 2.5 ScriptDedup 之前插入 **Stage 2.1** 两个标记阶段 (`discord-video-mark` + `curated-cn-mark`), **失败不阻断**主流程, 仅打印警告

### 验证记录 (2026-09-01)

- 全部 10 个新建/修改文件 ast.parse 语法通过
- `python core/language_classifier.py` 自检 36 项全过
- `--verify` 抽样: `语言_Language` 字段在 YT 表自动创建成功; UUThJAku6oI (Language=en→AI:英文) / PHxKG91QmSQ (Language=N/A→AI:英文) / D9R0tmFX2Ko (字幕代码 de→AI:德文, Language 原值 en) 共 3 条写回, 未批量回填
- `curated_cn_mark --dry-run`: 135 条读取, 直链BV 119 + b23.tv短链解析 16 + 解析失败 0; 命中 119 (91 条可标记 / 28 已有标记跳过), 未命中 16, 未写飞书
- `discord_video_mark --dry-run`: 代理 17891 检测通过, 拉取 100 条消息, 7 天窗口 13 条, 去重 15 个视频; 命中 10 (2 条可标记 / 8 已有标记跳过), 未命中 5, 未写飞书未记历史

### 修改文件清单

| 文件 | 改动 |
|------|------|
| `core/language_classifier.py` | 新增: 语言代码映射 + AI 分类 + 自检 + --verify |
| `core/discord_video_mark.py` | 新增: Discord 自荐采集标记 |
| `core/curated_cn_mark.py` | 新增: 中文精选高优标记 |
| `platforms/youtube_client.py` | 结果携带 subtitle_language (原声/自动两处) |
| `core/feishu_fields.py` | LANG_FIELD + 10 选项常量 + 两表 schema |
| `core/feishu_sync.py` | NEEDED_FIELDS 两表加语言字段 (init 自动创建) |
| `main.py` | fetch 单视频时分类 + 同步写入 语言_Language + 字段保障 |
| `core/workflow_orchestrator.py` | 两个新 stage 入口 |
| `core/run_workflow.py` | Stage 2.1 插入 (非阻断) + 版本显示 |
| `core/version.py` | 5.3.4 → 5.3.5 |
| `README.md` | Stage 2.1 使用说明 + 版本号 |
| `docs/LESSONS.md` | 新增经验条目 |

---

## [2026-08-31] v5.3.4 - YouTube 智能重试 6 项优化

### 优化内容 (core/workflow_orchestrator.py + core/smart_retry.py)

1. **`_retry_youtube_smart` 消除双重请求** (`core/workflow_orchestrator.py`)
   - **问题**: 先探测字幕 (`_fetch_youtube_subtitle_via_transcript_api`) 又 `fetch_and_process_single` 再取一次, 同一视频发两次同样的请求; 且探测成功时 `error_category=""` 落入 UNKNOWN 策略可能被误跳过
   - **修复**: `fetch_and_process_single` 新增 `probe_data` 参数 (`main.py`), 探测拿到字幕时直接复用结果进入评分/AI, 不再二次请求; 探测失败则用错误分类直接决策

2. **SmartRetryEngine 提升到循环外复用** (两处: 完整流程 & 单独重试)
   - **问题**: 引擎在 `while` 循环内每轮重建, IP 健康度滑动窗口每轮重置, `should_pause_batch` 永远基于空窗口
   - **修复**: 引擎在循环外创建一次跨轮复用; 每次请求 `record_result` 到引擎, 窗口跨轮真实累积

3. **`decide()` attempt_count 改为单视频已尝试次数**
   - **问题**: `context={"attempt_count": round_num}` 传的是轮次号, 轮 2 的所有视频都被当成"第 2 次尝试"
   - **修复**: 新增 `video_attempts: Dict[url, count]` 跨轮累计, 按单视频实际尝试次数决策

4. **统一两个重试循环的终止条件**
   - **问题**: 完整流程 `round_num > MAX_RETRY_ROUNDS + 1` 与单独重试 `round_num > MAX_RETRY_ROUNDS` 表达式不一致
   - **修复**: 两处均改用 `retry_round`（已执行重试轮数）, 统一为 `if retry_round > MAX_RETRY_ROUNDS: break`

5. **`_sync_youtube_failed_status` 区分失败状态**
   - **问题**: 所有失败一律写 `failed`, ip_blocked 换代理后无法被单独重试逻辑明确识别
   - **修复**: 视频级问题 (no_subtitle/region_locked/video_unavailable) → `failed` (永久); 可重试问题 (ip_blocked/rate_limit/transient/unknown) → `pending` (保留待重试)

6. **IP_BLOCKED 首次重试等待降为 0**
   - **问题**: 首次重试等 60s, 批量失败时逐视频拖慢整个流程; 探测本身已带 3 次内部重试
   - **修复**: `smart_retry.py` STRATEGY_TABLE `wait_sequence: [60.0] → [0.0]`

7. **修复 TwoPhaseRetryCoordinator 报告死锁** (`core/smart_retry.py`, 自检发现)
   - **问题**: `generate_combined_report()` / `generate_phase_report(phase=2)` 在 `with self._lock` 内再调用 `get_success_after_retry()`/`get_still_failed()`（二者又加同一把非可重入 `threading.Lock`）→ 自检运行到"模拟重试"处永久死锁
   - **修复**: 持锁期间改为内联列表推导统计, 不再嵌套调用加锁 getter

### 修改文件清单

| 文件 | 改动 |
|------|------|
| `core/workflow_orchestrator.py` | 引擎跨轮复用 + 终止条件统一 + attempt_count 按视频 + 失败状态区分 + 探测复用 |
| `core/smart_retry.py` | IP_BLOCKED 等待 60s→0s, attempt_count 语义注释 |
| `main.py` | `fetch_and_process_single(raw_input, probe_data=None)` 支持复用探测结果 |
| `core/version.py` | 5.3.1 → 5.3.4 |
| `core/run_workflow.py` | 版本显示 5.3.1 → 5.3.4 |
| `README.md` | 版本号 5.3.1 → 5.3.4 |

---

## [2026-08-03] v5.3.1 - B站去重与增量过滤修复

### Bug 修复

1. **初筛增量过滤误判** (`core/prescreen.py`)
   - **问题**: 旧逻辑按【发布日期】判断 (`current_date <= last_date` 就跳过), 导致 7/27~8/1 发布、但晚于 8/2 才入库的新视频被误判为"已处理"而跳过, 初筛只处理 14 条
   - **修复**: 改为按【序号】判断 (`current_seq <= last_seq`), 序号是飞书自动编号单调递增, 新入库记录序号必然更大; 修复后处理 76 条 (通过 49, 拒绝 27)

2. **脚本去重入选数量 < 8** (`core/prescreen.py`)
   - **问题**: 10 条视频因 "去重前总数 > 8" 禁止同 up 多视频, 每 up 只留最新 1 条, 最终只入选 4 条 (违反"最终入选 ≥ 8"规则)
   - **修复**: 改为两轮逻辑 — 先按 up 严格去重, 若结果 < 8 则从丢弃池中按日期最新优先补充到 8 条; 修复后 13 条入选

3. **B站字幕获取 UnboundLocalError** (`platforms/bilibili_client.py`)
   - **问题**: `get_video_info_by_api` 网络异常 (10054 连接重置) 时, except 中访问未初始化的 `resp` 变量, 触发 `UnboundLocalError` 掩盖真实错误
   - **修复**: 预初始化 `resp = None` + `fetch()` 整体重试 3 次 (间隔递增); 之前失败的 `BV1N4dWBEEQX` 现已成功获取字幕

4. **orchestrator 误报 FAIL** (`core/workflow_orchestrator.py`)
   - **问题**: 脚本去重阶段 0 条待处理时, stats 无 `success` 键, 总结误报 FAIL
   - **修复**: `stage_script_prescreen` 补充 `stats["success"] = stats.get("failed", 0) == 0`

### 其他

- **删除硅基流动 (SiliconFlow) 配置**: `.env` 仅保留 DeepSeek (`deepseek-v4-flash`, 禁用思考模式)
- **B站全流程重跑**: 118/118 成功, 13 条入选 (满足 ≥ 8 规则), 飞书同步完成
- **新增 `tools/refetch_bilibili_recent.py`**: 以飞书为去重基准补推 7/27 后缺失视频 (86 条)

### 修改文件清单

| 文件 | 改动 |
|------|------|
| `core/prescreen.py` | 增量过滤按序号 + 去重两轮补充逻辑 + run() 空结果补 failed 键 |
| `core/workflow_orchestrator.py` | stage_script_prescreen 补 success 键 |
| `platforms/bilibili_client.py` | resp 预初始化 + fetch 重试 3 次 |
| `core/_deprecated_video_subtitle_censor7.py` | 同步 resp 修复 (废弃模块, main.py 使用 bilibili_client) |
| `core/version.py` | 5.3.0 → 5.3.1 |
| `core/run_workflow.py` | 版本显示 5.3.0 → 5.3.1 |
| `tools/refetch_bilibili_recent.py` | **新增** 飞书为基准的B站补推脚本 |
| `.env` | 删除 SiliconFlow 配置, 仅保留 DeepSeek |

---

## [2026-06-22] v5.1.0 - 语言归一化 (language-normalize)

### 新增功能: 写入飞书时统一语言代码

**问题**: 旧版 fetcher 写入飞书的是 BCP-47 全称 (`en-US`, `de-DE`, `ja-JP`, ...), 导致:
- 同一语言多种变体各占一个 select 选项 (UI 杂乱)
- ScriptPrescreen 豁免判断时, `en-US` 和 `en-GB` 被当成不同语言, 阈值失效

**改造**:
- 新增 `core/language_utils.py`, 提供 `normalize_language()` 函数
- 覆盖 ~80 个 BCP-47 主流变体 → ISO 639-1 二字符代码 (en, de, ja, ...)
- 写入飞书前调用 (在 fetcher `bilibili_youtube_once_for_all_fetcher.py:520`)
- ScriptPrescreen `_language_of()` 读取时也调用, 旧数据自动归一化

### 修改文件清单

| 文件 | 改动 |
|------|------|
| `core/language_utils.py` | **新增** ~190 行, 显式映射表 + normalize_language() |
| `core/prescreen.py` | `_language_of()` 改为调用 `normalize_language()` |
| `core/bilibili_youtube_once_for_all_fetcher.py` | 写入飞书的 Language 字段先归一化 |
| `core/version.py` | 5.0.0 → 5.1.0 |
| `test_prescreen_dryrun.py` | +2 个测试 (Test 6, Test 7) |

### 兼容性

- ✅ 旧 BCP-47 数据自动归一化 (读取时)
- ✅ 新数据写入 2 字符代码 (en/de/zh/...)
- ⚠️ **飞书 select 选项需手动同步**: 现有选项可能是 `en-US`/`de-DE`, 切换后飞书会创建新选项 (`en`/`de`)。建议:
  1. 先在飞书 UI 添加新选项 (en/de/zh/ja/ko/ru/...)
  2. 跑一次数据迁移脚本, 把旧值 `en-US` → `en`
  3. 删除飞书 UI 中的旧选项

### 验证

- 30 个 normalize_language case 全过
- 5 个 BCP-47 旧数据与新格式数据合并到同一语言组, 豁免判断正确

---

## [2026-06-22] v5.0.0 - 人工初筛版 (auto-prescreen)

### 重大变更: 人工只介入最后一步

**问题**: 原 v4.5.0 流程要求人工在飞书完成"人工初筛"标记后才能继续，导致:
- 流程中断，需人工等待
- 标记标准主观，每次不一致
- 大量重复工作

**改造**: 引入 `ScriptPrescreen` 脚本自动去重, 替代人工初筛:
- 同 up/youtuber 多视频 → 只保留最新
- 语言总视频数 ≤ 8 → 允许同 up 多个
- 自动写入 `Human Prescreen` (Selected/Rejected) + `Notes` 字段

**新工作流**:
```
[Stage 1]   Fetcher (B站 + YouTube)
   ↓
[Stage 2]   机器初筛 (D1+D2)
   ↓
[Stage 2.5] 【新】脚本去重 ScriptPrescreen
   ↓
[Stage 3]   AI 审核 + 评分
   ↓
[Stage 4]   写入飞书
   ↓
[人工最后审核] 直接打开飞书查看 AI 审核结果
```

### 修改文件清单 (小修小改)

| 文件 | 改动 |
|------|------|
| `core/prescreen.py` | +250 行: 新增 `ScriptPrescreen` 类 + `_group_by`/`_parse_date_value` helpers |
| `core/workflow_orchestrator.py` | +50 行: 新增 `stage_script_prescreen()` 方法 + `--stage script-prescreen` 选项 |
| `core/version.py` | 4.5.0 → 5.0.0 |
| `run.bat` | 移除"HUMAN PRESCREEN"等待段, 加入 Stage 2.5 脚本去重 |
| `run_from_prescreen.bat` | 标题改 v5.0.0 / Auto-Prescreen Edition |

### 兼容性

- ✅ `Human Prescreen` 字段保留, 零 schema 改动
- ✅ `Notes` 字段复用, 留档挑选原因
- ✅ `--stage human-prescreen` 兼容旧版, 自动调用脚本去重
- ✅ 新增 `--stage script-prescreen` 用于单独跑脚本去重
- ✅ 旧人工标记数据保留 (脚本只处理 `Prescreen=Passed` 且 `Human Prescreen` 为空 的记录)

---

## [2026-06-08] v4.4.0 IP被阻止误报修复 + 智能重试引擎

### 关键 Bug 修复: IP被阻止 误报

**问题**: 脚本运行时大量误报"IP被阻止"，实际是视频无字幕或区域限制。

**根因**: `platforms/youtube_client.py` 使用 `'blocked' in err_str.lower()` 子串匹配：
```python
# 错误代码 (v4.3.0 及之前)
if 'IpBlocked' in err_str or 'blocked' in err_str.lower():
    is_ip_blocked = True  # RequestBlocked 也满足此条件
```

最严重场景：循环遍历 10+ 种语言字幕时，**任意一种**抛 `RequestBlocked` 都把整个视频标记为 IP 阻止，触发级联退避。

**修复**: 接入 `core/smart_retry.py` 的三级分类器：
1. 第一级：异常类型名（最准确）
2. 第二级：字符串精确匹配（处理旧版/包装异常）
3. 第三级：短语整词匹配（`\b...\b` 单词边界，避免子串误判）

关键规则：**绝不单独匹配 "blocked" 子串**，宁可 unknown 也不误判。

**验证**: 12 个测试用例全部通过。

### 新增模块: SmartRetryEngine（智能重试决策器）

**位置**: `core/smart_retry.py`（约 770 行，含自检测试）

**核心能力**:
- 8 种错误类型的精确分类
- 7 种重试策略
- IP 健康度滑动窗口监控（5 分钟窗口）
- 时间预算控制（默认 30 分钟）
- 主动暂停 + 人工介入提示
- 两阶段流程协调器（TwoPhaseRetryCoordinator）

**策略表**:
| 错误类型 | 重试次数 | 等待序列 | 额外动作 |
|---------|---------|---------|---------|
| IP 阻止 | 3 | [60, 120, 300] | 提示换代理 |
| 频率限制 | 2 | [30, 60] | 降低并发 |
| 网络抖动 | 3 | [2, 5, 10] | 同 IP 重试 |
| 无字幕 | 0 | [] | 直接跳过 |
| 区域限制 | 0 | [] | 直接跳过 |
| 飞书限流 | 3 | [5, 15, 45] | 退避+降级 |

### 新增脚本: 两阶段重试工具

**位置**: `tools/retry_failed_youtube_two_phase.py`

**流程**:
```
阶段 1: 第一轮处理（失败即跳过，不立即重试）
   ↓
生成阶段 1 报告 → 写入飞书
   ↓
阶段 2: 智能重试（仅重试失败任务，应用 SmartRetryEngine 决策）
   ↓
追加合并报告 → 写入飞书
```

**特性**:
- 用户随时可看飞书进度（不再一次性同步）
- 视频级问题（无字幕/区域限制）自动跳过，不再触发 IP 阻止级联
- 重试决策有依据（错误分类 + IP 健康度 + 时间预算）
- 防止单次运行耗 2 小时（30 分钟预算上限）
- 报告保存到 `data/retry_reports/yt_retry_phase{1,2}_*.md` 供人工审阅

### 兼容性

- `tools/retry_failed_youtube_v2.py` 也接入新分类器，**仅修复 IP 误报**，保留旧版 RateLimitTracker 逻辑
- 用户可平滑迁移到新脚本，或继续使用旧版（误报已修）

### 影响的文件

| 文件 | 变更类型 |
|------|----------|
| `core/smart_retry.py` | 新建 |
| `platforms/youtube_client.py` | 修改 |
| `tools/retry_failed_youtube_v2.py` | 修改 |
| `tools/retry_failed_youtube_two_phase.py` | 新建 |
| `core/version.py` | 1 (4.3.0 → 4.4.0) |
| `docs/LESSONS.md` | 追加 |

---

## [2026-06-08] v4.8.1 B站AI审核重跑 + YouTube中文字段清理 + init_fields 修复

### B站字幕+AI重跑
- **问题**: 上次 run.bat 后 B站 2026/06/01 后的视频字幕获取全部失败
- **操作**: `run_bili_ai.py` (days_filter=7, 有人工初筛标记)
- **结果**: 8/8 全部成功 (subtitle + AI + 飞书同步)
- **评分**: 4个A优质 + 3个B良好 + 1个C一般

### YouTube 中文字段删除
- **删除**: 标题翻译、初筛状态、字幕获取状态、字幕文本、综合评分、评分等级、AI审核结果、人工确认、备注 (9/9 OK)
- **根因**: `init_fields` 的 `NEEDED_FIELDS` 全为中文名, 导致 YouTube 表也被创建中文字段
- **修复**: `feishu_sync.py` 拆分为 `BILI_NEEDED_FIELDS` (中文) + `YT_NEEDED_FIELDS` (纯英文)
- **新导入**: YT_AI_RESULT_REJECTED, YT_F_NOTES, YT_F_MANUAL_CONFIRM

---

## [2026-06-08] v4.8.0 lark-cli vs OpenAPI 性能实测 + delete schema 修复

### 性能对比实测 (1000条/YouTube表)

| 方案 | 耗时 | 吞吐 | 倍数 |
|------|------|------|------|
| A. lark-cli 串行 1000次 (200限制×5) | 7.0s | 142.6 rec/s | 1x (基线) |
| B. lark-cli batch_create 1000 | ❌ 不支持 (lark-cli 1.0.39 无该命令) | — | — |
| C. OpenAPI batch_create 1000/批 | 2.3s | 430.1 rec/s | 3.0x |
| **D. OpenAPI 1000/批 × 4并发 (4000条)** | **3.6s** | **1125.9 rec/s** | **7.89x** 🏆 |

### 结论

**OpenAPI + 4并发** 是最快方案, 吞吐 1125.9 rec/s, 比 lark-cli 串行快 7.89 倍.
lark-cli 1.0.39 的 `+record-list --limit` 硬编码 200, 没有任何 config 能突破.
lark-cli 1.0.39 根本没有 `+record-batch-create` 命令 (404 page not found).

### 关键 bug 修复: delete_records schema

**修复前**:
```python
body = {"records": [{"record_id": rid} for rid in record_ids]}
# 错! 飞书官方 schema 要求 字符串数组, 不是对象数组
```

**修复后**:
```python
body = {"records": chunk}  # ["recabc", "recxyz", ...]
```

### 清理

- 删除 5081 条 PERF 性能测试残留记录 (YouTube表)
- 删除 perf_compare.py, cleanup_perf.py 临时脚本
- 项目目录恢复干净

---

## [2026-06-08] v4.7.0 Prescreen Status 全量补齐 + 飞书OpenAPI限流速查

### 变更

#### Prescreen Status 全量补齐
- **背景**: 之前 audit 脚本用 lark-cli 拉取只能看到 200 条/页, 误判"164 条 YouTube 缺 Prescreen Status"
- **真相**: 修复 `get_records` 改用飞书 OpenAPI (1000 条/页) 后, 实际全部 25/25 (B站) + 164/164 (YouTube) 视频都已 mark Prescreen Status
- **操作**: 重跑 `Prescreen().run(platform='bilibili', incremental=False)` (508 条成功) + `Prescreen().run(platform='youtube', incremental=True)` (66 条新增) 完成最后兜底

#### 飞书OpenAPI官方限流速查 (来自 TapData 文档)
| API | 限流 | 单批 |
|-----|------|------|
| Batch Insert/Update | 50 req/s | **1000 records/req** |
| Batch Delete | 50 req/s | 500 records/req |
| Batch Query (read) | 20 req/s | 1000 records/req |

- **lark-cli 限制**: `--limit 范围 1-200 (default 100)` 为 hardcoded, 没有任何 config 能突破
- **本项目策略**: 全用 OpenAPI (1000 records/req), `_batch_update_concurrent` 40 并发, 实测 ~150 records/s 写入吞吐

### 工具脚本清理
- 删除: audit_prescreen.py, backfill_yt_prescreen.py, verify_prescreen.py
---

## [2026-06-08] v4.4.0 飞书API全面迁移 (弃用lark-cli) + DatetimeFieldConvFail修复

### 重大变更

#### 弃用lark-cli, 全面改用飞书OpenAPI (直接HTTP调用)
- **原因**: lark-cli `base +record-list` 单次最多200条/页, `base +field-list` 返回的type是字符串与代码内部整数常量不兼容
- **影响**:
  - YouTube表实际有 **3678条**, 但lark-cli只能拉到约600条 (5-6倍数据"丢失")
  - B站表实际有 **613条**, lark-cli只能拉到约300条
- **修复**: 所有读取操作改用飞书OpenAPI + 项目bot的tenant_access_token, 单次1000条/页
- **修改文件**:
  - `core/feishu_sync.py`: `get_fields` / `get_records` / `_get_field_id` 全部改用 requests
  - `core/bilibili_youtube_once_for_all_fetcher.py`: `push_to_bitable` 中拉fields也改用 requests
- **保留lark-cli**: 仅作为agent快速调试工具 (通过`lark-cli base +record-list --limit 5` 快速查看)

#### DatetimeFieldConvFail永久修复
- **根因1**: lark-cli `field-list` 返回的type是字符串 (e.g. `"datetime"`), 而 `push_to_bitable` 内部用 `ft == 5` 整数比较
  - **结果**: datetime分支从未被执行, 所有日期字段被当作文本字符串发送, 飞书返回 `DatetimeFieldConvFail`, **整个batch_create事务回滚**
  - **影响**: 上次run.bat后, 新抓的153条YouTube数据**全部未进入飞书** (Excel写入成功, 飞书回滚)
- **根因2**: 即使识别为datetime, Excel的日期是GMT+8本地时间, 旧代码无时区解析导致毫秒值偏移
- **修复**:
  - 改用OpenAPI (type=整数5) 后, 字符串→整数映射加整数→整数fallback
  - 新增 `self_dt_to_gmt8_ms()` 顶级辅助函数, 强制按GMT+8解析再转UTC毫秒
  - 非法值(0/负数/NaT/1970)统一 `continue` 跳过该字段, 飞书保留空值
- **验证**: 11项边界用例全部通过 (字符串日期/秒级时间戳/毫秒时间戳/0/负数/空/None/naive datetime/pd.Timestamp)

### 工具脚本清理
- 删除了 8 个临时诊断脚本: test_push_one.py, debug_*.py, dump_*.py, check_*.py, audit_channels.py
- 项目目录恢复干净, 无遗留

### 受影响用户
- 运行 `run.bat` 后YouTube表数据丢失的用户 (已被自动回填 385+273 条)

---

## [2026-05-26] v4.3.0 飞书字段SSOT重构 + 字段值回归Bug修复

### 新增功能

#### feishu_fields.py - 飞书字段名+字段值唯一数据源 (SSOT)
- 集中定义所有飞书字段名常量 (BILI_F_*/YT_F_*)
- 集中定义所有飞书字段值常量 (BILI_AI_RESULT_PASS/BILI_SUBTITLE_SUCCESS 等)
- 提供值映射函数: `map_censor_result_to_bili()`, `map_censor_result_to_yt()`, `map_subtitle_status_to_bili()`, `map_subtitle_status_to_yt()`
- PRIORITY_WEIGHT 同时包含中英文键，修复YouTube标签权重为0的问题

#### version.py - 版本号集中管理
- `__version__ = "4.3.0"`，所有模块从此文件获取版本号

### BUG修复

#### 字段值回归Bug (P0 - 数据写入失败)
- **现象**: B站"AI审核结果"字段被写入英文值"safe"/"unsafe"，飞书select字段静默失败（值不更新但API不报错）
- **根因**: main.py 直接写入"safe"/"unsafe"而非中文"通过"/"不通过"，B站select字段选项只有中文
- **修复**: 所有写入点改用 `map_censor_result_to_bili()` / `map_subtitle_status_to_bili()` 映射函数
- **影响文件**: main.py, sync_pending_results.py, core/workflow_orchestrator.py, tools/sync_report_to_feishu.py, tools/retry_failed_youtube_v2.py, patch_feishu_from_report.py

#### 接口迁移Bug
- **现象**: run_bili_ai.py 期望 `load_videos_from_feishu` 返回 `(priority, forced)` 但实际返回 `(bili, youtube)`
- **修复**: 解构改为 `(bili_videos, _) = load_videos_from_feishu(days_filter=7)`

#### 优先级权重语言不匹配
- **现象**: PRIORITY_WEIGHT 只有中文键"高优先级"，YouTube英文标签"High Priority"权重为0
- **修复**: PRIORITY_WEIGHT 同时包含中英文键

#### workflow_orchestrator.py B站审核通过判断错误
- **现象**: `passed_values = ["通过", "safe"]`，"safe"不应出现在B站判断中
- **修复**: 改为 `[BILI_AI_RESULT_PASS]`（仅"通过"）

#### reporter.py 评级阈值对齐
- **修复**: >=12→>=10, 9-12→8-10, 6-9→6-8，与实际评分范围对齐

#### 裸except修复
- **修复文件**: core/feishu_sync.py, core/prescreen.py, core/prescreen_youtube.py, core/bilibili_youtube_once_for_all_fetcher.py, tools/retry_failed_youtube_v2.py
- **修复**: 所有裸 `except:` 改为具体异常类型

### 废弃与清理

- `core/video_subtitle_censor7.py` → `core/_deprecated_video_subtitle_censor7.py`（添加DeprecationWarning）
- 删除12个调试/检查脚本（_debug_*.py, debug_*.py, check_*.py）
- run_bili_ai.py 导入从 video_subtitle_censor7 迁移至 main.py

### 验证结果

- 全项目编译检查: ALL COMPILE OK
- feishu_fields.py 断言测试: 25+ assertions ALL PASSED
- 残留硬编码grep: 确认无活跃代码中残留硬编码字段值
- 飞书B站表历史脏数据检查: 0条"safe"/"unsafe"/"success"记录，无需修复

---

## [2026-04-14] v3.4.0 新评分系统

### 新增功能

#### D4 AI吸引力评分维度
- 新增 `calc_d4_appeal_score()` 函数，将AI评分(0-100)映射到0-10分
- 评分公式调整：
  - B站视频：`(D1 + D2 + D3 + D4) / 4`
  - YouTube视频：`(D1 + D4) / 2`（跳过D2/D3）
- 解决YouTube视频全部0分的问题

#### D3互动得分阈值调整
- 原来>5%就得满分，现在需要>10%
- 增加评分区分度：
  - >10%: 10分（极高）
  - 7-10%: 9分（高）
  - 5-7%: 7分（中高）
  - 3-5%: 5分（中等）
  - 1-3%: 3分（低）
  - <1%: 1分（极低）

#### M1新人乘数调整
- 降低乘数幅度，避免分数膨胀：
  - 0次：1.5x → 1.2x
  - 1次：1.3x → 1.15x
  - 2次：1.1x（不变）
  - 3次：1.0x → 1.05x
  - 4次：0.9x → 1.0x
  - 5次：新增0.95x
  - >5次：0.8x → 0.9x

#### 评分等级调整
- A级：>=12.0 → >=10.0
- B级：9.0-11.9 → 8.0-9.9
- C级：6.0-8.9 → 6.0-7.9
- D级：<6.0 → 4.0-5.9
- E级：新增 <4.0（拒绝）

### BUG修复

#### 问题5: 飞书同步并发过高导致限流
**现象**:
```
[WARN] 同步失败: Data not ready, please try again later (code=1254607)
```

**根因**:
20个并发worker同时调用飞书API，每个worker都调用`get_records`获取全量记录再搜索，导致大量API请求触发限流。

**修复文件**: `refactor/main.py`

**修复内容**:
1. 并发数从20降到5
2. 预加载B站和YouTube两张表的记录，建立`video_id → record_id`索引
3. 同步时直接用`update_record`更新，避免重复获取全表
4. 添加3次指数退避重试

#### 问题6: AI审查返回[ERR]时未正确处理
**现象**:
AI返回`[ERR] 模型 xxx 不支持此请求: HTTP 400`，但视频被标记为pending而非失败。

**根因**:
`process_ai_results`函数未检查`censor_result`和`appeal_result`是否以`[ERR]`开头。

**修复文件**: `refactor/video_processor.py`

**修复内容**:
```python
if censor_val.startswith('[ERR]') or appeal_val.startswith('[ERR]'):
    result_dict['success'] = False
    result_dict['error'] = '; '.join(err_parts)
    continue  # 跳过当前视频，继续处理后续
```

#### 问题7: process_ai_results中return导致后续视频被跳过
**现象**:
部分B站视频error为空，但被标记为失败。

**根因**:
在检测到`[ERR]`时使用`return`退出函数，导致后续所有视频的AI结果都不会被处理。

**修复文件**: `refactor/video_processor.py`

**修复内容**:
将`return`改为`continue`，只跳过当前失败视频。

#### 问题8: fetch阶段日志误报FAIL
**现象**:
有字幕的视频在fetch阶段显示`[FAIL]`，但实际字幕获取成功。

**根因**:
`submit_ai_tasks`返回的`result_info`初始`success=False`（等AI完成后才更新），导致误判。

**修复文件**: `refactor/main.py`

**修复内容**:
增加`_censor_future`检查，区分`[PENDING]`（AI已提交）和`[FAIL]`（真正失败）。

### AI模型配置优化

移除7B模型（JSON格式不稳定67%成功率），推荐配置：
- 便宜模型：`Qwen/Qwen2.5-14B-Instruct, THUDM/GLM-4-9B-0414`
- 强力模型：`Qwen/Qwen2.5-32B-Instruct`

---

## [2026-04-13] 修复飞书 API 调用问题

### 问题 1: JSON 解析错误导致脚本崩溃

**现象**:
```
18:56:48 [ERROR] 字幕处理失败: JSON解析错误 - Extra data: line 1 column 5 (char 4)
```

**根因**:
`feishu_sync.py` 的 `get_records()` 方法在调用飞书 API 时，没有对返回数据进行 JSON 解析错误处理。当飞书返回非 JSON 格式数据（如网络错误、代理错误、网关错误）时，脚本直接崩溃。

**修复文件**: `refactor/core/feishu_sync.py`

**修复内容**:

1. 添加 `json` 模块导入（第 22 行）:
```python
# 修改前
import os
import requests
from typing import Dict, List, Any, Optional

# 修改后
import os
import json
import requests
from typing import Dict, List, Any, Optional
```

2. 重写 `get_records()` 方法（第 124-163 行）:
```python
# 修改前
res = requests.get(url, headers=headers, params=params).json()  # 无错误处理

# 修改后
try:
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    raw_text = resp.text
    res = resp.json()
except json.JSONDecodeError as e:
    print(f"[ERROR] 飞书API返回非JSON格式数据")
    print(f"[ERROR] JSON解析错误: {e}")
    print(f"[ERROR] 原始响应内容 (前500字符): {raw_text[:500]}")
    print(f"[ERROR] 请求URL: {url}")
    print(f"[ERROR] 请求参数: {params}")
    break
except requests.exceptions.RequestException as e:
    print(f"[ERROR] 飞书API请求失败: {e}")
    break
```

---

### 问题 2: 废弃代码残留

**现象**:
`video_urls.txt` 相关代码已被废弃但未清理，存在潜在隐患。

**修复文件**: `refactor/core/video_subtitle_censor7.py`

**修复内容**:

1. 移除 `VIDEO_LIST_FILE` 导入（第 37-45 行）
2. 删除 `filter_safe_urls()` 函数（原第 1425-1462 行，共 38 行）
3. 删除 `load_video_urls()` 函数（原第 1531-1551 行，共 21 行）
4. 移除 `main()` 中对 `filter_safe_urls()` 的调用

---

## [2026-04-13] 修复 bitable_token 未设置问题

### 问题 3: 飞书 API 返回 404 错误

**现象**:
```
[ERROR] 飞书API返回非JSON格式数据
[ERROR] JSON解析错误: Extra data: line 1 column 5 (char 4)
[ERROR] 原始响应内容 (前500字符): 404 page not found
[ERROR] 请求URL: https://open.feishu.cn/open-apis/bitable/v1/apps//tables/tbl_BILIBILI/records
```

**根因**:
URL 中 `apps//tables` 有两个斜杠，说明 `self.bitable_token` 是空字符串。`get_videos_for_human_prescreen()` 方法没有调用 `set_bitable_token()` 设置多维表格 token。

**修复文件**: `refactor/core/feishu_sync.py`

**修复内容**:

在 `get_videos_for_human_prescreen()` 方法开头添加 `bitable_token` 设置:

```python
# 修改前
def get_videos_for_human_prescreen(
    self,
    platform: str,
    days_filter: int = None
) -> Dict[str, List[Dict]]:
    from config import CONFIG
    if days_filter is None:
        days_filter = CONFIG.get("human_prescreen_days_filter", 7)

    table_id = BILI_TABLE_ID if platform.lower() == "bilibili" else YT_TABLE_ID
    # ... 直接调用 get_records()，但 bitable_token 为空

# 修改后
def get_videos_for_human_prescreen(
    self,
    platform: str,
    days_filter: int = None
) -> Dict[str, List[Dict]]:
    from config import CONFIG
    if days_filter is None:
        days_filter = CONFIG.get("human_prescreen_days_filter", 7)

    # 设置多维表格 token
    self.set_bitable_token(BILI_BITABLE_TOKEN)

    table_id = BILI_TABLE_ID if platform.lower() == "bilibili" else YT_TABLE_ID
    # ... 继续执行
```

---

## [2026-04-13] 修复报告错误信息问题

### 问题 4: 报告错误字段显示"AI评分解析失败"但实际AI评分已成功

**现象**:
报告中显示：
- `**AI吸引力评分**: 85分` - AI 评分解析成功
- `**状态**: OK 成功` - `success` 是 `True`
- `- **错误**: AI评分解析失败` - `error` 字段有值

**根因**:
`process_ai_results()` 函数中，当 AI 评分解析成功时，`success` 被设为 `True`，但 `error` 字段没有被清除（保留之前可能设置的错误值）。

**修复文件**: `refactor/core/video_subtitle_censor7.py`

**修复内容**:

在 `process_ai_results()` 函数中，AI 评分解析成功时清除 `error` 字段:

```python
# 修改前
if appeal_desc == "解析失败":
    result_dict['success'] = False
    result_dict['error'] = f"AI评分解析失败"
else:
    result_dict['success'] = True

# 修改后
if appeal_desc == "解析失败":
    result_dict['success'] = False
    result_dict['error'] = f"AI评分解析失败"
else:
    result_dict['success'] = True
    result_dict['error'] = ''
```

---

## 经验教训

1. **API 调用必须有错误处理**: 所有外部 API 调用都应该包含 try-except 块，捕获网络异常和 JSON 解析错误。

2. **打印原始响应内容**: 当 API 返回异常时，打印原始响应内容可以帮助快速定位问题。

3. **依赖注入模式**: 类的必要属性（如 `bitable_token`）应该在构造函数中初始化，或者在每个使用它的方法开头检查是否已设置。

4. **及时清理废弃代码**: 废弃代码会引入混淆和潜在 bug，应该及时删除。

---

*最后更新: 2026-04-13*
