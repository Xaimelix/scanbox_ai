# Сеть для теста VM Selectel: приватная подсеть + временный floating IP

В выбранном проекте/регионе API вернул пустые списки собственных сетей,
роутеров и floating IP. Видимая `external-network` принадлежит провайдеру.
Для этого сценария подготовим приватную сеть и роутер один раз, а публичный
floating IP будет создаваться и удаляться самим smoke-тестом.

**Команды подготовки ниже изменяют ресурсы.** При подключении внешнего
шлюза роутеру выделяется публичный IP; он оплачивается отдельно и остаётся
после удаления тестовой VM. Сам smoke также выделит платный floating IP
на время проверки. Роль только для чтения для этих операций не подходит.

Сначала обновите и пересоберите runner:

```bash
git pull --ff-only
source /путь/к/rc.sh
docker compose build selectel

osc() {
  docker compose run --rm --entrypoint openstack selectel "$@"
}
```

Убедитесь, что RC относится к нужному проекту и `OS_REGION_NAME=ru-6`.
Используйте отдельные имена ресурсов теста. Если команда уже была выполнена
или завершилась timeout, сначала проверьте наличие ресурса через `show/list`,
не повторяйте создание вслепую. Команды ниже не изменяют `external-network`
и не удаляют чужие ресурсы.

## 1. Создать приватную сеть и подсеть

```bash
osc network create --enable-port-security scanbox-test-net

osc subnet create scanbox-test-subnet \
  --network scanbox-test-net \
  --subnet-range 192.168.50.0/24 \
  --dhcp --dns-nameserver 1.1.1.1
```

CIDR здесь пример; при необходимости выберите незанятый диапазон.

## 2. Подключить подсеть к внешней сети через роутер

```bash
osc router create --availability-zone-hint ru-6a scanbox-test-router

osc router set \
  --external-gateway fad06e74-fb6e-4188-8a7a-dfb9f245c97f \
  scanbox-test-router

osc router add subnet scanbox-test-router scanbox-test-subnet
```

После `router set --external-gateway` появляется оплачиваемый внешний адрес
роутера. Это отдельный адрес, не тот floating IP, по которому будет доступна VM.

**Не выполняйте `floating ip create` вручную для этого smoke:** скрипт сам
выделит адрес после создания VM и привяжет его к её IPv4-порту.

## 3. Записать ID приватной сети в .env

Получите ID:

```bash
osc network show scanbox-test-net -f value -c id
```

Настройки:

```dotenv
SELECTEL_AUTH_SOURCE=openstack
SELECTEL_NETWORK_ID=UUID_НОВОЙ_SCANBOX_TEST_NET
SELECTEL_FLOATING_NETWORK_ID=fad06e74-fb6e-4188-8a7a-dfb9f245c97f
SELECTEL_SECURITY_GROUP=5f0ff8f3-3d85-491f-a196-96acf92347ed
SELECTEL_FLAVOR=13960
SELECTEL_AVAILABILITY_ZONE=ru-6a
SELECTEL_IMAGE=18ae9eeb-bd22-47f4-b03b-0eba5da50729
SELECTEL_WORKER_IMAGE=
```

Образ, SSH-пользователь и тип/размер boot volume должны быть выбраны по
[основному гайду](selectel-test-guide.md). Здесь показаны ID из текущего
окружения пользователя; для другого проекта получите свои значения.

Группа `default` из предоставленного вывода разрешает весь входящий трафик
IPv4/IPv6, включая SSH. Дополнительное правило TCP 22 для неё не требуется.
Для постоянного использования создайте ограниченную группу либо сузьте
правила с учётом остальных VM, которые используют `default`.
Скрипт автоматически правила группы не меняет.

Экспортируйте актуальные настройки в той же WSL-сессии:

```bash
set -a
source .env
source /путь/к/rc.sh
set +a
export SELECTEL_AUTH_SOURCE=openstack
```

## 4. Read-only проверка схемы

```bash
docker compose run --rm selectel --preflight
docker compose run --rm selectel --plan
```

При заданном `SELECTEL_FLOATING_NETWORK_ID` preflight дополнительно читает
Neutron и проверяет:

- приватная и внешняя сети различаются;
- port security приватной сети включён;
- внешняя сеть имеет `router:external=true`;
- есть административно включённый роутер с интерфейсом в приватной сети
  и шлюзом в выбранной внешней сети.

В JSON появится секция `network` с найденными ID и `resources_created=false`.
Это проверка конфигурации, а не гарантия работы dataplane или свободной квоты IP.
В план добавляется секция `floating_ip`, но никаких ресурсов план не создаёт.

## 5. Smoke одной платной VM

```bash
REPORT="smoke-$(date +%Y%m%d-%H%M%S).json"
docker compose run --rm selectel --smoke --report "/reports/$REPORT"
docker compose run --rm --entrypoint cat selectel "/reports/$REPORT"
```

Скрипт перед созданием VM повторно проверит топологию. Затем создаст VM,
найдёт её единственный IPv4-порт в выбранной приватной сети, выделит и
привяжет floating IP и подключится по SSH **к публичному адресу**.

При очистке он удаляет только свой выделенный floating IP, подтверждает
его отсутствие, затем выполняет обычную очистку VM/boot volumes/keypair.
Ошибка удаления IP не отменяет попытку удалить VM и ключ.

Для успешного теста с floating IP дополнительно ожидаются:

```json
{
  "floating_ip_deleted": true,
  "floating_ip_creation_uncertain": false,
  "cleanup_errors": [],
  "status": "passed"
}
```

В отчёте будут ID/адрес выделенного IP. Если ответ на создание IP оборвался,
`floating_ip_creation_uncertain=true`: автоматического повтора нет.
Проверьте floating IP в проекте. У созданного скриптом адреса поле
`description` равно уникальному имени `name` из отчёта.

```bash
osc floating ip list --long -f json
# Для подозреваемого адреса — сверить проект, description и port_id:
osc floating ip show ID_АДРЕСА -f json
```

После подтверждения, что это именно оставшийся IP теста, его можно удалить:

```bash
osc floating ip delete ID_АДРЕСА
```

Статус не считается успешным при ошибке очистки или неопределённом результате
создания VM/IP. Остальные случаи разобраны в основном гайде.

## 6. Что остаётся после успешного smoke

Приватная сеть, подсеть и общий роутер **сохраняются** для следующих запусков.
Внешний IP роутера продолжает оплачиваться. Скрипт не удаляет общие ресурсы
после каждой VM и не управляет уже существующими floating IP.

Когда тестовая инфраструктура больше не нужна, сначала убедитесь, что
в ней нет других VM, портов и floating IP. Только для созданных выше
ресурсов, которые больше нигде не используются:

```bash
osc router remove subnet scanbox-test-router scanbox-test-subnet
osc router unset --external-gateway scanbox-test-router
osc router delete scanbox-test-router
osc subnet delete scanbox-test-subnet
osc network delete scanbox-test-net
```

Проверьте списки ресурсов после удаления; сохраните отчёты.
После полной очистки уберите старые ID сетей из `.env` либо подготовьте
сеть заново перед следующим запуском.

## Генерация после smoke

Тот же режим floating IP поддерживает `scripts/orchestrator.py`:
он использует публичный адрес для SSH и удаляет выделенный IP вместе с VM.
При `--keep-on-error` сохраняемые для диагностики ресурсы, включая IP,
нужно очищать вручную. Подготовка Docker worker/весов остаётся отдельным этапом.

Справка: [сетевой доступ Selectel](https://docs.selectel.ru/cloud-servers/cloud-networks/configure-access-to-internet/).
