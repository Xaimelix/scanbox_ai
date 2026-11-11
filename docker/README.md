# Docker для локального runner и GPU VM Selectel

Есть два независимых Compose-файла:

| Файл | Где запускать | Что делает |
| --- | --- | --- |
| `compose.yaml` | Ваш WSL/Docker Desktop или управляющий backend | Команды Selectel/OpenStack. По умолчанию только `--preflight` |
| `compose.gpu.yaml` | GPU VM или машина с настроенным NVIDIA runtime | Проверка CUDA, подготовка весов, задача Hunyuan shape → GLB |

Compose управляет контейнерами на одном Docker host. Создание VM выполняет
наш Python-клиент Nova, а не `docker compose --scale`.

## Команды на вашем компьютере

Docker Desktop должен предоставлять Docker/Compose внутри вашей WSL Ubuntu.
Из корня репозитория, после подготовки `.env` и загрузки RC:

```bash
source /path/to/rc.sh
docker compose build selectel
docker compose run --rm selectel --check-env
docker compose run --rm selectel --preflight
docker compose run --rm selectel --plan
```

Compose автоматически читает `.env` в корне проекта для подстановки параметров.
`OS_*`, экспортированные RC-файлом в текущей shell, передаются в контейнер.
`SELECTEL_AUTH_SOURCE=openstack` можно задать в `.env`. RC-файл не исполняется
внутри контейнера и не копируется в образ. GPU локально не требуется.
Пароли передаются только при запуске, не через build args. Не публикуйте
отрендеренный `docker compose config`: он содержит подставленные переменные.

Образ runner также содержит OpenStack CLI. Например, read-only команды:

```bash
docker compose run --rm --entrypoint openstack selectel server list
docker compose run --rm --entrypoint openstack selectel image list
docker compose run --rm --entrypoint openstack selectel network list
```

Для создания тестовой платной VM после выбора всех параметров:

```bash
docker compose run --rm selectel --smoke --report /reports/smoke-1.json
docker compose run --rm --entrypoint cat selectel /reports/smoke-1.json
```

Отчёты сохраняются в именованном volume `reports` и переживают `run --rm`.
Для нескольких независимых тестов используйте разные имена отчётов, например
`smoke-1.json` и `smoke-2.json`. Каждый процесс создаёт собственную VM/keypair
и выполняет очистку. Число параллельных запусков ограничивайте своими квотами
и бюджетом. Текущий `api/worker.py` обрабатывает очередь последовательно;
нового общего планировщика параллельных GPU-задач этот Compose не добавляет.

## Одна SSH security group для нескольких VM

Security group — сетевые правила, не SSH-ключ и не группа размещения серверов.
В рамках подходящего проекта/региона одну группу можно назначить нескольким VM.
Для нашего варианта достаточно входящего TCP 22 от публичного IP runner `/32`
или от вашей VPN-подсети. При подключении из Docker это внешний адрес выхода
хоста/VPN, а не внутренний адрес контейнера.
Описание Selectel: [группы безопасности](https://docs.selectel.ru/cloud-servers/security-groups/about/).

Пример **создаёт сетевую группу и правило**, выполняется один раз с ролью,
разрешающей изменения (read-only пользователь для этого не подходит):

```bash
export RUNNER_CIDR='YOUR_PUBLIC_IP/32'
docker compose run --rm --entrypoint openstack selectel \
  security group create scanbox-runner-ssh
docker compose run --rm --entrypoint openstack selectel \
  security group rule create --ingress --ethertype IPv4 --protocol tcp \
  --dst-port 22 --remote-ip "$RUNNER_CIDR" scanbox-runner-ssh
```

В `.env` укажите `SELECTEL_SECURITY_GROUP=scanbox-runner-ssh`. Не создавайте
её заново для каждой VM. Правила группы применяются ко всем подключённым
серверам, поэтому изменение разрешённых адресов влияет на них одновременно.
Нужны также рабочий маршрут к VM, port security сети и разрешённый исходящий
доступ, необходимый выбранному образу. Публичный IP эта команда не выделяет.

Ключи у задач отдельные: runner генерирует временную keypair при создании VM,
хранит приватную часть только в памяти, после теста удаляет keypair в Selectel.
Приватный ключ сервисного пользователя/OpenStack-пароль на GPU VM не передаётся.
SSH внутри GPU-контейнера не нужен: runner входит на VM и запускает Docker там.

## GPU worker: состав и сборка

`docker/Dockerfile.gpu` содержит PyTorch 2.5.1 с CUDA 12.4, shape-зависимости
и код Hunyuan3D-2.1, зафиксированный на commit
`82920d643c0dc2f7bfd7255f45f62d386edfe60c`.
При сборке проверяется импорт Python pipeline. Это **shape-only**, без
PBR/texture, Gradio и HTTP API. Для L4/24 GB полный Hunyuan PBR отдельно
не обещается. Модель обрабатывает заранее подготовленное фото, предпочтительно RGBA
с удалённым фоном; сегментация не входит в этот worker.

Образ можно собрать заранее на CPU-машине; для выполнения `doctor`/`infer`
нужен NVIDIA GPU. Сборка тяжёлая, включает CUDA/PyTorch; не делайте её заново
при каждом создании эфемерного сервера.

```bash
export SCANBOX_GPU_IMAGE='YOUR_REGISTRY/scanbox-gpu:0.1'
docker compose -f compose.gpu.yaml build worker
docker compose -f compose.gpu.yaml push worker
```

На целевой VM обязательны **NVIDIA driver, Docker Engine, NVIDIA Container Toolkit
и Compose v2**. Пользователь `SELECTEL_SSH_USER` должен иметь право запускать
Docker. В Compose запрашивается один GPU; [формат Docker](https://docs.docker.com/compose/how-tos/gpu-support/).
Драйвер ядра устанавливается на хосте и не поставляется Docker-образом.

Подготовьте каталоги на GPU VM (или при сборке golden image):

```bash
mkdir -p models work
# Container worker работает с UID 65532; отдельный рабочий каталог должен быть доступен ему.
chmod 0777 work
export SCANBOX_GPU_IMAGE='YOUR_REGISTRY/scanbox-gpu:0.1'
docker compose -f compose.gpu.yaml pull worker
export SCANBOX_LOCAL_UID="$(id -u)"
export SCANBOX_LOCAL_GID="$(id -g)"
docker compose -f compose.gpu.yaml run --rm models --revision main
docker compose -f compose.gpu.yaml run --rm worker doctor
```

`prepare-models` разрешает `main` в точный Hugging Face commit SHA,
загружает shape-подкаталог и записывает `models/Hunyuan3D-2.1/scanbox-revision.json`.
Для повторяемых развёртываний передавайте уже записанный SHA вместо `main`.
Веса скачиваются отдельно, не требуют GPU и не входят в образ. Затем
`infer` работает offline. Храните каталог моделей на сетевом диске либо
в golden image, иначе локальный диск прерываемой VM может его потерять.

Для первой генерации (положите `photo.png` в `work`):

```bash
docker compose -f compose.gpu.yaml run --rm worker infer \
  --input /work/photo.png --output /work/model.glb
```

`doctor` выполняет реальное матричное вычисление CUDA, не только `nvidia-smi`.
`infer` явно завершится ошибкой, если нет CUDA/весов или закончилась память;
автоматической подмены на mock нет. Явный `--engine mock` предусмотрен только
для проверки формата/дискового ввода-вывода и даёт пустую сцену.
VM может быть прервана во время обработки; checkpoints/resume не добавлены.

## Связь worker с существующим оркестратором

Для эфемерных VM сначала подготовьте собственный OS/golden image с Docker,
NVIDIA runtime, заранее загруженным worker-образом и каталогом весов.
Это **другой образ**: его ID задаётся в `SELECTEL_IMAGE`, а Docker reference —
в `SELECTEL_WORKER_IMAGE`. Текущий cloud-init не устанавливает этот стек.

После подготовки `.env` для выбранного вами L4:

```dotenv
SELECTEL_AUTH_SOURCE=openstack
SELECTEL_FLAVOR=13960
SELECTEL_AVAILABILITY_ZONE=ru-6a
SELECTEL_SECURITY_GROUP=scanbox-runner-ssh
# Также задайте SELECTEL_IMAGE, SELECTEL_NETWORK_ID, SELECTEL_SSH_USER,
# SELECTEL_BOOT_VOLUME_GB по выбранному загрузочному диску.
SELECTEL_WORKER_IMAGE=YOUR_REGISTRY/scanbox-gpu@sha256:YOUR_DIGEST
SELECTEL_MODEL_DIR=/opt/scanbox/models
```

В `/opt/scanbox/models` ожидается подкаталог `Hunyuan3D-2.1` с весами.
Образ с **тем же digest** должен уже находиться в Docker на golden image.
Скрипт использует `--pull never`, чтобы тест не скачивал многогигабайтный
образ во время аренды и не требовал registry credentials на каждой VM.

При `SELECTEL_WORKER_IMAGE` smoke-тест запустит `doctor` внутри GPU-контейнера.
Standalone-оркестратор использует тот же образ для реальной генерации:

```bash
docker compose run --rm \
  --volume "$PWD/captures:/input:ro" --entrypoint python selectel \
  -m scripts.orchestrator --preemptible --photo /input/photo.png --out /reports/model.glb
```

Он передаёт фото по SSH, запускает один Docker job без сети, получает GLB
и удаляет VM. Модели монтируются только для чтения; OpenStack credentials
в GPU-контейнер не передаются. При пустом `SELECTEL_WORKER_IMAGE` остаётся
прежний режим `scripts/generate.py` с его placeholder ML entrypoint.

## Проверка и границы готовности

Compose проверяется через `docker compose -f FILE config --quiet`, без вывода
подставленных секретов. Тесты Python:

```bash
python -m unittest discover -s scripts/tests -v
```

Сборка runner и `--check-env` не создают облачные ресурсы. Сборка GPU-образа
сама по себе не подтверждает реальный inference: нужен запуск `doctor` и
`infer` на вашей L4. Живые Selectel VM и security group здесь не создавались.

Runner был собран и проверен через Compose (`--check-env`) и OpenStack CLI
(`openstack --version`). Python-тесты проверяют также передачу путей в Docker,
отсутствие cloud credentials в удалённой команде, offline-параметры, запись GLB
в явном mock-режиме, отказ при отсутствии CUDA и фиксацию revision весов.
Полная сборка CUDA-образа и реальный Hunyuan inference в этой среде не выполнены.
