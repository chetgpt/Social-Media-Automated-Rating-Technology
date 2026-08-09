@echo off
echo TikTok Comments Compiler Launcher
echo ====================================
echo.

REM Check if Python is installed
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo Python is not installed or not in your PATH.
    echo Please install Python from https://www.python.org/downloads/
    echo.
    pause
    exit /b
)

REM Check for command line arguments
if "%~1"=="" (
    echo Starting TikTok Comments Compiler in GUI mode...
    python comment_compiler.py
) else if "%~1"=="batch" (
    echo Starting TikTok Comments Compiler in Batch Processing mode...
    python comment_compiler.py batch
) else if "%~1"=="help" (
    echo TikTok Comments Compiler - Command Line Options
    echo.
    echo Usage:
    echo   run_compiler.bat           - Start with normal GUI
    echo   run_compiler.bat batch     - Start in batch processing mode
    echo   run_compiler.bat [folder]  - Process a specific folder
    echo   run_compiler.bat help      - Show this help message
    echo.
    pause
) else (
    echo Processing specific folder: %~1
    python comment_compiler.py "%~1"
)

echo.
echo Press any key to exit...
pause > nul