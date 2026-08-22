@echo off
REM ===========================================================================
REM  WebImove API - development launcher
REM
REM  Double-click this file, or run it from a terminal:
REM
REM      start.bat                 dev server on port 8002, auto-reload
REM      start.bat prod            no reload, 4 workers
REM      start.bat test            run the test suite instead
REM      set PORT=9000             then start.bat, to use a different port
REM
REM  Anything else you pass is handed straight to uvicorn, e.g.
REM      start.bat --log-level debug
REM ===========================================================================
setlocal

REM Run from the folder this script lives in, whatever the current directory is.
cd /d "%~dp0"

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"
if "%HOST%"=="" set "HOST=0.0.0.0"
if "%PORT%"=="" set "PORT=8002"

echo.
echo  ======================================================
echo    WebImove API
echo  ======================================================
echo.

REM --------------------------------------------------------------- virtualenv
if not exist "%PY%" (
    echo  [1/3] Creating virtual environment in %VENV% ...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo.
        echo  [ERROR] Could not create the virtual environment.
        echo          Check that Python 3.11+ is installed and on PATH:
        echo              python --version
        goto :fail
    )
    set "NEEDS_DEPS=1"
) else (
    echo  [1/3] Virtual environment found.
)

REM ------------------------------------------------------------- dependencies
REM uvicorn is the last thing that has to be importable before we can serve,
REM so it doubles as the "are the requirements installed" probe.
"%PY%" -c "import uvicorn" >nul 2>&1
if errorlevel 1 set "NEEDS_DEPS=1"

if defined NEEDS_DEPS (
    echo  [2/3] Installing dependencies from requirements.txt ...
    "%PY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
    "%PY%" -m pip install -r requirements.txt --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo  [ERROR] Dependency installation failed. See the output above.
        goto :fail
    )
) else (
    echo  [2/3] Dependencies already installed.
)

REM ------------------------------------------------------------ configuration
if not exist ".env" (
    if not exist ".env.example" (
        echo.
        echo  [ERROR] No .env file, and no .env.example to copy from.
        goto :fail
    )
    copy /y ".env.example" ".env" >nul
    echo.
    echo  [WARNING] .env did not exist - a copy of .env.example was created.
    echo            The API will start, but you must fill these in before it works:
    echo              MONGODB_URI      - your MongoDB Atlas connection string
    echo              JWT_SECRET_KEY   - generate one, see below
    echo              SMTP_*           - or no OTP email can be sent
    echo              OPENAI_API_KEY   - or the AI features stay disabled
    echo.
    echo            .env.example carries the command for generating a JWT secret.
    echo.
)

REM -------------------------------------------------------------------- modes
if /i "%~1"=="test" (
    echo  [3/3] Running the test suite ...
    echo.
    "%PY%" -m pytest -q
    if errorlevel 1 goto :fail
    goto :done
)

if /i "%~1"=="prod" (
    echo  [3/3] Starting in production mode on %HOST%:%PORT% ...
    echo.
    "%PY%" -m uvicorn app.main:app --host %HOST% --port %PORT% --workers 4
    goto :stopped
)

echo  [3/3] Starting the development server ...
echo.
echo         API   http://localhost:%PORT%
echo         Docs  http://localhost:%PORT%/docs
echo         Health http://localhost:%PORT%/health
echo.
echo         Press CTRL+C to stop.
echo.
"%PY%" -m uvicorn app.main:app --host %HOST% --port %PORT% --reload %*

:stopped
echo.
echo  Server stopped.
goto :done

:fail
echo.
echo  Startup aborted.
pause
exit /b 1

:done
endlocal
REM Keep the window open when the script was double-clicked from Explorer.
if "%~1"=="" pause
exit /b 0
