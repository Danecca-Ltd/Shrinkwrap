@echo off
REM ============================================================
REM Shrinkwrap - deploy the add-in to Fusion
REM Double-click this file to copy the bundle into ApplicationPlugins.
REM robocopy overwrites in place and never deletes the destination first.
REM ============================================================

setlocal

set "SCRIPT_DIR=%~dp0"
set "SOURCE_PATH=%SCRIPT_DIR%Shrinkwrap.bundle"
set "DEPLOY_PATH=%APPDATA%\Autodesk\ApplicationPlugins\Shrinkwrap.bundle"

if not exist "%SOURCE_PATH%" (
    echo ERROR: Source bundle not found: %SOURCE_PATH%
    goto :end
)

echo Source:      %SOURCE_PATH%
echo Destination: %DEPLOY_PATH%
echo.

REM /E include subdirs  /IS /IT overwrite same/tweaked files  /XD __pycache__
robocopy "%SOURCE_PATH%" "%DEPLOY_PATH%" /E /IS /IT /XD __pycache__ /NFL /NDL /NJH /NJS /NP >NUL
if errorlevel 8 (
    echo ERROR: Copy failed. The previous install was not deleted.
    goto :end
)

echo Deployed. In Fusion: Utilities ^> Add-Ins ^> Shrinkwrap ^> Stop, then Run
echo ^(or restart Fusion^) to load the new code.

:end
echo.
pause
