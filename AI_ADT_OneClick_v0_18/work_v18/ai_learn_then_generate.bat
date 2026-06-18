@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo No ADT source folder provided. Skipping learn step and generating from zone_prompt.txt.
) else (
  echo Learning local texture/style rules from: %~1
  python learn_blizzlike_rules.py "%~1" -o learned_blizzlike_rules.json || goto :err
)
python ai_zone_spec.py || goto :err
python generate_adt.py || goto :err
python validate_structure.py output_loose || goto :err
echo.
echo Done. Output is in output_loose
pause
exit /b 0
:err
echo Failed. See error above.
pause
exit /b 1
