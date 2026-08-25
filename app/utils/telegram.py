"""
telegram.py
Central utility for pushing direct notifications via the Siiqo Telegram Bot.
Uses standard requests to api.telegram.org. Fails gracefully without throwing exceptions.
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

def send_telegram_message(chat_id: str | int, text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> bool:
    """
    Sends a message via the official Siiqo Telegram bot to a given chat_id / telegram_id.
    
    Args:
        chat_id: The Telegram user ID or group chat ID.
        text: Message content (supports HTML tags like <b>, <i>, <code>, <a href="...">).
        parse_mode: 'HTML' or 'MarkdownV2' (defaults to HTML for safety).
        reply_markup: Optional inline keyboard or button layout dictionary.
        
    Returns:
        bool: True if sent successfully (HTTP 200), False otherwise.
    """
    if not chat_id:
        return False

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.warning("[TELEGRAM PUSH] TELEGRAM_BOT_TOKEN is not set in environment. Skipping Telegram message.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        response = requests.post(url, json=payload, timeout=6)
        if response.status_code == 200:
            logger.info(f"[TELEGRAM PUSH SUCCESS] Sent message to chat_id={chat_id}")
            return True
        else:
            logger.warning(f"[TELEGRAM PUSH FAIL] HTTP {response.status_code} to chat_id={chat_id}: {response.text}")
            return False
    except Exception as e:
        logger.error(f"[TELEGRAM PUSH ERR] Network or request error for chat_id={chat_id}: {e}")
        return False
