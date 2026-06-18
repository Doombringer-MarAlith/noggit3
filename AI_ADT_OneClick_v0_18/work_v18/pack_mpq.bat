@echo off
setlocal
cd /d "%~dp0"
if not exist MPQEditor.exe (
  echo MPQEditor.exe not found next to pack_mpq.bat.
  echo Put Ladik's MPQEditor.exe here, then run this again.
  pause
  exit /b 1
)
if exist patch-AI.MPQ del patch-AI.MPQ
MPQEditor.exe new patch-AI.MPQ
MPQEditor.exe add patch-AI.MPQ output_loose\world world /r
MPQEditor.exe add patch-AI.MPQ output_loose\World World /r
MPQEditor.exe add patch-AI.MPQ output_loose\Textures Textures /r
MPQEditor.exe close patch-AI.MPQ
echo Created patch-AI.MPQ. Put it in your WoW 3.3.5a Data folder.
echo NOTE: This includes a minimal Textures\Minimap\md5translate.trs test file.
echo For a serious patch, merge md5translate_AIGEN_FRAGMENT.trs into the original file instead.
pause
