@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\activate.bat" (
    call "%~dp0.venv\Scripts\activate.bat"
)
python "%~dp0run.py" %*
pause
