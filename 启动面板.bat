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
rem ── 启动前预检：面板窗口要 WebView2，机器人读提示数字要 Tesseract-OCR ──
rem    缺组件时双击 exe 会「没反应」，这里先把原因和下载地址讲清楚，别让人干等。
set "MISS="

rem WebView2 运行时（微软 Edge WebView2 Evergreen，面板窗口靠它渲染）
set "WV2="
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul
if not errorlevel 1 set "WV2=1"
if not defined WV2 (
  reg query "HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul
  if not errorlevel 1 set "WV2=1"
)
if not defined WV2 (
  echo [提示] 没检测到 WebView2 运行时，面板窗口可能打不开（双击后没反应）。
  echo        装一下就好：https://go.microsoft.com/fwlink/p/?LinkId=2124703
  echo        Win11 和较新的 Win10 一般自带；没自带时装上面这个即可。
  set "MISS=1"
)

rem Tesseract-OCR（识别提示数字用；缺了会表现为「点了没反应 / 一直读不出来」）
set "TESS="
if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" set "TESS=1"
if not defined TESS if exist "%ProgramFiles(x86)%\Tesseract-OCR\tesseract.exe" set "TESS=1"
if not defined TESS if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" set "TESS=1"
if not defined TESS for /f "delims=" %%P in ('where tesseract 2^>nul') do set "TESS=1"
if not defined TESS (
  echo [提示] 没检测到 Tesseract-OCR，机器人无法识别提示数字（会一直读不出来）。
  echo        下载安装（免费，约 1 分钟，一路「下一步」即可，不用手动配 PATH）：
  echo        https://github.com/UB-Mannheim/tesseract/wiki
  set "MISS=1"
)

if defined MISS (
  echo.
  echo 上面缺的组件装好后再用；现在按任意键先开面板，或直接关掉本窗口去安装。
  pause >nul
)

rem 面板本体不在就直接说清楚，别让“双击了没反应”
if not exist "MikuPanel.exe" (
  echo.
  echo [错误] 没找到 MikuPanel.exe，请把本脚本放回程序文件夹再运行。
  echo        如果文件不见了，多半是被杀毒软件删了，重新解压一次即可。
  pause
  exit /b 1
)

echo 正在启动面板…
start "" "MikuPanel.exe"
exit /b 0

