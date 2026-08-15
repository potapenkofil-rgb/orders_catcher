#!/usr/bin/env python3
"""Получить строку сессии на своём компьютере, а не на сервере.

Зачем: Телеграм часто молча не доставляет код входа, если запрос пришёл
с серверного IP (хостинг, VPS) или с только что созданного api_id. С домашнего
интернета код приходит нормально. Логинимся тут, а в бота отдаём уже готовую
строку сессии — она переносимая и никаких кодов больше не потребует.

Как пользоваться:

    pip install telethon
    python tools/make_session.py

Скрипт спросит api_id, api_hash, телефон и код, потом напечатает строку сессии.
Её целиком отдай боту командой /session — он подключит аккаунт без входа.

Строка сессии = полный доступ к аккаунту. Никому не пересылай.
"""

import asyncio
import sys

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    sys.exit("Сначала поставь telethon:  pip install telethon")

DEVICE = dict(
    device_model="Desktop",
    system_version="Windows 10",
    app_version="5.3.1",
)


async def main() -> None:
    print("Данные приложения берутся на my.telegram.org → API development tools\n")
    try:
        api_id = int(input("api_id: ").strip())
    except ValueError:
        sys.exit("api_id — это число")
    api_hash = input("api_hash: ").strip()
    phone = input("Телефон (+79991234567): ").strip()

    client = TelegramClient(StringSession(), api_id, api_hash, **DEVICE)
    await client.start(phone=lambda: phone)

    me = await client.get_me()
    name = " ".join(p for p in [me.first_name or "", me.last_name or ""] if p)
    session = client.session.save()
    await client.disconnect()

    print(f"\nВошли как {name} (@{me.username or '—'})")
    print("\nСтрока сессии — отдай её боту командой /session:\n")
    print(session)
    print("\nЭто полный доступ к аккаунту. Никому не пересылай.")


if __name__ == "__main__":
    asyncio.run(main())
