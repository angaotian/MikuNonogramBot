@echo off

chcp 936 >nul

cd /d "%~dp0"

title 数织自动闯关（懒人版）



where python >nul 2>nul
if errorlevel 1 (
  echo [提示] 没有检测到 Python，这个脚本跑不起来。
  echo        只是想玩的话：双击「启动面板.bat」即可，面板自带运行环境、不需要 Python。
  echo        只有想改脚本、重新打包给别人，才需要装 Python 3（安装时勾选 Add Python to PATH）。
  pause
  exit /b 1
)



python -c "import PIL, numpy, pytesseract" >nul 2>nul

if errorlevel 1 (

  echo 首次运行，正在安装所需库，稍等片刻...

  python -m pip install -r requirements.txt

)



rem 主脚本不在就没必要往下跑，先把话说清楚
if not exist "miku_logic_paint_bot.py" (
  echo [错误] 没找到 miku_logic_paint_bot.py，请把本脚本放在程序文件夹里运行。
  pause
  exit /b 1
)

rem ==== 可选：校准模式  启动.bat 校准 ====

if /i "%~1"=="校准" goto calibrate

if /i "%~1"=="calibrate" goto calibrate

if /i "%~1"=="-c" goto calibrate



if not "%~1"=="" goto run_args



rem 双击即开跑：棋盘尺寸自动识别，5x5 ~ 20x20 都支持

goto run_loop



:calibrate

echo ============================================================

echo  先让游戏停在一关的棋盘界面，再按屏幕提示操作

echo ============================================================

python miku_logic_paint_bot.py --calibrate

goto end



:run_loop

python miku_logic_paint_bot.py --loop

goto end



:run_args

python miku_logic_paint_bot.py %*



:end

echo.

pause

