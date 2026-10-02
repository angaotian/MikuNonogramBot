@echo off
chcp 936 >nul
title Miku 数织自动闯关 - 卸载

rem 本脚本有两条执行路径：
rem   1) 直接双击 —— 做身份校验、列出将删除的清单，确认后把本脚本复制到 %TEMP%，
rem      再由副本（带 /purge 参数）去删除程序文件夹。之所以要复制，是因为 rd 删不掉
rem      "正被自己占用的"目录，删除动作必须从别的地方发起。
rem   2) 副本带 /purge —— 只负责删掉传入的目标目录，删完把自己也删掉。
if /i "%~1"=="/purge" goto purge

cd /d "%~dp0"
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"

rem ================= 安全护栏 =================
rem 只有通过下面两道校验，才允许继续；否则一律拒绝，避免误删系统或用户自己的文件。
rem 第一道：目标不能是系统 / 个人目录，也不能是任意盘符的根目录。
set "PF86=%ProgramFiles(x86)%"
if "%APPDIR:~-1%"==":" goto refuse
if /i "%APPDIR%"=="%USERPROFILE%" goto refuse
if /i "%APPDIR%"=="%USERPROFILE%\Desktop" goto refuse
if /i "%APPDIR%"=="%USERPROFILE%\Documents" goto refuse
if /i "%APPDIR%"=="%USERPROFILE%\Downloads" goto refuse
if /i "%APPDIR%"=="%USERPROFILE%\AppData" goto refuse
if /i "%APPDIR%"=="%USERPROFILE%\AppData\Local" goto refuse
if /i "%APPDIR%"=="%APPDATA%" goto refuse
if /i "%APPDIR%"=="%LOCALAPPDATA%" goto refuse
if /i "%APPDIR%"=="%TEMP%" goto refuse
if /i "%APPDIR%"=="%SystemDrive%" goto refuse
if /i "%APPDIR%"=="%SystemRoot%" goto refuse
if /i "%APPDIR%"=="%ProgramFiles%" goto refuse
if /i "%APPDIR%"=="%PF86%" goto refuse

rem 第二道：目录里必须确实有本程序的文件，才算"这是我们的目录"。
if not exist "%APPDIR%\miku_logic_paint_bot.py" goto refuse
if not exist "%APPDIR%\MikuPanel.exe" if not exist "%APPDIR%\启动面板.bat" goto refuse

echo ============================================================
echo   Miku 数织自动闯关  卸载程序
echo ============================================================
echo   将要删除的内容（都是绝对路径）：
echo.
echo     [程序文件夹] %APPDIR%
if exist "%APPDIR%\debug" echo     [截图调试]   %APPDIR%\debug\
if exist "%APPDIR%\miku_bot_config.json" echo     [校准配置]   %APPDIR%\miku_bot_config.json
if exist "%APPDIR%\*.new" echo     [临时备份]   %APPDIR%\*.new
set "HASMEI="
for /d %%D in ("%TEMP%\_MEI*") do if exist "%%~fD\miku_logic_paint_bot.py" (echo     [临时残留]   %%~fD & set "HASMEI=1")
if not defined HASMEI echo     [临时残留]   （无）
echo.
echo   不会动 Tesseract-OCR、WebView2，也不会动这个清单以外的任何文件。
echo ============================================================
echo.
choice /c YN /n /m "确定要卸载吗？（Y = 卸载    N = 取消）"
if errorlevel 2 goto cancel

echo.
echo [1/4] 关闭正在运行的程序…
taskkill /f /im MikuPanel.exe >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter 'Name=''python.exe'' or Name=''pythonw.exe''' -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*miku_logic_paint_bot*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul

echo [2/4] 清理系统临时目录里本程序遗留的临时文件…
for /d %%D in ("%TEMP%\_MEI*") do if exist "%%~fD\miku_logic_paint_bot.py" rd /s /q "%%~fD" >nul 2>nul

echo [3/4] 清理运行数据（截图 / 校准配置 / 临时备份）…
rd /s /q "%APPDIR%\debug" >nul 2>nul
del /f /q "%APPDIR%\miku_bot_config.json" >nul 2>nul
del /f /q "%APPDIR%\*.new" >nul 2>nul

echo [4/4] 删除程序文件夹…
copy /y "%~f0" "%TEMP%\miku_uninstall_purge.bat" >nul 2>nul
if not exist "%TEMP%\miku_uninstall_purge.bat" (
  echo [错误] 无法创建临时卸载脚本，请手动删除这个文件夹：
  echo        %APPDIR%
  echo.
  pause
  exit /b 1
)
rem 先把本窗口的当前目录切走：只要还有窗口停在这个文件夹里，它就删不掉。
cd /d "%TEMP%"
start "" /min cmd /c ""%TEMP%\miku_uninstall_purge.bat" /purge "%APPDIR%""
echo.
echo 已发起删除，几秒后文件夹会自动消失；本窗口可以关掉。
timeout /t 2 /nobreak >nul 2>nul
exit /b 0

:cancel
echo.
echo 已取消，没有删除任何东西。
timeout /t 2 /nobreak >nul 2>nul
exit /b 0

:refuse
echo.
echo ============================================================
echo   已停止：这里不是 Miku 数织自动闯关 的安装目录
echo ============================================================
echo   当前目录：%APPDIR%
echo.
echo   为防止误删系统文件或你自己的资料，只有同时满足这两条才会执行卸载：
echo     - 这个目录里有 miku_logic_paint_bot.py
echo     - 它不是桌面 / 下载 / 文档 / 用户目录 / C 盘根目录等位置
echo.
echo   如果你是把程序文件直接解压到了桌面，请先新建一个文件夹
echo   （例如 MikuBot），把文件全部移进去，再运行里面的 卸载.bat。
echo ============================================================
echo.
pause
exit /b 1

:purge
set "TARGET=%~2"
rem 二次校验：只删确属本程序的目录，防止参数被改坏后误删。
if "%TARGET%"=="" exit
if "%TARGET:~-1%"==":" exit
if not exist "%TARGET%\miku_logic_paint_bot.py" exit
rem 关键：把自己的当前目录切到 %TEMP%。cmd 只要还停在待删目录里，rd 就会报
rem "目录正在使用"而失败；上面那个窗口也一样，所以先等它退出再动手。
cd /d "%TEMP%"
timeout /t 2 /nobreak >nul 2>nul
set /a tries=0
:retry
rd /s /q "%TARGET%" >nul 2>nul
if not exist "%TARGET%" goto purged
set /a tries+=1
if %tries% GEQ 30 goto purged
timeout /t 1 /nobreak >nul 2>nul
goto retry
:purged
(goto) 2>nul & del /f /q "%~f0" >nul 2>nul
exit
