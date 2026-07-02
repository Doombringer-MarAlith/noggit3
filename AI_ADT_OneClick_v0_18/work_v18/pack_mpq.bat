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
echo NOTE: Minimaps ship as a *_FRAGMENT.trs only by default. Merge it into your
echo extracted original Textures\Minimap\md5translate.trs. A full md5translate.trs is
echo only written when write_full_md5translate=true and would wipe stock minimaps.
pause
