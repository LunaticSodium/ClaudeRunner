@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem Wide 9-structure runner (sequential)
rem
rem Structures:
rem   1) pure SiN
rem   2) pure SiO2
rem   3) pure Al2O3
rem   4) SiN gap + SiO2        (top=sio2, spacer=sin)
rem   5) SiN gap + Al2O3       (top=al2o3, spacer=sin)
rem   6) air gap + SiO2        (top=sio2, spacer=air)
rem   7) air gap + Al2O3       (top=al2o3, spacer=air)
rem   8) sio2 gap + Al2O3      (top=al2o3, spacer=sio2)
rem   9) al2o3 gap + SiO2      (top=sio2, spacer=al2o3)
rem
rem Current Python supports spacer-material=sin in custom mode.
rem Defaults:
rem   per-structure time = 7200 sec (2 h)
rem   dx = dy = 0.02
rem   fixed electrode gap = 4.4 um
rem ============================================================

set "PER_JOB_SEC=%~1"
if "%PER_JOB_SEC%"=="" set "PER_JOB_SEC=7200"

set "FIXED_GAP=%~2"
if "%FIXED_GAP%"=="" set "FIXED_GAP=4.4"

set /a TOTAL_SEC=%PER_JOB_SEC%*9

set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"
set "OUT_DIR=%BASE_DIR%\wide9_dx02_%STAMP%"
mkdir "%OUT_DIR%" >nul 2>nul

echo ============================================================
echo Output directory:
echo   %OUT_DIR%
echo Per-structure time: %PER_JOB_SEC% sec
echo Total planned time: %TOTAL_SEC% sec
echo dx = dy = 0.02
echo fixed electrode gap = %FIXED_GAP% um
echo ============================================================
echo.

call :RUN_JOB "01_patch_sin" ^
  --structure-family patch_sin ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :RUN_JOB "02_patch_sio2" ^
  --structure-family patch_sio2 ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :RUN_JOB "03_patch_al2o3" ^
  --structure-family patch_al2o3 ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :RUN_JOB "04_sin_gap_sio2" ^
  --structure-family custom ^
  --top-core-material sio2 ^
  --spacer-material sin ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :RUN_JOB "05_sin_gap_al2o3" ^
  --structure-family custom ^
  --top-core-material al2o3 ^
  --spacer-material sin ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :RUN_JOB "06_air_gap_sio2" ^
  --structure-family sio2_air_bto ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :RUN_JOB "07_air_gap_al2o3" ^
  --structure-family custom ^
  --top-core-material al2o3 ^
  --spacer-material air ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :RUN_JOB "08_sio2_gap_al2o3" ^
  --structure-family sandwich_al2o3_sio2 ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :RUN_JOB "09_al2o3_gap_sio2" ^
  --structure-family sandwich_sio2_al2o3 ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

echo.
python -u "%BASE_DIR%\util.py" summarize --dir "%OUT_DIR%"
echo ============================================================
echo Wide run finished.
echo Output directory:
echo   %OUT_DIR%
echo ============================================================
goto :EOF

:RUN_JOB
set "JOB_NAME=%~1"
shift

set "JOB_LOG=%OUT_DIR%\%JOB_NAME%.log"
set "JOB_JSON=%OUT_DIR%\%JOB_NAME%.json"
set "JOB_ARGS="

:RUN_JOB_COLLECT
if "%~1"=="" goto RUN_JOB_EXEC
set "JOB_ARGS=!JOB_ARGS! %1"
shift
goto RUN_JOB_COLLECT

:RUN_JOB_EXEC
echo ------------------------------------------------------------
echo Running %JOB_NAME%
echo ------------------------------------------------------------

python -u "%BASE_DIR%\util.py" tee --log "%JOB_LOG%" -- ^
python -u "%BASE_DIR%\Sandwich_Autosweeper.py" ^
!JOB_ARGS! ^
--save-json "%JOB_JSON%"

echo Finished %JOB_NAME%
echo.
exit /b 0