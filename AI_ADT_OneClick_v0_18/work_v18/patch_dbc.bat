@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: patch_dbc.bat C:\path\to\extracted\DBFilesClient [map_id] [area_id]
  echo.
  echo Appends the generated map to Map.dbc and AreaTable.dbc so the 3.3.5a client
  echo and your server can enter it. Extract DBFilesClient from the client MPQs
  echo first ^(the tool never modifies the source folder^).
  pause
  exit /b 1
)
set MAPID=
set AREAID=
if not "%~2"=="" set MAPID=--map-id %~2
if not "%~3"=="" set AREAID=--area-id %~3
python patch_dbc.py --dbc-dir "%~1" %MAPID% %AREAID%
if errorlevel 1 goto :err
echo.
echo Patched DBCs written next to the generated map ^(see output above^).
echo Put DBFilesClient\ into the patch MPQ and copy the same files into the server dbc folder.
pause
exit /b 0
:err
echo.
echo FAILED. See error above.
pause
exit /b 1
