@echo off
chcp 65001 >nul
title CapsWriter-Offline Launcher

set PROJECT_DIR=D:\.Projects\CapsWriter-Offline
set UV_PATH=%USERPROFILE%\.local\bin\uv

echo ========================================
echo   CapsWriter-Offline Launcher
echo ========================================
echo.

echo [1/3] Starting server ...
start "CapsWriter-Server" cmd /k "cd /d %PROJECT_DIR% && "%UV_PATH%" run start_server.py"

echo [2/3] Waiting for server to load model (8 seconds) ...
echo.
timeout /t 8 /nobreak >nul

echo [3/3] Starting client ...
start "CapsWriter-Client" cmd /k "cd /d %PROJECT_DIR% && "%UV_PATH%" run start_client.py"

echo.
echo Both windows launched!
echo - Server window: loading model, wait for green prompt
echo - Client window: hold CapsLock/X2 to speak
echo.
echo Hold CapsLock or X2 mouse button to speak, release to commit.
echo Close client tray first, then server window.
echo.
pause
