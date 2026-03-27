@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set PY=python
set SCRIPT=Sandwich_Autosweeper.py

set TOTAL=%~1
if "%TOTAL%"=="" set TOTAL=14400

set /a STAGE1=%TOTAL%/4
set /a STAGE2=%TOTAL%-%STAGE1%

set DX=0.02
set DY=0.02
set FIX_AL2O3=0.026
set FIX_BTO=0.150
set FIX_GAP=4.400
set FIX_TOPW=1.000

for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString(\"yyyyMMdd_HHmmss\")"') do set TS=%%i
set OUTDIR=quick_staged_%TS%
mkdir "%OUTDIR%" >nul 2>nul

echo [Stage-Plan] total=%TOTAL%s, stage1=%STAGE1%s, stage2=%STAGE2%s
echo [Stage-Plan] output=%OUTDIR%

echo [Stage-1] Launch 3 patch jobs in parallel...
start "PATCH_SIO2" /min cmd /c "%PY% -u %SCRIPT% --structure-family custom --top-core-material sio2 --spacer-material sio2 --fixed-al2o3 %FIX_AL2O3% --fixed-bto %FIX_BTO% --fixed-gap %FIX_GAP% --fixed-top-width %FIX_TOPW% --time-limit-sec %STAGE1% --dx %DX% --dy %DY% --progress-every-sec 30 --save-json \"%OUTDIR%\patch_sio2.json\" 1>>\"%OUTDIR%\patch_sio2.log\" 2>&1"
start "PATCH_AL2O3" /min cmd /c "%PY% -u %SCRIPT% --structure-family custom --top-core-material al2o3 --spacer-material sio2 --fixed-al2o3 %FIX_AL2O3% --fixed-bto %FIX_BTO% --fixed-gap %FIX_GAP% --fixed-top-width %FIX_TOPW% --time-limit-sec %STAGE1% --dx %DX% --dy %DY% --progress-every-sec 30 --save-json \"%OUTDIR%\patch_al2o3.json\" 1>>\"%OUTDIR%\patch_al2o3.log\" 2>&1"
start "PATCH_SIN" /min cmd /c "%PY% -u %SCRIPT% --structure-family custom --top-core-material sin --spacer-material sio2 --fixed-al2o3 %FIX_AL2O3% --fixed-bto %FIX_BTO% --fixed-gap %FIX_GAP% --fixed-top-width %FIX_TOPW% --time-limit-sec %STAGE1% --dx %DX% --dy %DY% --progress-every-sec 30 --save-json \"%OUTDIR%\patch_sin.json\" 1>>\"%OUTDIR%\patch_sin.log\" 2>&1"

echo [Stage-1] Running for %STAGE1%s ...
timeout /t %STAGE1% /nobreak >nul

echo [Stage-1] Budget reached, stopping patch jobs...
taskkill /FI "WINDOWTITLE eq PATCH_SIO2*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq PATCH_AL2O3*" /T /F >nul 2>nul
taskkill /FI "WINDOWTITLE eq PATCH_SIN*" /T /F >nul 2>nul

echo [Stage-2] Launch 2 sandwich jobs in parallel...
start "SAND_AL2O3_SIO2" /min cmd /c "%PY% -u %SCRIPT% --structure-family custom --top-core-material al2o3 --spacer-material sio2 --fixed-al2o3 %FIX_AL2O3% --fixed-bto %FIX_BTO% --fixed-gap %FIX_GAP% --fixed-top-width %FIX_TOPW% --time-limit-sec %STAGE2% --dx %DX% --dy %DY% --progress-every-sec 30 --save-json \"%OUTDIR%\sand_al2o3_sio2.json\" 1>>\"%OUTDIR%\sand_al2o3_sio2.log\" 2>&1"
start "SAND_SIO2_AL2O3" /min cmd /c "%PY% -u %SCRIPT% --structure-family custom --top-core-material sio2 --spacer-material al2o3 --fixed-al2o3 %FIX_AL2O3% --fixed-bto %FIX_BTO% --fixed-gap %FIX_GAP% --fixed-top-width %FIX_TOPW% --time-limit-sec %STAGE2% --dx %DX% --dy %DY% --progress-every-sec 30 --save-json \"%OUTDIR%\sand_sio2_al2o3.json\" 1>>\"%OUTDIR%\sand_sio2_al2o3.log\" 2>&1"

echo [Done] Staged jobs launched.
echo [Done] Logs/JSON directory: %OUTDIR%
echo [Hint] You can monitor logs in real time from this folder.
