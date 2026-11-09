# Тест прерываемых GPU-серверов Selectel для ScanBox AI

## Что проверяем

Эфемерность — наш жизненный цикл: создать VM под задачу и удалить после работы.
Прерываемость — условие аренды: провайдер может остановить VM до окончания задачи.
Для ScanBox можно совместить оба: **эфемерная прерываемая GPU VM**.

По документации Selectel прерываемый сервер работает не более 24 часов и может
быть остановлен раньше. Остановка не удаляет сервер: сетевые диски сохраняются,
данные локальных дисков теряются. Цена в среднем ниже на 70%, но сетевые диски
и публичные сетевые ресурсы продолжают оплачиваться после прерывания.
Это не обещание точной скидки на выбранную GPU-конфигурацию.
Источник: [Прерываемые серверы](https://docs.selectel.ru/cloud-servers/about/preemptible-servers/).

Для создания нужен тег `preemptible` и Nova microversion `2.72`.
Это теперь реализовано в `scripts/orchestrator.py`:

```http
POST <compute endpoint из service catalog>/servers
OpenStack-API-Version: compute 2.72
X-Auth-Token: <project scoped token>
```

```json
{
  "server": {
    "name": "scanbox-test",
    "flavorRef": "GPU_FLAVOR_ID",
    "imageRef": "GPU_IMAGE_ID",
    "availability_zone": "POOL_SEGMENT",
    "networks": [{"uuid": "NETWORK_ID"}],
    "security_groups": [{"name": "SSH_GROUP"}],
    "key_name": "EPHEMERAL_KEY_NAME",
    "tags": ["preemptible"]
  }
}
```

При `SELECTEL_BOOT_VOLUME_GB>0` код заменяет `imageRef` на
`block_device_mapping_v2` с `destination_type=volume` и
`delete_on_termination=true`.
Источник способа создания: [инструкция Selectel](https://docs.selectel.ru/cloud-servers/create/create-preemptible-server/).

## Тест без ML

Чтобы сначала проверить сам сервис, добавлен `scripts/selectel_smoke.py`.
Он использует реальный Selectel/OpenStack API, а не Cloud Run.

1. Проверяет заданные flavor/image через read-only API.
2. Создаёт временную SSH keypair, затем прерываемую VM.
3. Сохраняет имя и ID ресурсов в JSON-отчёт до ожидания запуска.
4. Дожидается ACTIVE, проверяет серверный тег `preemptible`.
5. Подключается по SSH и запускает `nvidia-smi` для проверки GPU/драйвера.
6. В `finally` запрашивает удаление VM и дожидается Nova 404.
7. Проверяет через Cinder исчезновение подключённых сетевых дисков и удаляет keypair.

Образ должен уже содержать рабочие NVIDIA-драйверы и SSH. Smoke-тест не ставит
Hunyuan и не использует cloud-init для установки драйвера. Успех этого теста
подтверждает создание и доступность GPU, но не CUDA-вычисления, качество GLB
или устойчивость к фактическому вытеснению планировщиком Selectel.

Из корня репозитория:

```bash
python3 -m pip install -r scripts/requirements.txt
# Подготовьте .env по scripts/.env.example вне version control.
set -a
source .env
set +a

# Без сети, без ресурсов; JSON будущего запроса:
python3 -m scripts.selectel_smoke --plan

# Реальная авторизация и чтение flavor/image, без создания ресурсов:
python3 -m scripts.selectel_smoke --preflight

# Реальная аренда VM с GPU и последующее удаление:
python3 -m scripts.selectel_smoke --smoke --report /tmp/selectel-smoke.json
```

Настройки теста:

| Переменная | Значение |
| --- | --- |
| `SELECTEL_ACCOUNT_ID` | Domain аккаунта для OpenStack |
| `SELECTEL_USERNAME`, `SELECTEL_PASSWORD` | Учётные данные OpenStack project user |
| `SELECTEL_PROJECT_NAME` | Проект Selectel |
| `SELECTEL_REGION` | Регион endpoint, например `ru-7` |
| `SELECTEL_AVAILABILITY_ZONE` | Сегмент пула, например `ru-7a` |
| `SELECTEL_FLAVOR` | Точно выбранный GPU flavor ID/имя; тест не выбирает дорогую конфигурацию автоматически |
| `SELECTEL_IMAGE` | Образ с драйвером NVIDIA и SSH |
| `SELECTEL_NETWORK_ID` | UUID сети с маршрутом SSH от runner |
| `SELECTEL_SECURITY_GROUP` | Группа, разрешающая SSH от runner |
| `SELECTEL_SSH_USER` | Пользователь образа: например `ubuntu` или `root` |
| `SELECTEL_BOOT_VOLUME_GB` | Размер сетевого boot volume, либо `0` для локального загрузочного диска выбранного flavor |

Smoke-тест всегда включает `preemptible`, независимо от env. Он не создаёт
публичную сеть или floating IP и не открывает SSH-группу автоматически.
Нужны квоты GPU/CPU/RAM/дисков и права на Nova, keypairs, чтение образов,
создание boot volume и чтение Cinder. Каталог и образы API также зависят
от полномочий пользователя. Отдельно подтвердите наличие свободной GPU
ёмкости в выбранном сегменте — корректный API-запрос этого не гарантирует.

Для запуска из этой рабочей среды нужно подключить секреты и разрешить
HTTP-доступ к `cloud.api.selcloud.ru` и публичным compute/volume endpoints,
которые возвращает service catalog. Для SSH runner должен иметь TCP-маршрут
до VM: используйте рабочую станцию/VPN/runner с таким доступом. Одного
разрешения HTTP-доменов для Paramiko недостаточно. Секреты не отправляйте в чат.

## Результат и ошибки очистки

Exit code `0` у smoke означает, что проверка GPU и проверки очистки прошли.
При недоступном GPU/SSH, неверном теге, прерывании или ошибке удаления — код `1`.
Отчёт содержит `server_id`, `key_name`, `volume_ids`, `server_deleted`,
`volumes_deleted`, `cleanup_errors` и общую длительность; приватный SSH-ключ
и учётные данные в него не записываются.

Если create-запрос оборвался до ответа, `creation_uncertain=true`:
сервер мог быть создан, даже когда ID неизвестен. Автоматический повтор
не выполняется, чтобы не создать вторую платную VM. Сначала найдите сервер
по уникальному имени из отчёта в панели или через `openstack server list`,
сверьте metadata `scanbox-job`, затем удалите именно тестовые ресурсы.
Отчёт сохранит имя keypair даже при частичной ошибке её создания.

Если runner принудительно завершён, `finally` не гарантирован. Проверьте
ресурсы вручную или внешним cleanup-процессом. Ограничение аренды в 24 часа
не заменяет удаление сетевых дисков. Не используйте `--keep-on-error` для smoke:
такого режима у него нет.

## Использование в существующем ScanBox API

`Config.from_env()` читает `SELECTEL_PREEMPTIBLE=1`, поэтому существующий
`POST /api/upload` использует прерываемые VM при запуске с `SELECTEL_MOCK=0`.
Локальная demo по умолчанию по-прежнему mock:

```bash
SELECTEL_PREEMPTIBLE=1 SELECTEL_MOCK=0 uvicorn api.app:app --host 127.0.0.1 --port 8000
```

Standalone-пайплайн:

```bash
python3 scripts/orchestrator.py --preemptible --photo photo.png --out model.glb
```

Не считайте это проверкой реального Hunyuan: прежний `scripts/generate.py`
содержит placeholder-команду `run.py` и может переключаться на mock.
Сначала завершите независимый smoke, затем настройте ML-образ и реальный
entrypoint модели, который явно завершится ошибкой при отсутствии модели.

API Selectel/OpenStack сообщает остановку как `SHELVED_OFFLOADED`, панель —
как `EXPIRED`. Оркестратор проверяет эти состояния при ожидании VM и
во время SSH-генерации. В API задач результат будет `status=failed`,
`error_code=server_preempted`; частичный GLB не считается успешным.
Ручной offload прерываемой VM имеет тот же статус и тоже считается прерыванием.
Источник: [статусы Selectel](https://docs.selectel.ru/cloud-servers/manage/server-statuses/).

Автоматический retry/resume не добавлен: для него нужен сохранённый вход,
чекпоинты и ограниченная политика повторов. Для остановленного сервера
Selectel описывает `openstack server unshelve`, но для одноразового ScanBox job
текущая политика — закончить задачу ошибкой и удалить тестовую VM.
Восстановление VM не означает восстановление работающего процесса модели.
Источник: [восстановление](https://docs.selectel.ru/cloud-servers/manage/restore-preemptible-server/).

## Что уже проверено локально

```bash
python3 -m unittest discover -s scripts/tests -v
```

Проверяются запрос с тегом и microversion, региональный public endpoint,
удаление сетевого boot volume по настройке, распознавание прерывания,
smoke lifecycle, ошибочный GPU/тег, ошибки очистки и неоднозначный create.
В этих тестах API/SSH подменены; аренда у Selectel не выполняется.

На момент подготовки реальный preflight остановился до сети из-за отсутствия
параметров и учётных данных. Совместимость с выбранным проектом, GPU-квотой,
конкретным образом и сетью пока не подтверждена живым тестом.
