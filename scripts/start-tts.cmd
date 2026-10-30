@echo off
rem Start the local TTS service (idempotent: skips if port is already listening).
rem Usage: scripts\start-tts.cmd [port] [idleMinutes]
setlocal
set ROOT=%~dp0..
set PORT=%1
if "%PORT%"=="" set PORT=7880
set IDLE=%2
if "%IDLE%"=="" set IDLE=15

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if %errorlevel%==0 (
  echo [tts] already running on %PORT%
  exit /b 0
)

if not exist "%ROOT%\.venv\Scripts\python.exe" (
  echo [tts] python venv not found: %ROOT%\.venv
  exit /b 1
)

echo [tts] starting on %PORT% ...
start "tts-%PORT%" /min "%ROOT%\.venv\Scripts\python.exe" "%ROOT%\server\tts_server.py" --port %PORT% --idle-min %IDLE% --tts-dir "%ROOT%"
echo [tts] launched. Model loads on first request.
echo [tts] health: http://127.0.0.1:%PORT%/health
endlocal
