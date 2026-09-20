@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 演示模式：用脱敏演示库，真库不上台
set "ZUITI_DB=%~dp0demo_context.db"
python zuiti_overlay.py
if errorlevel 1 pause
