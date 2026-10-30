@echo off
rem Usage:
rem   scripts\stop-tts.cmd [port]          unload model only (frees VRAM; service stays)
rem   scripts\stop-tts.cmd /kill [port]    kill the service that belongs to THIS repo
setlocal
rem 归一化仓库根路径（%~dp0.. 里带 ".."，直接字符串匹配会失败）
for %%I in ("%~dp0..") do set "ROOT=%%~fI"

set "PORT="
if /I "%~1"=="/kill" (
  set "PORT=%~2"
  goto kill
)
set "PORT=%~1"
if "%PORT%"=="" set "PORT=7880"

echo [tts] unloading model on port %PORT% ...
powershell -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:%PORT%/unload' -Method Post -TimeoutSec 30 | Out-Null; Write-Host '  [tts] model unloaded' } catch { Write-Host '  [tts] service not running on %PORT%' }"
exit /b 0

:kill
if "%PORT%"=="" set "PORT=7880"
echo [tts] killing service on port %PORT% (only if it belongs to this repo) ...
powershell -NoProfile -Command ^
  "$port=%PORT%;" ^
  "$root='%ROOT%';" ^
  "$c=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1;" ^
  "if(-not $c){ Write-Host ('  [tts] nothing listening on ' + $port); exit 0 }" ^
  "$p=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $c.OwningProcess);" ^
  "if($p.CommandLine -and $p.CommandLine.Contains($root)){ Stop-Process -Id $p.ProcessId -Force; Write-Host ('  [tts] killed PID ' + $p.ProcessId) }" ^
  "else { Write-Host ('  [tts] port ' + $port + ' belongs to another program, refusing to kill'); exit 1 }"
exit /b %errorlevel%
