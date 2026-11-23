# Проверка ScanBox AI на Selectel: от read-only до GPU VM

Этот гайд рассчитан на Windows + WSL Ubuntu + Docker Desktop. Все команды
выполняются в WSL из корня репозитория ScanBox AI, если не указано иное.

Цель первого теста: создать **одну платную прерываемую VM**, проверить
доступ к ней по SSH и работу NVIDIA-драйвера, затем подтвердить удаление
VM, её тестовых сетевых дисков и временной SSH keypair.
Генерация 3D-модели и подготовка ML-образа — отдельный следующий этап.

## 1. Проверить Docker и актуальность кода

**Без создания облачных ресурсов.**

```bash
cd /путь/к/scanbox_ai
git pull --ff-only
docker version
docker compose version
```

В `docker version` должны присутствовать **Client и Server**.
Ошибка `/var/run/docker.sock: no such file or directory` означает, что
Docker CLI не подключился к daemon. Запустите Docker Desktop, включите
WSL 2 engine и интеграцию с вашей Ubuntu, затем заново откройте WSL.
Путь в проекте менять не нужно. Если ошибка остаётся, проверьте
`docker context ls` и наличие старой настройки `DOCKER_HOST`.

Не используйте `git reset --hard`, если обновление остановилось из-за ваших
локальных изменений: сохраните их отдельно или закоммитьте.

## 2. Загрузить настройки и авторизацию

RC-файл содержит параметры авторизации **сервисного пользователя**.
`.env` содержит параметры ScanBox: flavor, образ ОС, сеть, группу безопасности.
Это разные файлы. Храните RC вне репозитория.

Если `.env` ещё нет, скопируйте шаблон один раз:

```bash
cp scripts/.env.example .env
```

Если файл уже настроен, не перезаписывайте его шаблоном.

Пример `.env` для выбранной конфигурации:

```dotenv
SELECTEL_AUTH_SOURCE=openstack
SELECTEL_FLAVOR=13960
SELECTEL_AVAILABILITY_ZONE=ru-6a
SELECTEL_IMAGE=18ae9eeb-bd22-47f4-b03b-0eba5da50729
SELECTEL_NETWORK_ID=ЗАМЕНИТЬ_НА_ПРОВЕРЕННЫЙ_UUID_СЕТИ
SELECTEL_SECURITY_GROUP=ЗАМЕНИТЬ_НА_ID_ИЛИ_ИМЯ_ГРУППЫ
SELECTEL_SSH_USER=root
SELECTEL_BOOT_VOLUME_GB=200
SELECTEL_WORKER_IMAGE=
```

Значения выше — пример для L4 в `ru-6a`, не универсальные настройки:

- Flavor `13960` был выбран через интерфейс: `GL21.8-32768-1GPU`,
  8 vCPU и 32 GiB RAM. Наличие свободной GPU-ёмкости пока не подтверждено.
- Образ `18ae9eeb-bd22-47f4-b03b-0eba5da50729` в полученном списке —
  **Ubuntu 22.04 LTS 64-bit GPU Driver 580 Docker**. Перед запуском
  проверьте его доступность и минимальный размер диска в своём регионе.
- `SELECTEL_SSH_USER` — Linux-пользователь образа, а не имя сервисного
  пользователя Selectel. `root` приведён как пример; подтвердите пользователя
  по свойствам/описанию выбранного образа.
- `SELECTEL_BOOT_VOLUME_GB=200` означает создание сетевого boot volume.
  Выберите размер по требованиям образа и конфигурации. Для загрузки с
  локального диска подходящего flavor указывается `0`; нельзя выбирать
  тип загрузки без проверки выбранного flavor.
- `SELECTEL_WORKER_IMAGE=` пока оставляем пустым: первый smoke проверяет
  `nvidia-smi` на VM, а не Docker worker.

Загрузите файлы **в той же сессии**, где будут выполняться команды:

```bash
set -a
source .env
source /полный/путь/к/rc.sh
set +a
export SELECTEL_AUTH_SOURCE=openstack
```

RC может запросить пароль. Не сохраняйте его в Dockerfile или build args.
Регион для RC-авторизации берётся из `OS_REGION_NAME`; в этом примере нужен
`ru-6`, а `ru-6a` — отдельный параметр сегмента, не замена региону.

Compose умеет автоматически читать `.env`, но команды ниже также используют
shell-переменные вроде `"$SELECTEL_IMAGE"`. Поэтому здесь мы явно экспортируем
`.env` через `set -a`. RC сам по себе контейнером не исполняется:
экспортированные `OS_*` передаются runner через Compose.
После открытия нового терминала загрузку нужно повторить.

## 3. Проверить окружение и read-only API

**Без создания VM, дисков и SSH-ключей.**

```bash
docker compose build selectel
docker compose run --rm selectel --check-env
docker compose run --rm selectel --preflight
```

Сборка runner устанавливает локальные зависимости; после изменения кода
runner нужно пересобрать. GPU-образ для этого этапа не требуется.

Ожидаемые признаки:

| Проверка | Успешный результат |
| --- | --- |
| `--check-env` | `auth_source: openstack`, наличие `OS_PASSWORD`, username/user ID, project ID/name и региона |
| `--preflight` | `auth: ok`, ожидаемые project/region, `resources_created: false`, список flavors |
| Пустой список серверов | `servers_on_first_page: 0` — нормальный успешный результат |

`OS_USER_ID: false` допустим, если есть `OS_USERNAME`.
`OS_PROJECT_NAME: false` допустим, если есть `OS_PROJECT_ID`.
Диагностика показывает наличие переменных, не их значения.

Если явно задан `SELECTEL_IMAGE`, preflight дополнительно читает образ через
Glance. Роль только для просмотра compute может не давать прав чтения образов.

Успешный preflight **не подтверждает** право создавать VM, свободную GPU,
сетевой доступ SSH или работоспособность CUDA.

## 4. Проверить образ ОС, сеть и SSH-группу

**Пока только чтение.** Для сокращения команд определим функцию:

```bash
osc() {
  docker compose run --rm --entrypoint openstack selectel "$@"
}
```

Она запускает OpenStack CLI в runner. Функция действует только в текущем shell.

```bash
osc flavor show "$SELECTEL_FLAVOR" -f json
osc image show "$SELECTEL_IMAGE" -f json
osc network list --long -f json
osc security group list -f json
```

Используйте **ID ресурсов Selectel**, а не локальные Docker image IDs.

В предоставленном ранее выводе были найдены:

| Ресурс | ID | Что ещё нужно подтвердить |
| --- | --- | --- |
| Образ Ubuntu 22.04 GPU Driver 580 Docker | `18ae9eeb-bd22-47f4-b03b-0eba5da50729` | Доступность, свойства, boot disk и SSH-пользователь |
| `external-network` | `fad06e74-fb6e-4188-8a7a-dfb9f245c97f` | Способ подключения VM, port security и маршрут SSH |
| Группа `default` | `5f0ff8f3-3d85-491f-a196-96acf92347ed` | Входящий SSH от адреса runner |

Эти сетевые ID относятся к конкретному окружению, не копируйте их
в другой проект без проверки.

**Нельзя считать `external-network` готовой сетью VM только по её названию.**
Она может участвовать в маршрутизации/выделении публичных IP. Нужен
подтверждённый вариант подключения будущей VM: публичная сеть/подсеть
или подготовленная приватная сеть с доступным публичным IP/VPN.

Текущий скрипт не создаёт роутер, не выделяет floating IP и не настраивает
VPN. Он подключает VM к указанной сети и пробует SSH на найденный IPv4.
Приватный IP сам по себе недоступен из вашей WSL через интернет.
Если видна только `external-network`, сначала определите схему сети в панели
Selectel; этого вывода недостаточно для запуска платного smoke.

После выбора сети и группы замените placeholders в `.env`, повторно
загрузите настройки из этапа 2 и проверьте:

```bash
osc network show "$SELECTEL_NETWORK_ID" -f json
osc security group rule list "$SELECTEL_SECURITY_GROUP" -f json
```

Для назначения security group требуется включённый **port security**.
Для SSH нужно правило входящего TCP 22 от публичного адреса вашего
компьютера/runner (`/32`) либо от вашей VPN-подсети. Адрес Docker-контейнера
`172.x.x.x` и домашний `192.168.x.x` обычно не являются адресом, который
видит VM при подключении через интернет.

Имя `default` не означает, что SSH уже разрешён. При отсутствии правила
можно добавить его отдельно. Следующая команда **изменяет сетевое правило**
и требует прав записи; подставьте свой настоящий адрес:

```bash
RUNNER_CIDR="ВАШ_ПУБЛИЧНЫЙ_IP/32"
osc security group rule create --ingress --ethertype IPv4 --protocol tcp \
  --dst-port 22 --remote-ip "$RUNNER_CIDR" "$SELECTEL_SECURITY_GROUP"
```

Общая группа применяется ко всем использующим её VM. Не открывайте SSH
для всего интернета только ради теста. Отдельная временная keypair для
тестовой VM будет создана самим скриптом.

**Переход к следующему этапу:** образ и тип диска выбраны, сеть/маршрут
подтверждены, группа разрешает SSH, placeholders заменены.
Одного успешного preflight для этого недостаточно.

## 5. Посмотреть будущий запрос создания

**Без обращения к API создания.**

```bash
docker compose run --rm selectel --plan
```

В JSON должны быть:

- `flavorRef: "13960"`, нужные image ID и `availability_zone: "ru-6a"`;
- сеть по UUID и выбранная security group;
- `tags: ["preemptible"]`;
- заголовок `OpenStack-API-Version: compute 2.72`;
- при сетевом boot volume — `block_device_mapping_v2` с нужным размером
  и `delete_on_termination: true` вместо верхнего `imageRef`.

План показывает форму запроса. Он **не выполняет серверную валидацию**
квот, доступности ресурсов, размера диска или сетевого доступа.

## 6. Запустить платный smoke-тест одной VM

Для этого этапа сервисному пользователю нужны права на создание и удаление
VM/keypairs/boot volume, использование сети и чтение Cinder для проверки дисков.
Read-only роли недостаточно. Права администратора всего аккаунта не являются
обязательным условием: используйте нужные права в нужном проекте.

На этом шаге будет арендована **реальная платная GPU VM**:

```bash
REPORT="smoke-$(date +%Y%m%d-%H%M%S).json"

docker compose run --rm selectel --smoke --report "/reports/$REPORT"
echo "Smoke exit code: $?"

docker compose run --rm --entrypoint cat selectel "/reports/$REPORT"
```

Сохраните имя `REPORT`; отчёт находится в именованном Docker volume
`reports` и переживает удаление runner-контейнера через `--rm`.
Не запускайте `docker compose down -v` до сохранения нужных отчётов.

Скрипт последовательно:

1. Читает выбранные flavor и образ.
2. Создаёт временную SSH keypair.
3. Создаёт прерываемую VM и сохраняет её ID в отчёт.
4. Ждёт ACTIVE и проверяет серверный тег `preemptible`.
5. Подключается по SSH и выполняет `nvidia-smi`.
6. Запрашивает удаление VM и ждёт её исчезновения из Nova.
7. Проверяет исчезновение подключённых сетевых дисков и удаляет keypair.

Финальный JSON может появиться не сразу: ожидание VM, SSH и очистка имеют
отдельные таймауты. Отсутствие новых строк в терминале само по себе не
означает зависание. Имя и известные resource IDs сохраняются в отчёт по ходу теста.

Не запускайте несколько одинаковых повторов, пока первый не завершён.
Для этого smoke-теста нет режима «оставить VM»: успешный сервер также удаляется.

## 7. Проверить результат и очистку

Пример ключевых полей успешного отчёта:

```json
{
  "status": "passed",
  "preemptible": true,
  "server_deleted": true,
  "volumes_deleted": true,
  "key_deleted": true,
  "creation_uncertain": false,
  "cleanup_errors": []
}
```

В реальном отчёте также будут `name`, `server_id`, `key_name`,
`volume_ids`, результат проверки GPU и длительность.
Для локального boot disk `volumes_deleted: true` может означать,
что отдельных подключённых Cinder-дисков для проверки не было.

Успех первого smoke подтверждает API создания, запуск выбранной VM,
тег прерываемости, SSH, доступность NVIDIA-драйвера и выполненные проверки
очистки. Он не доказывает Hunyuan inference, правильность GLB, поведение
планировщика при настоящем вытеснении или устойчивость к будущим прерываниям.

При ошибке сначала читайте `error`, `cleanup_errors`,
`creation_uncertain` и флаги удаления. `status: failed` не обязательно
означает, что остались ресурсы; успешная очистка также отражается в отчёте.

### Если создание неоднозначно или очистка не подтверждена

При `creation_uncertain: true` запрос создания мог не вернуть ID.
**Сначала найдите VM по уникальному имени из отчёта, затем решайте о повторе.**

```bash
VM_NAME="ИМЯ_ИЗ_ПОЛЯ_name"
osc server list --name "$VM_NAME" --long -f json
```

Перед любым ручным удалением сверьте имя, проект, ID и metadata
`scanbox-job`. Не удаляйте все серверы или все диски проекта.

При известном ID:

```bash
SERVER_ID="ID_ТЕСТОВОЙ_VM"
osc server show "$SERVER_ID" -f json
```

Если это именно тестовая VM и её удаление ещё нужно выполнить,
следующая команда **удаляет ресурс**:

```bash
osc server delete --wait "$SERVER_ID"
```

Для дисков и ключа из отчёта сначала выполните read-only проверки:

```bash
VOLUME_ID="ID_ДИСКА_ИЗ_ОТЧЁТА"
KEY_NAME="ИМЯ_КЛЮЧА_ИЗ_ОТЧЁТА"
osc volume show "$VOLUME_ID" -f json
osc keypair show "$KEY_NAME" -f json
```

Ответ «не найден» подтверждает отсутствие конкретного ресурса только при
корректных проекте/регионе и достаточных правах чтения. Ошибка 403 или сети
не подтверждает удаление. Для известного оставшегося тестового диска проверьте
attachments и отсутствие другого использования. Его и тестовый ключ можно
удалить отдельно **только после проверки принадлежности**:

```bash
osc volume delete "$VOLUME_ID"
osc keypair delete "$KEY_NAME"
```

Если IDs дисков не успели попасть в отчёт, проверяйте связанные ресурсы в
панели/Cinder по времени создания и принадлежности тесту. Сетевой диск
не считается удалённым только потому, что VM остановлена или исчезла из списка.

Если runner был принудительно завершён, его `finally` не гарантирован.
Ограничение времени жизни прерываемой VM не заменяет очистку дисков/IP.
Этот скрипт сам floating IP и роутеры не создаёт.

## 8. Следующий этап: контейнер и реальная модель

Для первого smoke `SELECTEL_WORKER_IMAGE` был пустым. После него VM уже удалена.

Для проверки Docker worker нужна отдельно подготовленная GPU VM/OS image
с NVIDIA-драйвером, Docker, NVIDIA Container Toolkit и заранее загруженным
worker-образом. **Локально собранный Docker-образ не появляется на VM сам.**

На GPU-хосте, где доступны `compose.gpu.yaml` и необходимые каталоги,
проверка контейнера:

```bash
docker compose -f compose.gpu.yaml run --rm worker doctor
```

Она выполняет реальное матричное вычисление CUDA. На ноутбуке без GPU эта
команда не подтверждает работоспособность сервера Selectel.

Когда образ worker уже находится на создаваемых VM, можно заполнить
`SELECTEL_WORKER_IMAGE=REGISTRY/worker@sha256:DIGEST`: smoke вместо
`nvidia-smi` запустит контейнерный `doctor` с `--pull never`.
Для генерации нужны также заранее подготовленные веса и
`SELECTEL_MODEL_DIR`. Инструкции: [Docker runner и GPU worker](docker/README.md).
На L4 текущий worker делает **геометрию Hunyuan shape без PBR-текстур**.

## Частые ошибки

| Сообщение/ситуация | Что проверить |
| --- | --- |
| Нет `docker.sock` или Server в `docker version` | Docker Desktop, WSL integration, Docker context |
| Ошибка `SELECTEL_PASSWORD` при использовании RC | Обновить код и пересобрать runner; явно выбрать `SELECTEL_AUTH_SOURCE=openstack` |
| `OS_PASSWORD: false` | Пароль экспортирован в той же shell; RC загружен после старого `.env` |
| HTTP 401 | Пароль, user/domain, project scope и актуальность RC |
| HTTP 403 | Права на конкретную операцию: чтение compute, Glance, создание, удаление или Cinder |
| Не найден compute/image endpoint | `OS_REGION_NAME` и service catalog выбранного проекта |
| HTTP 400/409 или VM в ERROR | Параметры запроса, совместимость образа/flavor/диска/сети, квота и доступность; сохранить точную ошибку |
| VM ACTIVE, но SSH timeout | Маршрут/публичный IP/VPN, TCP 22, security group, port security, SSH-пользователь и готовность SSH |
| `nvidia-smi` не найден/не работает | Выбранный OS image и драйвер NVIDIA |
| `EXPIRED` / `SHELVED_OFFLOADED` | Прерывание/offload; результат не считать успешным, проверить очистку |
| `cleanup_errors` или неполный отчёт | Проверить конкретные VM/volume/keypair до повторного запуска |
| С контейнером `doctor` не работает, а `nvidia-smi` работает | Docker/права пользователя, NVIDIA Container Toolkit и наличие нужного image digest на VM |

Для диагностики можно передать вывод `--check-env`, финальный smoke JSON
и точную ошибку. Не публикуйте RC, пароль, токен, полный вывод `env` или
отрендеренный `docker compose config`: последний содержит runtime credentials.

## Если используете uv вместо Docker runner

После того же экспорта `.env` и RC read-only проверка:

```bash
uv run --with-requirements scripts/requirements.txt \
  python -m scripts.selectel_smoke --preflight
```

Платный smoke с отчётом в локальном каталоге WSL:

```bash
mkdir -p reports
uv run --with-requirements scripts/requirements.txt \
  python -m scripts.selectel_smoke --smoke --report "reports/$REPORT"
```

Переменную `REPORT` задайте как на этапе 6. Смысл режимов и проверки
очистки тот же. `uv` не требует отдельной активации venv.

## Источники и текущее состояние

- [Прерываемые серверы Selectel](https://docs.selectel.ru/cloud-servers/about/preemptible-servers/).
- [Создание прерываемого сервера](https://docs.selectel.ru/cloud-servers/create/create-preemptible-server/).
- [Назначение security group и port security](https://docs.selectel.ru/cloud-servers/security-groups/manage-groups/assign-security-groups/).
- [Доступ в интернет и из интернета](https://docs.selectel.ru/cloud-servers/cloud-networks/configure-access-to-internet/).
- [Описание реализации ScanBox](selectel-preemptible.md).

На 23 ноября 2026 года, по результатам пользователя, сборка обоих Docker-образов и preflight
в `ru-6` успешны. **Выбор доступной по SSH сети и правила группы
`default` ещё требуют подтверждения. Реальный smoke и Hunyuan inference
пока не подтверждены отчётом.**
