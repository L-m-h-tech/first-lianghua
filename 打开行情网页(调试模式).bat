@echo off
chcp 65001 >nul
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "P1=https://www.openvlab.cn/market"
set "P2=https://www.jiaoyikecha.com/"
if exist "%EDGE%" (
  start "" "%EDGE%" --remote-debugging-port=9222 --user-data-dir="%TEMP%\ovl_jykc_profile" "%P1%" "%P2%"
) else if exist "%CHROME%" (
  start "" "%CHROME%" --remote-debugging-port=9222 --user-data-dir="%TEMP%\ovl_jykc_profile" "%P1%" "%P2%"
) else (
  echo 未找到 Edge/Chrome，请手动打开这两个网页
  pause
  exit /b
)
echo 已用调试模式打开行情网页(OpenVlab + 交易可查)。
echo 现在运行 main.py 即可自动读取这两个页面的内容数据。
timeout /t 5 >nul
