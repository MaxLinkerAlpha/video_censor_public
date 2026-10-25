@echo off
set PYTHONUNBUFFERED=1
cd /d "%~dp0"

chcp 65001 > nul

python core\run_workflow.py
set RC=%errorlevel%

echo.
echo ============================================================
echo   Exit code: %RC%
echo ============================================================

if "%1"=="" (
    echo Press Enter to close (auto-close in pipe mode)...
    set /p "_end_pause=" 2>nul
    if errorlevel 1 (
        echo [PIPE MODE] Auto-close.
    )
)

exit /b %RC%
