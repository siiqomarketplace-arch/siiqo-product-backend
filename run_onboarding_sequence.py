#!/usr/bin/env python3
"""
run_onboarding_sequence.py — Run onboarding email sequence background task
Add to cron (e.g. daily at 9am):
  0 9 * * * cd /path/to/backend && python run_onboarding_sequence.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.tasks.onboarding_tasks import run_onboarding_email_sequence

if __name__ == '__main__':
    app = create_app()
    with app.app_context():
        try:
            run_onboarding_email_sequence()
        except Exception as e:
            print(f"ERROR: {e}")
            sys.exit(1)
