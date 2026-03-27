@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem Wide 9-structure runner (FULL PARALLEL)
rem
rem 9 jobs are launched at the same time.
rem Each Python process uses BLAS/OpenMP threads = 1 by default.
rem Completion is judged by JSON existence (robust), not PID capture.
rem
rem Usage:
rem   run_wide_9parallel.bat
rem   run_wide_9parallel.bat 7200
rem   run_wide_9parallel.bat 7200 4.4
rem   run_wide_9parallel.bat 7200 4.4 1
rem
rem Args:
rem   %1 = per-job time budget in sec   (default 7200)
rem   %2 = fixed electrode gap in um    (default 4.4)
rem   %3 = BLAS/OpenMP threads/process  (default 1)
rem ============================================================

set "PER_JOB_SEC=%~1"
if "%PER_JOB_SEC%"=="" set "PER_JOB_SEC=7200"

set "FIXED_GAP=%~2"
if "%FIXED_GAP%"=="" set "FIXED_GAP=4.4"

set "BLAS_THREADS=%~3"
if "%BLAS_THREADS%"=="" set "BLAS_THREADS=1"

set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"
set "OUT_DIR=%BASE_DIR%\wide9_parallel_dx02_%STAMP%"
mkdir "%OUT_DIR%" >nul 2>nul

rem ---- Thread / runtime controls ----
set "OMP_NUM_THREADS=%BLAS_THREADS%"
set "MKL_NUM_THREADS=%BLAS_THREADS%"
set "OPENBLAS_NUM_THREADS=%BLAS_THREADS%"
set "NUMEXPR_NUM_THREADS=%BLAS_THREADS%"
set "KMP_BLOCKTIME=0"
set "OMP_WAIT_POLICY=PASSIVE"

echo ============================================================
echo Output directory:
echo   %OUT_DIR%
echo Per-job time: %PER_JOB_SEC% sec
echo Structures: 9 parallel jobs
echo dx = dy = 0.02
echo fixed electrode gap = %FIXED_GAP% um
echo BLAS/OpenMP threads per process = %BLAS_THREADS%
echo OMP_NUM_THREADS=%OMP_NUM_THREADS%
echo MKL_NUM_THREADS=%MKL_NUM_THREADS%
echo OPENBLAS_NUM_THREADS=%OPENBLAS_NUM_THREADS%
echo NUMEXPR_NUM_THREADS=%NUMEXPR_NUM_THREADS%
echo KMP_BLOCKTIME=%KMP_BLOCKTIME%
echo OMP_WAIT_POLICY=%OMP_WAIT_POLICY%
echo ============================================================
echo.

echo [Launch] Starting all 9 jobs in parallel...
echo.

call :START_JOB "01_patch_sin" ^
  --structure-family patch_sin ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --progress-every-evals 1 ^
  --print-each-eval ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :START_JOB "02_patch_sio2" ^
  --structure-family patch_sio2 ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --progress-every-evals 1 ^
  --print-each-eval ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :START_JOB "03_patch_al2o3" ^
  --structure-family patch_al2o3 ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --progress-every-evals 1 ^
  --print-each-eval ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :START_JOB "04_sin_gap_sio2" ^
  --structure-family custom ^
  --top-core-material sio2 ^
  --spacer-material sin ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --progress-every-evals 1 ^
  --print-each-eval ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :START_JOB "05_sin_gap_al2o3" ^
  --structure-family custom ^
  --top-core-material al2o3 ^
  --spacer-material sin ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --progress-every-evals 1 ^
  --print-each-eval ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :START_JOB "06_air_gap_sio2" ^
  --structure-family sio2_air_bto ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --progress-every-evals 1 ^
  --print-each-eval ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :START_JOB "07_air_gap_al2o3" ^
  --structure-family custom ^
  --top-core-material al2o3 ^
  --spacer-material air ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --progress-every-evals 1 ^
  --print-each-eval ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :START_JOB "08_sio2_gap_al2o3" ^
  --structure-family sandwich_al2o3_sio2 ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --progress-every-evals 1 ^
  --print-each-eval ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

call :START_JOB "09_al2o3_gap_sio2" ^
  --structure-family sandwich_sio2_al2o3 ^
  --time-limit-sec %PER_JOB_SEC% ^
  --progress-every-sec 30 ^
  --progress-every-evals 1 ^
  --print-each-eval ^
  --dx 0.02 --dy 0.02 ^
  --fixed-gap %FIXED_GAP% ^
  --fixed-bto 0.150 ^
  --fixed-al2o3 0.026

echo.
echo [Monitor] Waiting for all JSON files...
echo.

:WAIT_ALL
set "DONE=1"

if not exist "%OUT_DIR%\01_patch_sin.json" set "DONE=0"
if not exist "%OUT_DIR%\02_patch_sio2.json" set "DONE=0"
if not exist "%OUT_DIR%\03_patch_al2o3.json" set "DONE=0"
if not exist "%OUT_DIR%\04_sin_gap_sio2.json" set "DONE=0"
if not exist "%OUT_DIR%\05_sin_gap_al2o3.json" set "DONE=0"
if not exist "%OUT_DIR%\06_air_gap_sio2.json" set "DONE=0"
if not exist "%OUT_DIR%\07_air_gap_al2o3.json" set "DONE=0"
if not exist "%OUT_DIR%\08_sio2_gap_al2o3.json" set "DONE=0"
if not exist "%OUT_DIR%\09_al2o3_gap_sio2.json" set "DONE=0"

if "%DONE%"=="1" goto ALL_DONE

echo [%DATE% %TIME%] still waiting...
timeout /t 30 /nobreak >nul
goto WAIT_ALL

:ALL_DONE
echo.
echo [Done] All 9 jobs finished.
echo.

python -u "%BASE_DIR%\util.py" summarize --dir "%OUT_DIR%"

echo ============================================================
echo Wide parallel run finished.
echo Output directory:
echo   %OUT_DIR%
echo ============================================================
goto :EOF

:START_JOB
set "JOB_NAME=%~1"
shift

set "JOB_LOG=%OUT_DIR%\%JOB_NAME%.log"
set "JOB_JSON=%OUT_DIR%\%JOB_NAME%.json"
set "JOB_ARGS="

:START_JOB_COLLECT
if "%~1"=="" goto START_JOB_EXEC
set "JOB_ARGS=!JOB_ARGS! %1"
shift
goto START_JOB_COLLECT

:START_JOB_EXEC
echo ------------------------------------------------------------
echo Launching %JOB_NAME%
echo Log :
echo   %JOB_LOG%
echo JSON:
echo   %JOB_JSON%
echo ------------------------------------------------------------

start "%JOB_NAME%" /b cmd /c ^
python -u "%BASE_DIR%\util.py" tee --log "%JOB_LOG%" -- ^
python -u "%BASE_DIR%\Sandwich_Autosweeper.py" ^
!JOB_ARGS! ^
--save-json "%JOB_JSON%"

exit /b 0