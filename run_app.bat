@echo off
REM ===========================================================================
REM  Veri-Fact Desktop launcher
REM  ---------------------------------------------------------------------
REM  Double-click this file to open the application window.
REM  It does NOT start a web server and it does NOT open a browser.
REM ===========================================================================

REM Run from the folder this .bat lives in, whatever the shell's current dir is.
cd /d "%~dp0"

title Veri-Fact Desktop

REM --- find a usable Python -------------------------------------------------
REM 'py' (the Windows launcher) is preferred because it can pick 3.10+ for us.
where py >nul 2>&1
if %errorlevel%==0 (
    set "PYCMD=py -3"
) else (
    where python >nul 2>&1
    if %errorlevel%==0 (
        set "PYCMD=python"
    ) else (
        echo.
        echo  Python was not found on your PATH.
        echo  Install Python 3.10 or newer from https://www.python.org/downloads/
        echo  and tick "Add python.exe to PATH" during installation.
        echo.
        pause
        exit /b 1
    )
)

echo Starting Veri-Fact Desktop...
%PYCMD% main.py

REM --- keep the console open if something went wrong ------------------------
if %errorlevel% neq 0 (
    echo.
    echo  The application exited with error code %errorlevel%.
    echo.
    echo  If dependencies are missing, run these two commands once:
    echo      %PYCMD% -m pip install -r requirements.txt
    echo      %PYCMD% -m spacy download en_core_web_sm
    echo.
    pause
)
exit /b %errorlevel%
