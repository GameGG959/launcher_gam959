@echo off
cd /d "%~dp0"
set PY=
where py >nul 2>nul
if %errorlevel%==0 set PY=py -3
if "%PY%"=="" where python >nul 2>nul
if "%PY%"=="" if %errorlevel%==0 set PY=python
if "%PY%"=="" goto nopy
if not exist .venv %PY% -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
cd server
echo.
echo  Site: http://127.0.0.1:8080   (keep this window open, close it to stop the site)
echo.
start "" http://127.0.0.1:8080
python app.py
pause
exit /b 0
:nopy
echo Python is not installed. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then run this file again.
start "" https://www.python.org/downloads/
pause
