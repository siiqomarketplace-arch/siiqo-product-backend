@echo off
REM run_escrow_cron.bat - Windows script to run Siiqo Escrow Background Tasks
REM Set this script up in Windows Task Scheduler to run hourly or daily.

cd /d "%~dp0"
echo Running Siiqo Escrow Tasks at %date% %time%
python run_escrow_tasks.py
echo Tasks completed at %date% %time%
