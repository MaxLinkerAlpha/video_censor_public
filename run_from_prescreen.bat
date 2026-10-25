@echo off
set PYTHONUNBUFFERED=1
cd /d "%~dp0"

echo ==================================================
echo    Content Censor Workflow v5.0.0
echo    From Script-Prescreen to Completion
echo    (人工初筛版 - 自动去重 + AI 审核)
echo    Platform: %1 (all / bilibili / youtube)
echo ==================================================
echo.
echo This script:
echo   1. v5.0.0: 脚本自动去重 (替代人工初筛)
echo   2. 字幕获取 + AI 评分 + 飞书同步
echo.
echo Usage:
echo   run_from_prescreen.bat            (default: all platforms)
echo   run_from_prescreen.bat bilibili   (B站 only)
echo   run_from_prescreen.bat youtube    (YouTube only)
echo.

REM Parse first argument as platform
set PLATFORM=all
if not "%1"=="" set PLATFORM=%1

python core\workflow_orchestrator.py --stage human-prescreen --days 7 --platform %PLATFORM% %*
if errorlevel 1 goto error

echo.
echo ==================================================
echo Workflow completed successfully!
echo   - 人工仅需在飞书查看 AI 审核结果
echo ==================================================
goto :end

:error
echo.
echo ==================================================
echo Error occurred during workflow!
echo ==================================================
goto :end

:end
REM Bug fix (2026-06-15): mirror of the fix in run.bat - 'pause'
REM returns immediately in pipe mode, so we probe real stdin first.
if "%1"=="" (
    echo.
    echo Press Enter to close (or auto-close in pipe mode)...
    set /p "_end_pause=" 2>nul
    if errorlevel 1 (
        echo [PIPE MODE] 脚本结束，自动关闭。
    )
)
