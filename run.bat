@echo off
setlocal enabledelayedexpansion

:: Change directory to the script's location
cd /d "%~dp0"

echo ===================================================
echo             Starting GameSync LAN Sync
echo ===================================================

:: Check for virtual environment in standard locations
if exist ".venv\Scripts\python.exe" (
    echo [GameSync] Found virtual environment in .venv
    set "PYTHON_EXE=.venv\Scripts\python.exe"
) else if exist "venv\Scripts\python.exe" (
    echo [GameSync] Found virtual environment in venv
    set "PYTHON_EXE=venv\Scripts\python.exe"
) else (
    echo [GameSync] No virtual environment found.
    echo [GameSync] Checking for Python in system PATH...
    where python >nul 2>nul
    if !errorlevel! equ 0 (
        echo [GameSync] Python found. Creating a new virtual environment (.venv)...
        python -m venv .venv
        if !errorlevel! neq 0 (
            echo [GameSync] Warning: Failed to create virtual environment. Using system Python.
            set "PYTHON_EXE=python"
        ) else (
            set "PYTHON_EXE=.venv\Scripts\python.exe"
            echo [GameSync] Virtual environment created successfully.
            echo [GameSync] Installing dependencies from requirements.txt...
            .venv\Scripts\pip install -r requirements.txt
            if !errorlevel! neq 0 (
                echo [GameSync] Warning: Some dependencies failed to install.
            )
        )
    ) else (
        echo [GameSync] ERROR: Python was not found on your system or in your PATH.
        echo Please install Python (3.8 or newer) and check "Add Python to PATH".
        echo.
        pause
        exit /b 1
    )
)

:: Run the application
echo [GameSync] Launching run.py...
echo.
"%PYTHON_EXE%" run.py %*

if %errorlevel% neq 0 (
    echo.
    echo [GameSync] Application exited with error code %errorlevel%
    pause
)
