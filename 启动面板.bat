@echo off
chcp 936 >nul
cd /d "%~dp0"
title Miku 数织自动闯关 · 控制面板

rem 本机 Python 默认 UTF-8 输出，会把中文写成乱码打到 936 控制台；这里钉死成 GBK
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk"

where python >nul 2>nul
if errorlevel 1 (
  echo [提示] 没检测到 Python：跳过一致性自检，直接开面板。
  echo        面板本身不需要 Python，但改了主脚本/界面之后需要 Python 重新打包。
  goto launch
)

echo 校验面板内容与工作区源码是否一致…
python tools\panel_verify.py --quiet
if errorlevel 2 set ENVPROB=1
if errorlevel 1 (
  echo.
  echo 检测到不一致或环境隐患，正在按真源重新打包面板（旧版会自动备份）…
  python tools\panel_build.py
  if errorlevel 1 (
    echo.
    echo [错误] 重新打包失败。为避免跑错版本，这次先不开面板。
    echo        原因见上面的输出；也可以手动跑 python tools\panel_build.py 看细节。
    pause
    exit /b 1
  )
)

if defined ENVPROB (
  echo.
  echo [警告] 运行环境有上面列出的问题：面板能开，但可能读不了题（比如缺 Tesseract）。
  echo        回车继续开面板，或先关掉本窗口去处理。
  pause >nul
)

:launch
echo 正在启动面板…
start "" "MikuPanel.exe"
exit /b 0
