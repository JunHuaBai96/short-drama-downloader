@echo off
rem 构建 YCDramaDownloader 单文件 exe（内嵌 ffmpeg）
rem 用法：先设置 FF 为 imageio_ffmpeg 自带的 ffmpeg.exe 完整路径
cd /d "%~dp0.."
pyinstaller --onefile --name YCDramaDownloader ^
  --add-binary "%FF%;." ^
  --clean --noconfirm ^
  src/yc_tool_server.py
