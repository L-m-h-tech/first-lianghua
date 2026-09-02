@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist logs mkdir logs
rem P0-2：固定使用已装好依赖的解释器（默认 python 可能是无 requests 的沙箱版）；找不到再回退 PATH 中的 python
set "PYEXE=D:\Python\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
echo 解释器: %PYEXE% ｜ 异常退出将15秒后自动重启，Ctrl+C 正常退出不重启

:retry
"%PYEXE%" main.py %*
set "RC=%errorlevel%"
if "%RC%"=="0" goto end
rem P0-6：非正常退出（崩溃/看门狗卡死自退出）时记录并自动重启；关闭本窗口即停止
echo [%date% %time%] main.py 异常退出(代码%RC%)，15秒后自动重启 >> logs\restart.log
echo 程序异常退出(代码 %RC%)，15秒后自动重启；直接关闭本窗口可停止守护
timeout /t 15 /nobreak >nul
goto retry

:end
echo 程序正常退出。
pause
