import os
import requests


def send_telegram_message(text: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing")

    if not chat_id:
        raise ValueError("TELEGRAM_CHAT_ID is missing")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()


def main() -> None:
    message = "✅ Job Alert Bot test message\nGitHub Actions 연결 테스트 성공"
    send_telegram_message(message)
    print("Telegram test message sent successfully.")


if __name__ == "__main__":
    main()
