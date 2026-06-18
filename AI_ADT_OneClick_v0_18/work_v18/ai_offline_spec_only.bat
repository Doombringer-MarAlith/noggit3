@echo off
setlocal
cd /d "%~dp0"
python ai_zone_spec.py --prompt-file zone_prompt.txt --out zone_spec.json --force-offline
pause
