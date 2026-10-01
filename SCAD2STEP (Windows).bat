@echo off
rem Double-click to start SCAD2STEP on Windows (64-bit Windows 10/11).
rem Everything lives inside this folder: a private Python, the libraries, work files and
rem your results. Nothing is installed on the system and no other Python is used.
rem To remove SCAD2STEP completely, delete the folder.
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
set "APP=%ROOT%app"
rem x64 build also on ARM Windows (runs emulated): the CAD library has no ARM build
set "RT=%ROOT%runtime\x86_64-pc-windows-msvc"
if not exist "%RT%" mkdir "%RT%"
if not exist "%ROOT%runtime\tmp" mkdir "%ROOT%runtime\tmp"
if not exist "%ROOT%Output" mkdir "%ROOT%Output"

set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONSTARTUP="
set "PYTHONUSERBASE="
set "VIRTUAL_ENV="
set "CONDA_PREFIX="
set "PIP_REQUIRE_VIRTUALENV="
set "PYTHONNOUSERSITE=1"
set "UV_PYTHON_INSTALL_DIR=%RT%\python"
set "UV_PYTHON_BIN_DIR=%RT%\bin"
set "UV_CACHE_DIR=%RT%\cache"
set "UV_TOOL_DIR=%RT%\tools"
set "UV_TOOL_BIN_DIR=%RT%\bin"
set "UV_NO_CONFIG=1"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_NO_MODIFY_PATH=1"
set "TMP=%ROOT%runtime\tmp"
set "TEMP=%ROOT%runtime\tmp"
set "MPLCONFIGDIR=%ROOT%runtime\tmp\matplotlib"
set "SCAD2STEP_WORK=%ROOT%runtime\work"
set "SCAD2STEP_OUTPUT=%ROOT%Output"

call :find_py
if defined PY goto have_py
echo First run: setting up a private Python inside this folder.
call :get_uv || goto fail
echo Downloading Python 3.12 - about 30 MB...
"%RT%\uv.exe" python install cpython-3.12-windows-x86_64-none --no-bin --no-registry
if errorlevel 1 goto py_fail
call :find_py
if defined PY goto have_py
echo Python was downloaded but will not start. Delete the runtime folder and try again.
goto fail
:py_fail
echo Could not download Python. Check your internet connection and run this again.
goto fail

:have_py
fc /b "%APP%\requirements.txt" "%RT%\req.txt" >nul 2>&1
if not errorlevel 1 goto run
call :get_uv || goto fail
echo Installing libraries - about 400 MB the first time, 1.5 GB on disk. Please wait...
"%RT%\uv.exe" pip install --python "%PY%" --system --break-system-packages -r "%APP%\requirements.txt"
if errorlevel 1 goto lib_fail
copy /y "%APP%\requirements.txt" "%RT%\req.txt" >nul
if exist "%UV_CACHE_DIR%" rmdir /s /q "%UV_CACHE_DIR%"
goto run
:lib_fail
echo Installing the libraries failed. Check your internet connection and run this again.
goto fail

:run
"%PY%" -E -s "%APP%\scad2step.py" %*
if errorlevel 1 pause
exit /b 0

:fail
pause
exit /b 1

rem ---------------------------------------------------------------- helpers
:find_py
set "PY="
for /d %%D in ("%RT%\python\cpython-3.12.*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
exit /b 0

:get_uv
if exist "%RT%\uv.exe" exit /b 0
set "DL=%RT%\uv-download"
if exist "%DL%" rmdir /s /q "%DL%"
mkdir "%DL%"
echo Downloading uv, the Python installer - about 20 MB...
curl.exe -fL --retry 3 -o "%DL%\uv.zip" "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
if errorlevel 1 goto uv_pypi
tar.exe -xf "%DL%\uv.zip" -C "%DL%"
if errorlevel 1 goto uv_pypi
goto uv_found
:uv_pypi
echo GitHub did not answer - getting uv from PyPI instead...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference = 'SilentlyContinue'; $u = (Invoke-RestMethod 'https://pypi.org/pypi/uv/json').urls | Where-Object { $_.filename -like '*-py3-none-win_amd64.whl' } | Select-Object -First 1 -ExpandProperty url; Invoke-WebRequest $u -OutFile $env:DL\uv.zip"
if errorlevel 1 goto uv_fail
tar.exe -xf "%DL%\uv.zip" -C "%DL%"
if errorlevel 1 goto uv_fail
:uv_found
for /r "%DL%" %%F in (uv.exe) do if exist "%%F" move /y "%%F" "%RT%\uv.exe" >nul
rmdir /s /q "%DL%"
if exist "%RT%\uv.exe" exit /b 0
:uv_fail
echo Could not download uv. Check your internet connection and run this again.
exit /b 1
