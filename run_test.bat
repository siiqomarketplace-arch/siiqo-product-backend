@echo off
chcp 65001 > nul
cd /d "%~dp0"
venv\Scripts\python.exe test_signup_flow.py > test_result.txt 2>&1
type test_result.txt
