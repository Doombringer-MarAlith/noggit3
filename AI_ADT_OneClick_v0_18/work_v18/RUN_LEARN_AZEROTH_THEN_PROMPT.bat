@echo off
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: RUN_LEARN_AZEROTH_THEN_PROMPT.bat C:\path\to\extracted\world\maps\azeroth
  echo.
  echo Example after extracting client MPQs:
  echo   RUN_LEARN_AZEROTH_THEN_PROMPT.bat C:\WoWExtract\world\maps\azeroth
  echo.
  pause
  exit /b 1
)
echo [1/5] Learning local Blizzlike texture/style rules from: %~1
python learn_blizzlike_rules.py "%~1" -o learned_blizzlike_rules.json
if errorlevel 1 goto :err
echo [2/5] Writing zone_spec.json from zone_prompt.txt.
python ai_zone_spec.py --prompt-file zone_prompt.txt --out zone_spec.json
if errorlevel 1 goto :err
echo [3/5] Generating WDT/ADT/MH2O/WDL/minimap using learned_blizzlike_rules.json.
python generate_adt.py
if errorlevel 1 goto :err
echo [4/5] Validating generated output.
python validate_structure.py output_loose
if errorlevel 1 goto :err
echo [5/5] Done. Run pack_mpq.bat if desired.
echo.
echo Output folder: output_loose
echo Learned rules: learned_blizzlike_rules.json
echo Brief: ZONE_SPEC_BRIEF.md
echo.
pause
exit /b 0
:err
echo.
echo FAILED. See error above.
pause
exit /b 1
