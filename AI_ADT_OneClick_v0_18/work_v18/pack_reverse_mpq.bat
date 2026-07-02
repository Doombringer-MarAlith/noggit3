@echo off
setlocal
cd /d "%~dp0"
if not exist MPQEditor.exe (
  echo MPQEditor.exe not found next to pack_reverse_mpq.bat.
  echo Put Ladik's MPQEditor.exe here, then run this again.
  pause
  exit /b 1
)
if not exist output_reverse\world\maps (
  echo output_reverse does not exist. Run reverse_improve.bat first.
  pause
  exit /b 1
)
if exist patch-AI-REVERSE.MPQ del patch-AI-REVERSE.MPQ
MPQEditor.exe new patch-AI-REVERSE.MPQ
MPQEditor.exe add patch-AI-REVERSE.MPQ output_reverse\world world /r
MPQEditor.exe add patch-AI-REVERSE.MPQ output_reverse\World World /r
MPQEditor.exe add patch-AI-REVERSE.MPQ output_reverse\Textures Textures /r
MPQEditor.exe close patch-AI-REVERSE.MPQ
echo Created patch-AI-REVERSE.MPQ. Put it in your WoW 3.3.5a Data folder to test reverse output.
echo NOTE: Minimaps ship as a *_FRAGMENT.trs only by default. Merge it into your
echo extracted original Textures\Minimap\md5translate.trs. A full md5translate.trs is
echo only written when outputs.write_full_md5translate=true and would wipe stock minimaps.
pause
