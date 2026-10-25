# Video Pipeline — YouTube 视频 + 字幕下载工具

用于游戏社区内容运营的 YouTube 视频 + 字幕下载工具。

## 目录结构

```
video_pipeline/
├── main.py                 # 下载工具入口（单视频 + 批量）
├── requirements.txt        # Python 依赖
├── cookies.txt             # YouTube cookies（自动从 youtube_cookies.json 转换）
├── .env                    # AI API 密钥配置（如需翻译功能可复用）
├── .gitignore
├── README.md               # 本文件
├── archive/                # 历史归档（切片/高光检测等旧版工作流备份）
│   ├── main.py             # 旧版完整管线（含切片+翻译）
│   ├── highlight_detector.py
│   ├── subtitle_generator.py
│   ├── requirements.txt    # 旧版依赖
│   └── README.md           # 旧版文档
└── output/
    └── raw/                # 下载的视频 + 字幕文件
```

## 依赖

| 类型 | 包名 | 用途 |
|------|------|------|
| Python | `yt-dlp` | YouTube 视频/字幕下载 |

### 安装

```bash
pip install -r requirements.txt
```

## 用法

### 单视频下载

```bash
# 默认下载英文 + 简体中文自动字幕
python main.py "https://www.youtube.com/watch?v=CQJKxkozQfA"

# 指定字幕语言
python main.py "https://www.youtube.com/watch?v=xxx" --sub-lang en

# 多语言字幕
python main.py "https://www.youtube.com/watch?v=xxx" --sub-lang en,zh-Hans,ja
```

### 仅下载字幕（不下载视频）

```bash
# 只下载简体中文字幕，节省带宽
python main.py "https://www.youtube.com/watch?v=xxx" --subs-only --sub-lang zh-Hans

# 下载英文 + 中文双字幕
python main.py "https://www.youtube.com/watch?v=xxx" --subs-only --sub-lang en,zh-Hans
```

### 批量下载

创建 `urls.txt`，每行一个 YouTube URL（空行和 `#` 注释行忽略）：

```text
# 游戏教程合集
https://www.youtube.com/watch?v=CQJKxkozQfA
https://www.youtube.com/watch?v=xxx
https://www.youtube.com/watch?v=yyy
```

```bash
# 批量下载视频 + 字幕
python main.py --batch urls.txt

# 批量仅下载字幕
python main.py --batch urls.txt --subs-only --sub-lang en,zh-Hans

# 自定义下载间隔（默认 2 秒）
python main.py --batch urls.txt --delay 5
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `url` | YouTube 视频 URL | 单个视频模式必填 |
| `--sub-lang` | 字幕语言代码，逗号分隔 | `en,zh-Hans` |
| `--subs-only` | 仅下载字幕，不下载视频 | `false` |
| `--batch` | 批量模式：URL 列表文件路径 | — |
| `--delay` | 批量下载间隔（秒） | `2.0` |

## 输出说明

所有下载产物统一存放在 `output/raw/` 目录下：

```
output/raw/
├── 视频标题_视频ID.webm         # 视频文件
├── 视频标题_视频ID.en.srt       # 英文字幕
└── 视频标题_视频ID.zh-Hans.srt  # 简体中文字幕
```

## Cookie 配置

工具自动从上级目录 `refactor/youtube_cookies.json` 读取 YouTube cookies 并转换为 Netscape 格式。

**无需手动操作** — 每次运行 `main.py` 时自动转换。如果 cookies 过期，重新导出 `youtube_cookies.json` 覆盖即可。

## 常见问题

**Q: 下载提示 cookie 过期？**
A: yt-dlp 会显示 WARNING 但通常不影响下载（会回退到 android VR player API）。如需更新，重新导出 `youtube_cookies.json`。

**Q: 如何下载特定语言的字幕？**
A: 使用 `--sub-lang` 参数。常见语言代码：`en`(英文) `zh-Hans`(简体中文) `zh-Hant`(繁体中文) `ja`(日文) `ko`(韩文) `fr`(法文) `de`(德文)。

**Q: 之前的切片/高光检测功能去哪了？**
A: 已归档到 `archive/` 子文件夹。完整代码和文档均保留，如需恢复可从中取出。

**Q: 下载的视频格式是什么？**
A: 默认下载最佳视频+音频格式（通常是 webm 或 mp4）。如需强制 mp4 格式，可修改 `main.py` 中 `-f` 参数为 `"bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]"`。