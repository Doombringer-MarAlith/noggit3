@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: learn_blizzlike_rules.bat C:\path\to\folder\with\ADTs
  echo Example: learn_blizzlike_rules.bat input_existing\world\maps\aigen
  echo.
  echo If no path is provided, this will analyze .\learn_input
  python learn_blizzlike_rules.py learn_input -o learned_blizzlike_rules.json
) else (
  python learn_blizzlike_rules.py "%~1" -o learned_blizzlike_rules.json
)
pause
