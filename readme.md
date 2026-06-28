## Music Light

### Веб-сервис пресетов

Лампы берутся из `app/core/lamps.json`. Пресеты хранятся локально в
`backend/data/presets.json`, SQL не используется.

Пароли задаются через переменные окружения или `.env`:

```env
MUSICLIGHT_ADMIN_PASSWORD=change-admin-password
MUSICLIGHT_USER_PASSWORD=change-user-password
MUSICLIGHT_DRY_RUN=0
```

Для локальной проверки без отправки команд на лампы:

```powershell
python -m backend.server --host 127.0.0.1 --port 8000 --dry-run
```

Для работы с реальными лампами:

```powershell
python -m backend.server --host 0.0.0.0 --port 8000 --live
```

После запуска откройте `http://127.0.0.1:8000`. Если пароли не заданы,
используются локальные значения `admin` и `user`.

При старте backend один раз запускает диагностику ламп и сохраняет результат
в `backend/data/state.json`. В админ-панели есть кнопка `Проверить`, которая
повторяет диагностику вручную.

В редакторе пресета каждая лампа включается в пресет отдельно. Если лампа не
отмечена, backend отправляет ей явную команду выключения через switch DP.
Эффекты `Огонь`, `Пульс` и `Волна` запускаются backend-циклом и работают до
применения следующего пресета.

### Сканирование Tuya

```powershell
python -m tinytuya scan
```

Tuya Cloud Explorer:
https://eu.platform.tuya.com/cloud/explorer?id=p1754286167231pstvaj&groupId=group-469000990949404&interfaceId=470224448454738&abilityId=1667049392684671055
