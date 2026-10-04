@echo off
title PHOENIX MK8 - Dynamic Island (live, connected to the bot)
cd /d "%~dp0"

rem The ModernGL Dynamic Island (M.PY) wired to the real bot.
rem
rem   Enter on the island  -> type a task, Enter sends it to submit_text()
rem   Esc                  -> cancels a running turn, or closes the island
rem   drag a file onto it  -> the bot takes that turn with the real path
rem   keys 1-8 / T / click -> emotions, the file-pickup animation
rem
rem If you do not want start_vision() capturing your screen, uncomment:
rem set PHOENIX_NO_VISION=1
set PHOENIX_SILENT=1
set PHOENIX_LLAMA_SERVER=C:\Users\Yp921\Downloads\llama.cpp\llama-server.exe

python island_clone\phoenix_live.py
pause
