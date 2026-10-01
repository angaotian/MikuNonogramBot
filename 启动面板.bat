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

rem ── 发布包环境（下载解压出来的那份）没有 .pylibs、机器上通常也没装 PyInstaller：
rem    读不了 exe 内部 → 既不能自检也不能重打包。这种环境下跳过自检直接开面板：
rem    包里的 exe 与主脚本本来就是一起打出来的，不存在「跑错版本」。
rem    （2026-10-01 修：以前这里会报「不一致 → 重打包失败 → 拒绝开面板」，
rem      下载包的人根本打不开面板。）
set "HAS_PYI="
if exist ".pylibs\PyInstaller" set "HAS_PYI=1"
if defined HAS_PYI goto has_pyi
python -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('PyInstaller') else 1)" >nul 2>nul
if errorlevel 1 goto no_pyi
set "HAS_PYI=1"
goto has_pyi

:no_pyi
echo [提示] 这台机器没有 PyInstaller（下载来的发布包就是这种环境）：
echo        跳过一致性自检，直接开面板。面板本身不需要它，
echo        只有改了主脚本或界面、要重新打包时才需要。
goto launch

:has_pyi
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
