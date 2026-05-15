#!/usr/bin/env python3
"""
run_escrow_tasks.py — Run escrow background tasks
Add this to your cron job or task scheduler:

Linux/Mac cron:
  # Run every hour
  0 * * * * cd /path/to/backend && python run_escrow_tasks.py >> logs/escrow_tasks.log 2>&1

Windows Task Scheduler:
  - Program: python
  - Arguments: C:\path\to\backend\run_escrow_tasks.py
  - Start in: C:\path\to\backend
  - Trigger: Hourly

Or use APScheduler, Celery, or similar for production.
"""
import sys
import os

# Add the backend directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.tasks.escrow_tasks import run_all_escrow_tasks

if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        try:
            run_all_escrow_tasks()
        except Exception as e:
            print(f"ERROR: {e}")
            sys.exit(1)
