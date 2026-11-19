# 🎬 Short Drama Downloader

**多平台短剧一键下载与无损合成工具**——带浏览器操作界面，粘贴播放页链接或剧名，自动下载全部剧集、逐集校验防漏集，并合成为可连续播放的全剧成片。

## ✨ 功能

- **多平台识别**：自动识别 13 个短剧剧场域名（YourChannel / NetShort / Narto Drama / ShortMax / DramaBox / ReelShort / StardustTV / KalosTV / GoodShort / FlickReels / MoboReels / StoryReel 等）
- **下载适配**：YourChannel（API 直连）、NetShort（播放页链接，签名直链由本机 Edge 解析）、**Narto Drama 多剧场**（播放页链接直连，免登录免浏览器，覆盖 BibiShort 等 35+ 片源剧场）；其余平台持续适配中
- **四种输入**：剧名（实时全局检索）/ 播放页 URL / playlet_id / 北斗任务链接（`inbeidou.cn/task-detail?task_id=…` 自动定位取剧）
- **实时检索**：搜索时实时拉取北斗任务池全量窗口（8 平台 × 各 200 条，含 dramawave）+ 11 平台上新面板索引 + **Narto Drama 免登录站内搜索**（`/search?q=` 通道，候选可一键「用此剧」）
- **原档下载**：优先原档 MP4（720p/1080p，按片源实际分辨率），失败自动回落 HLS 流；字幕为画面烧录式，随视频获取
- **防漏集校验**：逐集体积/时长/可解码校验，并与官方总集数比对
- **无损合成**：concat 流复制拼接（零转码损失），合成前自动归一化各集时间戳（片源视频轨普遍带 +0.16s 头部偏移 + 片尾静音填充，直接拼接会在每个接缝产生约 0.19 秒的可见卡顿，工具已修复），成片 `FULL.mp4` 与分集保存在同一输出目录
- **任务池管理**：一键清除本地缓存（下次搜索自动重新拉取实时数据）
- **日志**：实时界面日志 + 自动落盘 `logs/`，一键清空；导出路径可填写或点「浏览」选择

## 📦 安装

1. 从 [Releases](../../releases) 下载 `ShortDramaDownloader.exe`（单文件，已内置 ffmpeg，无需装 Python）
2. 双击运行，浏览器会自动打开控制台（`http://127.0.0.1:8765`）
3. 剧名搜索为实时全局检索，无需手动刷新任务池索引

> NetShort 下载需要本机 **Microsoft Edge**（Windows 10/11 自带，无需额外安装）。
> 从源码运行：`pip install requests websocket-client imageio-ffmpeg` → `python src/yc_tool_server.py`

## 🚀 使用

1. **输入剧目**：
   - YourChannel：粘贴播放页链接（`yourchannel.ai/pagesSub/player?id=…`）或北斗任务链接
   - NetShort：粘贴播放页链接（`web.netshort.com/episode/<slug>-<id>`），工具自动枚举全部分集
   - Narto Drama：粘贴播放页链接（`narto-drama.com/detail/watch/<slug>`，含 BibiShort 等片源的 `/search/import` 导入链接），工具自动枚举全部分集并直链下载
   - **北斗非 YC 源任务自动定位**：粘贴 dramawave / dramabox 等非 YC 平台的任务详情链接，工具自动按剧名在 Narto Drama 定位对应片源并就绪下载（例：dramawave task 1062568559 → 《Love Games with My Dragon Boss》51 集）
   - **直链 CDN 拦截自动降级**：Narto 部分片源（bibishort 等，托管于 hakunaymatata/CloudFront）直链被 CDN Function 拦截（实测 428 Precondition Required），工具自动探测并降级为站方边缘代理流（`force=1&force_edge=1&no_cache=1`，与站点播放器自身降级逻辑一致）
   - 剧名搜索为实时检索北斗任务池 + 面板索引（新上架剧可能不在池中，此时用链接直连）
2. **开始下载**：输出目录支持「浏览」选择，分集与合成成片均保存在此目录，进度实时显示
3. **合成**：下载校验通过后点「合成为完整视频」，成片 `FULL.mp4` 与分集在同一目录
4. **导出日志**：路径可填写或点「浏览」选择，留空默认存桌面

## ⚠️ 说明

- YourChannel 官网搜索接口已停用（实测任何参数均返回「短剧未找到」），剧名检索走北斗智影（inbeidou.cn）任务池
- NetShort 接口对请求/响应做了加密处理，工具不逆向加密，而是用本机 Edge 以真实浏览器取播放器实际请求的签名直链（有效期数小时）
- Narto Drama 为多剧场聚合站（BibiShort / MicroDrama 等 35+ 片源）：分集经其边缘域 refresh-source 端点实时获取，免登录。片源直链分两类：BibiShort 等源为 CDN 直链 MP4（无损转存）；dramawave 等源为站方代理 HLS 流（ffmpeg 无损 remux，`aac_adtstoasc`）
- Narto 站内搜索的 AJAX 接口需登录（实测 `login_required:true`），工具改走免登录的 `/search?q=` SSR 渲染页解析结果（JSON-LD 清单）
- 成片为片源原始编码（H.264/HEVC），主流播放器直接可播；部分旧版剪辑软件需转码
- 仅支持上述平台的公开剧集；字幕为片源烧录式，工具不生成独立字幕文件
- 请遵守各平台服务条款，下载内容仅限授权范围内的内部素材使用

## 🛠️ 构建 exe

```
pip install pyinstaller imageio-ffmpeg requests websocket-client
pyinstaller --onefile --name ShortDramaDownloader ^
  --hidden-import websocket ^
  --add-binary "<imageio_ffmpeg 路径>\ffmpeg-win-x86_64-v7.1.exe;." ^
  src/yc_tool_server.py
```

## 🧪 测试

```
python tests_headless.py   # 无头管线测试（搜索/获取/下载/校验/合成）
python tests_regress.py    # 回归验证（播放/检索/任务链接定位/任务池刷新清除/合成）
python tests_ui.py         # 界面端点验证
python verify_final.py     # 双平台全量验证（含下载、合成接缝连续性检查）
```

## 更新记录

- **v2.0.1**：修复合成视频卡顿——逐集归一化视频轨起始时间戳并截除片尾静音填充（均为流拷贝零转码），拼接点最大帧跳变从 0.194s 降至 ≤0.046s，成片帧率恢复精确 30fps
- **v2.0.0**：多平台升级（YourChannel + NetShort 下载、11 平台识别、跨平台面板索引、实时全局检索）

## License

MIT — 详见 [LICENSE](LICENSE)
