@echo off
title BTC Options Pinning Dashboard (Online)
echo ========================================================
echo  Starting BTC Options Pinning Dashboard with Cloudflare
echo ========================================================
cd /d "%~dp0"
set PYTHONPATH=.

start "Web App Backend" .venv\Scripts\python.exe main.py
timeout /t 3 /nobreak >nul
start "Cloudflare Tunnel" cloudflared.exe tunnel --url http://127.0.0.1:8000
echo.
echo System started! Check the Cloudflare window for your public HTTPS link.
pause
