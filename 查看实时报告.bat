@echo off
chcp 65001 >nul
cd /d %~dp0
rem 打开多页签实时看板：浏览器跟随轮动节奏自动刷新（开盘前30分钟5分钟、之后20分钟、非交易时段1分钟），无需关闭重开
start "" "reports\实时报告.html"
