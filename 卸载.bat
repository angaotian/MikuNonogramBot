@echo off
chcp 936 >nul
title Miku 数织自动闯关 - 卸载

rem 下面这段由 %TEMP% 里的副本执行：原文件夹里的文件正被占用，删不掉自己，
rem 所以先把本脚本复制到临时目录，再由副本删掉整个程序文件夹。
if /i "%~1"=="/purge" goto purge

cd /d "%~dp0"
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"

echo ============================================================
echo   Miku 数织自动闯关  卸载程序
echo ============================================================
echo   将会删除：
echo     - 程序文件夹   %APPDIR%
echo     - 里面的运行数据：debug 截图、校准配置、临时备份
echo     - 系统临时目录里本程序遗留的临时文件
echo.
echo   不会动：Tesseract-OCR、WebView2（系统或其他软件也可能在用）
echo ============================================================
echo.
choice /c YN /n /m "确定要卸载吗？（Y = 卸载    N = 取消）"
if errorlevel 2 goto cancel

echo.
echo [1/4] 关闭正在运行的程序…
taskkill /f /im MikuPanel.exe >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter 'Name=''python.exe'' or Name=''pythonw.exe''' -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*miku_logic_paint_bot*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul

echo [2/4] 清理系统临时目录里的临时文件…
for /d %%D in ("%TEMP%\_MEI*") do if exist "%%~fD\miku_logic_paint_bot.py" rd /s /q "%%~fD" >nul 2>nul

echo [3/4] 清理运行数据…
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
rem 先把本窗口的当前目录切走：只要还有窗口停在这个文件夹里，它就删不掉
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

:purge
set "TARGET=%~2"
rem 关键：把自己的当前目录切到 %TEMP%。cmd 只要还停在待删目录里，rd 就会报
rem 「目录正在使用」而失败；上面那个窗口也一样，所以先等它退出再动手。
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
