import httpx


class TelegramError(Exception):
    pass


def send_message(token: str, chat_id: str, text: str, *, client: httpx.Client | None = None) -> dict:
    owns_client = client is None
    http = client or httpx.Client()
    try:
        try:
            response = http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except httpx.HTTPError as exc:
            raise TelegramError("Telegram request failed") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramError("Telegram returned an unreadable response") from exc
        if response.status_code >= 400 or not payload.get("ok"):
            raise TelegramError("Telegram did not accept the message")
        return payload
    finally:
        if owns_client:
            http.close()
