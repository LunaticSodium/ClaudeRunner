@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem ============================================================
rem Staged 5-structure runner (basic / locked version)
rem - Total time default: 4 hours = 14400 sec
rem - dx = dy = 0.02
rem - gap fixed at 4.4 um
rem - Stage 1: 3 pure patch structures in parallel
rem             each may stop early once "local_opt=1" appears in log
rem - Stage 2: 2 sandwich structures in parallel
rem             both run until completion / time budget
rem
rem Robustness fix:
rem   Stage completion is judged by JSON existence, not unreliable PID capture.
rem ============================================================

set "TOTAL_TIME_SEC=%~1"
if "%TOTAL_TIME_SEC%"=="" set "TOTAL_TIME_SEC=86400"

set /a STAGE1_SEC=%TOTAL_TIME_SEC%/4
set /a STAGE2_SEC=%TOTAL_TIME_SEC%-%STAGE1_SEC%

set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"
set "OUT_DIR=%BASE_DIR%\quick_staged_%STAMP%"
mkdir "%OUT_DIR%" >nul 2>nul

echo ============================================================
echo Output directory:
echo   %OUT_DIR%
echo Total time:   %TOTAL_TIME_SEC% sec
echo Stage 1 time: %STAGE1_SEC% sec
echo Stage 2 time: %STAGE2_SEC% sec
echo dx = dy = 0.02
echo gap = fixed 4.4 um
echo ============================================================
echo.

echo [Stage 1/2] Launching 3 pure patch jobs in parallel...
echo.

set "LOG1=%OUT_DIR%\patch_sio2.log"
set "LOG2=%OUT_DIR%\patch_al2o3.log"
set "LOG3=%OUT_DIR%\patch_sin.log"

set "JSON1=%OUT_DIR%\patch_sio2.json"
set "JSON2=%OUT_DIR%\patch_al2o3.json"
set "JSON3=%OUT_DIR%\patch_sin.json"

start "patch_sio2" /b cmd /c ^
python -u "%BASE_DIR%\util.py" tee --log "%LOG1%" -- ^
python -u "%BASE_DIR%\Sandwich_Autosweeper.py" ^
--structure-family patch_sio2 ^
--time-limit-sec %STAGE1_SEC% ^
--progress-every-sec 5 ^
--dx 0.02 ^
--dy 0.02 ^
--fixed-gap 4.4 ^
--fixed-bto 0.30 ^
--fixed-al2o3 0.026 ^
--save-json "%JSON1%"

set "PID1="
for /f "tokens=2 delims=," %%P in ('tasklist /v /fo csv ^| findstr /i "patch_sio2"') do if not defined PID1 set "PID1=%%~P"

start "patch_al2o3" /b cmd /c ^
python -u "%BASE_DIR%\util.py" tee --log "%LOG2%" -- ^
python -u "%BASE_DIR%\Sandwich_Autosweeper.py" ^
--structure-family patch_al2o3 ^
--time-limit-sec %STAGE1_SEC% ^
--progress-every-sec 5 ^
--dx 0.02 ^
--dy 0.02 ^
--fixed-gap 4.4 ^
--fixed-bto 0.30 ^
--fixed-al2o3 0.026 ^
--save-json "%JSON2%"

set "PID2="
for /f "tokens=2 delims=," %%P in ('tasklist /v /fo csv ^| findstr /i "patch_al2o3"') do if not defined PID2 set "PID2=%%~P"

start "patch_sin" /b cmd /c ^
python -u "%BASE_DIR%\util.py" tee --log "%LOG3%" -- ^
python -u "%BASE_DIR%\Sandwich_Autosweeper.py" ^
--structure-family patch_sin ^
--time-limit-sec %STAGE1_SEC% ^
--progress-every-sec 5 ^
--dx 0.02 ^
--dy 0.02 ^
--fixed-gap 4.4 ^
--fixed-bto 0.30 ^
--fixed-al2o3 0.026 ^
--save-json "%JSON3%"

set "PID3="
for /f "tokens=2 delims=," %%P in ('tasklist /v /fo csv ^| findstr /i "patch_sin"') do if not defined PID3 set "PID3=%%~P"

set "DONE1=0"
set "DONE2=0"
set "DONE3=0"

:stage1_loop
if "%DONE1%"=="1" if "%DONE2%"=="1" if "%DONE3%"=="1" goto stage1_done

if "%DONE1%"=="0" (
    if exist "%JSON1%" (
        set "DONE1=1"
    ) else (
        if exist "%LOG1%" (
            findstr /c:"local_opt=1" "%LOG1%" >nul 2>nul
            if not errorlevel 1 (
                echo [Stage 1] patch_sio2 reached local_opt=1, stopping it early...
                if defined PID1 taskkill /PID %PID1% /T /F >nul 2>nul
            )
        )
    )
)

if "%DONE2%"=="0" (
    if exist "%JSON2%" (
        set "DONE2=1"
    ) else (
        if exist "%LOG2%" (
            findstr /c:"local_opt=1" "%LOG2%" >nul 2>nul
            if not errorlevel 1 (
                echo [Stage 1] patch_al2o3 reached local_opt=1, stopping it early...
                if defined PID2 taskkill /PID %PID2% /T /F >nul 2>nul
            )
        )
    )
)

if "%DONE3%"=="0" (
    if exist "%JSON3%" (
        set "DONE3=1"
    ) else (
        if exist "%LOG3%" (
            findstr /c:"local_opt=1" "%LOG3%" >nul 2>nul
            if not errorlevel 1 (
                echo [Stage 1] patch_sin reached local_opt=1, stopping it early...
                if defined PID3 taskkill /PID %PID3% /T /F >nul 2>nul
            )
        )
    )
)

timeout /t 5 /nobreak >nul
goto stage1_loop

:stage1_done
echo.
echo [Stage 1/2] Done.
echo.

echo [Stage 2/2] Launching 2 sandwich jobs in parallel...
echo.

set "LOG4=%OUT_DIR%\sandwich_al2o3_sio2.log"
set "LOG5=%OUT_DIR%\sandwich_sio2_al2o3.log"
set "JSON4=%OUT_DIR%\sandwich_al2o3_sio2.json"
set "JSON5=%OUT_DIR%\sandwich_sio2_al2o3.json"

start "sandwich_al2o3_sio2" /b cmd /c ^
python -u "%BASE_DIR%\util.py" tee --log "%LOG4%" -- ^
python -u "%BASE_DIR%\Sandwich_Autosweeper.py" ^
--structure-family sandwich_al2o3_sio2 ^
--time-limit-sec %STAGE2_SEC% ^
--progress-every-sec 30 ^
--dx 0.02 ^
--dy 0.02 ^
--fixed-gap 4.4 ^
--fixed-bto 0.30 ^
--fixed-al2o3 0.026 ^
--save-json "%JSON4%"

start "sandwich_sio2_al2o3" /b cmd /c ^
python -u "%BASE_DIR%\util.py" tee --log "%LOG5%" -- ^
python -u "%BASE_DIR%\Sandwich_Autosweeper.py" ^
--structure-family sandwich_sio2_al2o3 ^
--time-limit-sec %STAGE2_SEC% ^
--progress-every-sec 30 ^
--dx 0.02 ^
--dy 0.02 ^
--fixed-gap 4.4 ^
--fixed-bto 0.30 ^
--fixed-al2o3 0.026 ^
--save-json "%JSON5%"

:stage2_loop
if exist "%JSON4%" if exist "%JSON5%" goto all_done

timeout /t 5 /nobreak >nul
goto stage2_loop

:all_done
echo.
python -u "%BASE_DIR%\util.py" summarize --dir "%OUT_DIR%"
echo ============================================================
echo All staged jobs finished.
echo Output directory:
echo   %OUT_DIR%
echo ============================================================
echo.

endlocal