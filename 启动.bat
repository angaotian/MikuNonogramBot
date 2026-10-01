@echo off
chcp 936 >nul
cd /d "%~dp0"
title 数织自动闯关（懒人版）

where python >nul 2>nul
if errorlevel 1 (
  echo [提示] 没有检测到 python，请先安装 Python 3 并勾选 Add Python to PATH
  pause
  exit /b 1
)

python -c "import PIL, numpy, pytesseract" >nul 2>nul
if errorlevel 1 (
  echo 首次运行，正在安装所需库，稍等片刻...
  python -m pip install -r requirements.txt
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
