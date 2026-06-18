@echo off
setlocal
cd /d "%~dp0"
python make_sample_input.py
if errorlevel 1 pause
