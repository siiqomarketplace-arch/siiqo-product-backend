@echo off
cd /d "%~dp0"
venv\Scripts\python.exe check_test_users.py
pause
