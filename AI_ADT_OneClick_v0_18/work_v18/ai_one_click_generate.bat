@echo off
setlocal
cd /d "%~dp0"
echo [1/3] Writing AI zone spec from zone_prompt.txt...
python ai_zone_spec.py --prompt-file zone_prompt.txt --out zone_spec.json
if errorlevel 1 goto :err
echo [2/3] Generating ADT/WDT/MH2O/WDL/minimap from zone_spec.json...
python generate_adt.py
if errorlevel 1 goto :err
echo [3/3] Validating generated output...
python validate_structure.py
if errorlevel 1 goto :err
echo.
echo Done. See zone_spec.json, ZONE_SPEC_BRIEF.md, output_loose\, and output_loose\GENERATION_REPORT.txt
pause
exit /b 0
:err
echo.
echo FAILED. See the error above.
pause
exit /b 1
