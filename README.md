# 🎬 YC Drama Downloader

**YourChannel 短剧一键下载与无损合成工具**——带浏览器操作界面，输入剧名、播放页链接或北斗任务链接，自动下载全部剧集、逐集校验防漏集，并合成为可连续播放的完整视频。

## ✨ 功能

- **四种输入**：剧名（实时全局检索）/ 播放页 URL / playlet_id（直连 YourChannel API）/ 北斗任务链接（`inbeidou.cn/task-detail?task_id=…` 自动定位取剧）
- **实时全局检索**：剧名搜索时实时拉取北斗任务池全量窗口（7 平台 × 各 200 条），不依赖本地缓存；后台说明：YourChannel 官网搜索接口已停用（实测），北斗任务池是唯一可检索数据源
- **原档下载**：优先 720p/1080p 原档 MP4，失败自动回落 HLS 流；字幕为画面烧录式，随视频一并获取
- **防漏集校验**：逐集体积/时长/可解码校验，并与官方总集数比对
- **无损合成**：concat 流复制拼接（零转码损失），成片 `FULL.mp4` 与分集保存在同一输出目录
- **任务池管理**：一键清除本地缓存（下次搜索自动重新拉取实时数据）
- **日志**：实时界面日志 + 自动落盘 `logs/`，一键清空；导出路径可填写或点「浏览」选择

## 📦 安装

1. 从 [Releases](../../releases) 下载 `YCDramaDownloader.exe`（单文件，已内置 ffmpeg，无需装 Python）
2. 双击运行，浏览器会自动打开控制台（`http://127.0.0.1:8765`）
3. 剧名搜索为实时全局检索，无需手动刷新任务池索引

> 从源码运行：`pip install requests` → `python src/yc_tool_server.py`

## 🚀 使用

1. **输入剧目**：粘贴播放页链接（如 `https://yourchannel.ai/pagesSub/player?id=380651099712913408`）或北斗任务链接（如 `https://inbeidou.cn/task-detail?task_id=1093640043&app_id=yourchannel_drama&task_type=1`）最稳妥；剧名搜索为实时全局检索北斗任务池（每平台仅暴露 200 条、收入排序为平台硬限制，新上架剧可能不在池中，此时用链接直连）
2. **开始下载**：输出目录支持「浏览」选择，分集与合成成片均保存在此目录，进度实时显示
3. **合成**：下载校验通过后点「合成为完整视频」，成片 `FULL.mp4` 与分集在同一目录
4. **导出日志**：路径可填写或点「浏览」选择，留空默认存桌面

## ⚠️ 说明

- YourChannel 官网**不提供按剧名搜索接口**（实测），剧名检索走北斗智影（inbeidou.cn）任务池缓存
- 成片为片源原始编码（HEVC/H.264），主流播放器直接可播；部分旧版剪辑软件需转码
- 仅支持 YourChannel 片源剧集；字幕为片源烧录式，工具不生成独立字幕文件
- 请遵守各平台服务条款，下载内容仅限个人学习研究使用

## 🛠️ 构建 exe

```
pip install pyinstaller imageio-ffmpeg requests
pyinstaller --onefile --name YCDramaDownloader ^
  --add-binary "<imageio_ffmpeg 路径>\ffmpeg-win-x86_64-v7.1.exe;." ^
  src/yc_tool_server.py
```

## License

MIT — 详见 [LICENSE](LICENSE)

## 🧪 测试

```
python tests_headless.py   # 无头管线测试（搜索/获取/下载/校验/合成）
python tests_regress.py    # 回归验证（播放/检索/任务链接定位/任务池刷新清除/1080p合成）
```
