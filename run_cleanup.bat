@echo off
cd /d "%~dp0"
venv\Scripts\python.exe cleanup_test_users.py
pause
