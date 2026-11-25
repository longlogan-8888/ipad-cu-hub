@echo off
rem Tat PC Remote dang chay ngam
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*pc-remote*server.py*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; 'Da tat PC Remote.' } else { 'PC Remote khong chay.' }"
pause
