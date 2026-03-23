@echo off
pip install pyinstaller
pyinstaller --onefile --windowed --name "SoulSeek Agent" --icon icon.ico agent.py
echo.
echo Build complete. Check dist\ folder for the executable.
pause
