@echo off
REM Double-click to: set up, load sample data, run all tests, run the full demo.
cd /d "%~dp0"
if not exist .venv (
  echo Creating a private Python environment in .venv ...
  python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat
echo Installing requirements ...
python -m pip install -q -r requirements.txt || goto :error
echo.
echo Loading sample data ...
python -m app.seed || goto :error
echo.
echo Running the 54 automated tests ...
python -m pytest -q || goto :error
echo.
echo Running the scenario walkthrough ...
python demo.py
echo.
echo All done. To open the API in a browser, double-click start_server.bat
pause
exit /b 0
:error
echo.
echo Something failed above. Copy the messages and ask for help.
pause
exit /b 1
