# vendor

Пришпиленные копии клиентов чат-нейросетей. Используются бэкендом «аккаунты»
(`orderbot/llm/accounts.py`) — логин по email + паролю и работа через веб-API
аккаунта вместо платного ключа.

| Папка | Откуда | Что оставлено |
|---|---|---|
| `betterdeepseek/` | проект `betterDeepseek` (chat.deepseek.com) | логин и чаты: `client, auth, chat, files, ping, store, exceptions, pow_hash, pow_hash_fast, waf_solver, js/waf_shim.js` |
| `betterqwen/` | проект `betterQwen` (chat.qwen.ai) | логин и чаты: `client, auth, chat, files, ping, store, exceptions, bx_solver, oss_upload, tempmail, vendor/*.js` |

Копии не правились — обновлять простым перекладыванием файлов из исходных
проектов. Регистрация аккаунтов сюда не вошла: `browser_captcha.py`,
`gmail_reader.py`, `vision_solver.py` не копировались, поэтому
`DeepSeek.register()` / `Qwen.register()` тут не работают — аккаунты
создаются в исходных проектах, а сюда добавляются готовыми через `/addaccount`.

Зависимости этих копий вынесены в `requirements-accounts.txt` и импортируются
лениво: без аккаунтов ставить их не нужно.
