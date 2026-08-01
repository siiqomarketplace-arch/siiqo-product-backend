#!/usr/bin/env python3
"""
run_monday_recap.py — Run Monday morning weekly recap task
Add to cron (every Monday at 8am WAT / 7am UTC):
  0 7 * * 1 cd /path/to/backend && python run_monday_recap.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.tasks.recap_tasks import run_monday_recap_task

if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        try:
            run_monday_recap_task()
        except Exception as e:
            print(f"ERROR: {e}")
            sys.exit(1)
