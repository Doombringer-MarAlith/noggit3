@echo off
setlocal
cd /d "%~dp0"
echo [1/4] Writing zone_spec.json from zone_prompt.txt. Uses real AI only if AI_API_KEY/OPENAI_API_KEY is set.
python ai_zone_spec.py --prompt-file zone_prompt.txt --out zone_spec.json
if errorlevel 1 goto :err
echo [2/4] Generating new-map WDT/ADT terrain, alphamaps, MH2O water, WDL, and minimap files.
python generate_adt.py
if errorlevel 1 goto :err
echo [3/4] Validating reversed WoW chunk magic, MCIN/MCNK/MCVT/MCLY/MCAL, WDT, WDL, BLP.
python validate_structure.py output_loose
if errorlevel 1 goto :err
echo [4/4] Done. Optional: run pack_mpq.bat if MPQEditor.exe is next to this file.
echo.
echo Output folder: output_loose
echo If your prompt said "for map X", the generated directory is usually output_loose\world\maps\x
echo.
pause
exit /b 0
:err
echo.
echo FAILED. See error above.
pause
exit /b 1
