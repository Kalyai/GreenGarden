"""Inside app container: python backend/max_setup.py check|subscribe|status."""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from max_bot import MaxClient, MaxAPIError, NoRedirect, UPDATE_TYPES, settings, valid_webhook_url


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    config = settings(os.environ.get("DATA_DIR", "") or Path(__file__).parent)
    token = config["MAX_BOT_TOKEN"]
    secret = config["MAX_WEBHOOK_SECRET"]
    username = config["MAX_BOT_USERNAME"].lstrip("@")
    url = config["MAX_WEBHOOK_URL"]
    if not token:
        raise ValueError("Задайте MAX_BOT_TOKEN в .env и пересоздайте app")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", username):
        raise ValueError("Задайте MAX_BOT_USERNAME без @")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
        raise ValueError("Задайте MAX_WEBHOOK_SECRET: случайная строка из 32–256 символов")
    ca = config["MAX_CA_FILE"] or str(Path(__file__).parent / "certs" / "max-ca.pem")
    client = MaxClient(token, float(os.environ.get("MAX_TIMEOUT", "10")), ca)
    me = client.request("GET", "/me")
    if not me.get("is_bot") or (username and me.get("username", "").lstrip("@") != username):
        raise ValueError("MAX_BOT_USERNAME не соответствует токену")
    print("MAX API: соединение и токен проверены; бот:", me.get("username", ""))
    if command == "subscribe":
        if not valid_webhook_url(url):
            raise ValueError("MAX_WEBHOOK_URL должен иметь вид https://ваш-домен/api/max/webhook на порту 443")
        probe = urllib.request.Request(url, method="POST",
                    data=b'{"update_type":"health_check","timestamp":0}',
                    headers={"Content-Type": "application/json", "X-Max-Bot-Api-Secret": secret})
        try:
            with urllib.request.build_opener(NoRedirect()).open(probe, timeout=10) as response:
                if response.status != 200:
                    raise ValueError("Webhook пока не отвечает HTTP 200")
        except OSError:
            raise ValueError("Webhook недоступен: проверьте обновление обоих контейнеров, HTTPS и MAX_WEBHOOK_SECRET") from None
        client.request("POST", "/subscriptions", body={"url": url, "secret": secret, "update_types": UPDATE_TYPES})
        print("Webhook зарегистрирован:", url)
    elif command == "status":
        result = client.request("GET", "/subscriptions")
        for record in result.get("subscriptions", []):
            print(json.dumps({"url": record.get("url"), "update_types": record.get("update_types")}, ensure_ascii=False))
    elif command != "check":
        raise ValueError("Допустимые команды: check, subscribe, status")


if __name__ == "__main__":
    try:
        main()
    except (MaxAPIError, ValueError) as exc:
        print("MAX:", exc, file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print("MAX: ошибка настройки", type(exc).__name__, file=sys.stderr)
        sys.exit(1)
