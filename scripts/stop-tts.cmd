@echo off
rem Usage:
rem   scripts\stop-tts.cmd          unload model only (frees VRAM, service stays; next call ~28s)
rem   scripts\stop-tts.cmd /kill   kill the service (next call auto-starts it, ~59s)
setlocal
set PORT=%2
if "%PORT%"=="" set PORT=7880

if /I "%1"=="/kill" goto kill

echo [tts] unloading model ...
powershell -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:%PORT%/unload' -Method Post -TimeoutSec 30 | Out-Null; Write-Host '  [tts] model unloaded' } catch { Write-Host '  [tts] service not running on %PORT%' }"
exit /b 0

:kill
echo [tts] killing service ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'tts_server\.py' } | ForEach-Object { Write-Host ('  kill PID ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
exit /b 0
