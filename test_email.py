import os
import sys
sys.path.append('.')
from dotenv import load_dotenv
load_dotenv()
from app.utils.email import send_siiqo_email

try:
    send_siiqo_email('test@siiqo.com', 'Test Subject', 'test', test="Yes")
except Exception as e:
    print(f"Error: {e}")
