@echo off
setlocal
python reverse_tool.py analyze
if errorlevel 1 pause
