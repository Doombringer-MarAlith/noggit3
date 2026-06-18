@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ or add it to PATH.
  pause
  exit /b 1
)
python generate_adt.py
if errorlevel 1 (
  echo Generation failed.
  pause
  exit /b 1
)
echo.
echo Done. Files are in output_loose\
echo Includes ADT/WDT, custom WDL, MH2O water, minimap BLPs, TGA previews, and md5translate fragment.
echo.
echo Optional MPQ step:
echo   Put MPQEditor.exe next to this .bat, then run pack_mpq.bat.
echo.
pause
