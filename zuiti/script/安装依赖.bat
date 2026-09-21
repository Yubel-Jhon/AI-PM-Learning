@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r requirements.txt
echo.
echo 依赖安装完成。确认已装并启动 Ollama，且已拉取模型：
echo   ollama pull qwen2.5:7b
pause
