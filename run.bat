@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\activate.bat" (
    call "%~dp0.venv\Scripts\activate.bat"
)
echo Launching Web UI...
start http://localhost:8384
python "%~dp0run.py" %*
pause
