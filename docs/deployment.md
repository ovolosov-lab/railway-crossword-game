# Развёртывание новой версии «Железной дороги» на Ubuntu VPS

## Область применения

Это руководство описывает установку и обновление приложения на Ubuntu VPS. Предполагается, что на сервере установлены Docker Engine, Docker Compose v2, Git и Caddy на хосте; репозиторий проекта доступен с GitHub. Compose запускает два языковых экземпляра приложения, два Redis с отдельным bootstrap для каждого и одну общую PostgreSQL.

Запускаются `app-ru` и `app-en`, в каждом по одному Uvicorn worker. У них общая PostgreSQL и раздельные Redis: игровые поколения и активные сессии одного языка изолированы от другого. Caddy остаётся единственной публичной точкой входа и проксирует HTTPS на loopback-порты `8000` и `8080`.

При полном обновлении релиза необходимо остановить оба экземпляра приложения и создать по одному новому поколению в каждом Redis. Это завершит активные игровые сессии обоих языков и очистит их временное состояние. Учётные записи, подтверждённые результаты побед, постоянные очки, города, маршруты и словари хранятся в общей PostgreSQL и остаются после обновления. Резервная копия PostgreSQL не восстанавливает активные Redis-сессии.

Инструкции для прежней версии с общим раундом и in-memory состоянием сохранены отдельно: [архивное руководство MVP](./deployment_mvp.md). Не смешивайте его команды с этим порядком обновления.

## Установка приложения на VPS без сохранения прежних данных (и удалением их при необходимости) с загрузкой всех данных из файлов миграциями

Этот сценарий предназначен для первой установки или для осознанного полного сброса приложения. Он создаёт пустую PostgreSQL-базу, а затем заполняет её схемой, городами, маршрутами и словарями из файлов репозитория миграциями Alembic. Пользователи, очки, результаты и любые другие данные прежней базы при этом не переносятся.

**Удаление Docker volume PostgreSQL необратимо удаляет все данные проекта.** Если на VPS уже есть другие приложения или несколько Compose-проектов, сначала точно определите каталог, имя Compose-проекта и volume этого Railway. Не используйте глобальные команды `docker volume prune`, `docker system prune --volumes` или `docker compose down -v` из случайного каталога.

### 1. Получите репозиторий и задайте Compose project

Установите Docker Engine, Docker Compose v2, Git и Caddy на Ubuntu VPS. Настройте DNS-домен и firewall: публичными оставьте SSH, HTTP и HTTPS; порты PostgreSQL, Redis и upstream приложения не открывайте в Интернет.

Клонируйте репозиторий в постоянный каталог (подставьте URL своего репозитория) и используйте одно имя Compose-проекта во всех командах:

```bash
git clone https://github.com/<organization>/RAILWAY.git /opt/railway
cd /opt/railway
export COMPOSE_PROJECT_NAME=railway
git rev-parse --short HEAD
```

Если это не первая установка на данном VPS, остановите старые процессы приложения, включая экземпляры вне Compose. Проверьте активные Compose-проекты и volumes:

```bash
docker compose ls
docker compose ps -a
docker volume ls --filter label=com.docker.compose.project="$COMPOSE_PROJECT_NAME"
```

Если прежнее Railway использовало другое имя проекта, для удаления укажите именно его, а не создавайте новый volume рядом со старым. Если PostgreSQL внешний, не удаляйте Docker volume: очистка внешней базы выполняется отдельно по процедуре её провайдера.

### 2. Настройте `.env`

Создайте конфигурацию на основе примера, задайте production-параметры и замените все placeholder-значения:

```bash
cp .env.example .env
nano .env
chmod 600 .env
git check-ignore -v .env
openssl rand -hex 32
```

В `.env` укажите как минимум:

- `APP_ENV=production` и `DEBUG=false`;
- уникальные `DB_USER`, сильный `DB_PASSWORD` и `DB_NAME=railway`;
- случайный `JWT_SECRET` минимум 32 байта; значение из `openssl rand -hex 32` скопируйте в файл;
- `APP_RU_HOST_PORT=8000`, `APP_EN_HOST_PORT=8080` и `DB_HOST_PORT=5432` либо свободные loopback-порты, если эти заняты. Если меняете app-порты, скорректируйте Caddyfile.

Сервис приложения подключается к Compose PostgreSQL по `db:5432`, а PostgreSQL и приложение публикуются только на loopback. Не коммитьте `.env`; последняя команда должна подтвердить, что файл игнорируется Git. `COMPOSE_PROJECT_NAME` должен оставаться равным имени выбранного проекта во всех новых SSH-сессиях.

### 3. При необходимости удалите прежнюю базу

Пропустите этот шаг на чистом VPS, где нет прежнего Docker volume Railway. Если данные старого Railway нужно удалить, ещё раз сверьте `COMPOSE_PROJECT_NAME`, каталог проекта и список volumes. Команда ниже удалит контейнеры/сеть этого Compose-проекта и все его Compose volumes; в текущем Compose это, в частности, volume `postgres_data` со всей PostgreSQL-базой:

```bash
docker compose down --volumes
```

После выполнения будут удалены аккаунты, очки, результаты, города, маршруты, словари и история Alembic. Команду нельзя отменить; она не создаёт backup. Не выполняйте её, если нужно сохранить хотя бы часть прежних данных. Внешние или помеченные как `external` volumes этой командой не удаляются.

### 4. Соберите приложение, запустите чистую PostgreSQL и примените миграции

Проверьте Compose и соберите образы из выбранного commit:

```bash
docker compose config --quiet
docker compose build app-ru app-en game-bootstrap-ru game-bootstrap-en
```

Создайте пустую базу и дождитесь готовности PostgreSQL и Redis:

```bash
docker compose up -d db redis-ru redis-en
docker compose ps
docker compose exec -T db sh -c \
  'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose exec -T redis-ru redis-cli ping
docker compose exec -T redis-en redis-cli ping
```

Затем один раз примените всю цепочку миграций к этой базе. Не запускайте `alembic stamp head` и не импортируйте слова вручную до миграций: базовая миграция русских слов требует пустую `dictionary_words`.

```bash
docker compose run --rm --no-deps app-ru alembic upgrade head
docker compose run --rm --no-deps app-ru alembic current
docker compose run --rm --no-deps app-ru alembic heads
```

Цепочка Alembic загружает данные из versioned-файлов репозитория:

- JSON-каталогами миграций заполняются города на русском и английском и маршруты;
- русские слова и описания читаются из нумерованных CSV в `backend/migrations/words/`;
- отдельные некорректные строки русского CSV пропускаются с предупреждением в журнале Alembic; после импорта всё ещё проверяется, что словаря хватает для длин маршрутов;
- миграция `72fd40c93b31` импортирует английские файлы `1.csv`–`4.csv` из `words/en/`, а миграция `ab3d7f51c9e2` добавляет `5.csv`–`11.csv`;
- для повторяющихся пар `(language_code, word_text)` новые английские записи не вызывают ошибку и не перезаписывают существующее описание.

Итоговые данные обоих языков загружаются независимо от `LANGUAGE`. Каталоги, уже включённые в применённые миграции, считайте неизменяемыми: новые слова или изменения описаний оформляйте новой миграцией, а не редактированием файлов задним числом.

Проверьте head-ревизию и заполненность справочников:

```bash
docker compose exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT lang, count(*) FROM cities GROUP BY lang ORDER BY lang"'
docker compose exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) AS routes FROM routes"'
docker compose exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT language_code, count(*) FROM dictionary_words GROUP BY language_code ORDER BY language_code"'
```

Ожидаются строки для `ru` и `en` в `cities` и `dictionary_words`, а также ненулевое число маршрутов. Не привязывайте проверку к жёстким количествам: содержимое новых каталогов может изменяться новыми миграциями.

### 5. Создайте поколение игры, запустите приложение и подключите домен

Создайте поколение отдельно в каждом Redis и запустите оба языковых экземпляра:

```bash
docker compose up --force-recreate game-bootstrap-ru
docker compose up --force-recreate game-bootstrap-en
docker compose up -d --no-deps app-ru app-en
docker compose ps
docker compose logs --tail=100 app-ru app-en
```

Убедитесь, что оба приложения здоровы и в обоих журналах есть `Using game generation ...`. Направьте отдельные домены Caddy на `127.0.0.1:${APP_RU_HOST_PORT}` и `127.0.0.1:${APP_EN_HOST_PORT}`. Успешный `/health` подтверждает доступность PostgreSQL; состояние каждого Redis подтверждают startup соответствующего приложения и журнал поколения.

## 1. Устройство текущего развёртывания

- `app-ru` и `app-en` запускаются из одного [Dockerfile](../Dockerfile) на внутреннем порту `8000`, по одному Uvicorn worker в каждом. Compose публикует их только на `127.0.0.1:${APP_RU_HOST_PORT}` (по умолчанию `8000`) и `127.0.0.1:${APP_EN_HOST_PORT}` (по умолчанию `8080`). В контейнерах `LANGUAGE` зафиксирован соответственно как `ru` и `en`.
- `db` — PostgreSQL 16. Данные находятся в именованном Docker volume `postgres_data`; реальное имя включает имя Compose-проекта. При обновлении надо сохранить тот же Compose project и тот же volume. Для администрирования PostgreSQL Compose публикует `127.0.0.1:${DB_HOST_PORT}`; сервис приложения использует внутренний адрес `db:5432`.
- `redis-ru` и `redis-en` — два Redis 7 во внутренней Compose-сети. Порты наружу не опубликованы. Каждый Redis работает без RDB/AOF persistence и хранит только временные игровые сессии своего приложения.
- `game-bootstrap-ru` и `game-bootstrap-en` запускают `python -m app.game_generation` каждый против своего Redis. Для релиза запускайте каждый bootstrap **ровно один раз**, только после остановки обоих app workers. Никогда не используйте `FLUSHALL` или `FLUSHDB`.
- `/health` проверяет подключение приложения к PostgreSQL. Успешный `/health` сам по себе не проверяет Redis; готовность Redis и поколения подтверждают startup каждого app и запись `Using game generation ...` в его журнале.

Параметры подключения Compose задаёт отдельно для каждого контейнера: оба приложения используют `DB_HOST=db`, а `app-ru` — `redis://redis-ru:6379/0`, `app-en` — `redis://redis-en:6379/0`. Если PostgreSQL находится вне Compose, настройте подтверждённый сетевой адрес и резервирование отдельно; не оставляйте внешнюю БД доступной всему Интернету.

## 2. Подготовьте VPS и сохраните текущий Compose-проект

1. Запланируйте короткое техническое окно и предупредите игроков: активные сессии будут завершены при смене поколения.
2. Проверьте, что DNS-домен указывает на VPS, а Caddy обслуживает его по HTTPS. В firewall оставьте публичными SSH, HTTP и HTTPS. Порты приложения, PostgreSQL (`DB_HOST_PORT`) и Redis (`6379`) не открывайте в Интернет.
3. Перейдите в **существующий каталог проекта**, из которого запускалась предыдущая версия, и убедитесь, что это нужный GitHub-репозиторий:

   ```bash
   cd /opt/railway   # замените на фактический каталог проекта
   git remote -v
   git status --short
   git rev-parse --short HEAD
   docker compose ls
   docker compose ps -a
   ```

   Если в `git status` есть изменения, выясните, что это за файлы, и сохраните их отдельно. Не используйте `git reset --hard` и не удаляйте `.env`.

4. Узнайте имя существующего Compose-проекта и volume PostgreSQL до запуска новой конфигурации:

   ```bash
   docker ps --filter label=com.docker.compose.service=db \
     --format 'table {{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Ports}}'
   docker volume ls
   ```

   Все команды ниже выполняйте из того же каталога и с тем же Compose project. Если проектом раньше управляли с `-p`, экспортируйте то же значение для всех команд, например `export COMPOSE_PROJECT_NAME=railway` (замените `railway` на фактическое имя). Иначе Compose может создать новый пустой `postgres_data`, и приложение подключится не к прежней базе.

5. Сверьте upstream-порты Caddy с настройками `.env`: по умолчанию это `127.0.0.1:8000` для русского приложения и `127.0.0.1:8080` для английского. Если порт занят посторонним сервисом, задайте свободное значение в `APP_RU_HOST_PORT` или `APP_EN_HOST_PORT` и направьте соответствующий домен Caddy на него. Текущий старый контейнер может занимать `8000`; после его остановки проверьте, что новый набор сервисов использует выбранные порты.

## 3. Остановите запись и сделайте резервные копии

1. Остановите приложение, но оставьте PostgreSQL запущенным. Для старой конфигурации с одним сервисом выполните:

   ```bash
   docker compose stop app
   ```

   Для уже обновлённой конфигурации с двумя сервисами выполните:

   ```bash
   docker compose stop app-ru app-en
   ```

   Если процессы запущены вне Compose или есть дополнительные replicas/workers, остановите их штатным для сервера способом. До переключения поколений старые workers не должны принимать запросы или фиксировать победы.

2. Создайте закрытый каталог резервных копий и скопируйте production `.env` и текущий Caddyfile. Файл `.env` содержит секреты:

   ```bash
   backup_dir="$HOME/railway-backups"
   install -d -m 700 "$backup_dir"
   cp -a .env "$backup_dir/env-$(date -u +%Y%m%dT%H%M%SZ)"
   sudo cp -a /etc/caddy/Caddyfile "$backup_dir/Caddyfile-$(date -u +%Y%m%dT%H%M%SZ)"
   ```

3. Пока PostgreSQL ещё запущен, сделайте логическую резервную копию. Команда использует учётную запись и имя базы из окружения контейнера PostgreSQL, поэтому секреты не печатаются:

   ```bash
   backup_file="$backup_dir/postgres-$(date -u +%Y%m%dT%H%M%SZ).dump"
   docker compose exec -T db sh -c \
     'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_file"
   chmod 600 "$backup_file"
   test -s "$backup_file"
   docker compose exec -T db pg_restore --list - < "$backup_file" >/dev/null
   ```

   Если PostgreSQL внешний, сделайте backup способом, поддерживаемым провайдером, и проверьте, что копия доступна для восстановления. Не продолжайте обновление без проверенной резервной копии.

4. Не выполняйте `docker compose down -v`, не удаляйте Docker volumes и не меняйте `DB_NAME`, `DB_USER` или `DB_PASSWORD` в `.env` наугад. Для уже созданного тома значения `POSTGRES_*` из `.env` не меняют пароль существующей роли автоматически.

## 4. Получите релиз с GitHub и проверьте конфигурацию

Из того же каталога репозитория выберите используемую ветку (ниже пример для `main`) и обновитесь fast-forward-ом:

```bash
git fetch --prune origin
git switch main
git pull --ff-only origin main
git rev-parse --short HEAD
```

Если релизы публикуются тегами, вместо `main` переключитесь на согласованный release tag/commit. Не запускайте `git pull` с неизвестной веткой.

Сохраните существующий `.env`: не копируйте поверх него `.env.example`. Убедитесь, что в `.env`:

- `APP_ENV=production` и `DEBUG=false`; приложение выставляет флаг Secure для auth/CSRF cookies во всех средах, кроме `development`.
- `APP_RU_HOST_PORT=8000` и `APP_EN_HOST_PORT=8080` — loopback-порты для Caddy. Язык приложений задан в Compose как `ru` и `en`.
- `JWT_SECRET` — прежний секрет минимум 32 байта. Сохраните его, чтобы не инвалидировать уже выданные токены пользователей.
- `DB_USER`, `DB_PASSWORD`, `DB_NAME` соответствуют существующему PostgreSQL. Для контейнерной БД Compose сам передаёт приложению `DB_HOST=db`.
- `DB_HOST_PORT=5432` задаёт loopback-порт PostgreSQL для административного доступа с хоста. Если он занят, укажите свободный локальный порт; подключение приложения к БД всё равно идёт по `db:5432`.
- `PORT=8000` — внутренний порт обоих контейнеров; оставьте его без изменений. Redis URL задаются отдельно для каждого приложения в Compose.

Защитите файл и убедитесь, что Git его игнорирует:

```bash
chmod 600 .env
git check-ignore -v .env
```

Если последняя команда не подтверждает игнорирование `.env`, не добавляйте этот файл в Git и исправьте правила исключения до продолжения. Сгенерируйте новый `JWT_SECRET` только при первой установке, например `openssl rand -hex 32`; при обновлении уже работающей версии сохраняйте старый.

Проверьте Compose без вывода значений секретов и соберите новые образы:

```bash
docker compose config --quiet
docker compose build app-ru app-en game-bootstrap-ru game-bootstrap-en
```

Если Compose project или путь к volume отличаются от старого развёртывания, остановитесь и исправьте их до запуска приложения.

## 5. Запустите PostgreSQL/Redis и примените миграции

1. Запустите инфраструктурные сервисы, не запуская приложение:

   ```bash
   docker compose up -d db redis-ru redis-en
   docker compose ps
   docker compose exec -T db sh -c \
     'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
   docker compose exec -T redis-ru redis-cli ping
   docker compose exec -T redis-en redis-cli ping
   ```

   Ожидаемые ответы readiness: `accepting connections` и `PONG`.

2. Посмотрите текущую ревизию базы и доступную head-ревизию. Контейнер `app-ru` запускает Alembic из `/app/backend`:

   ```bash
   docker compose run --rm --no-deps app-ru alembic current
   docker compose run --rm --no-deps app-ru alembic heads
   ```

   Если история миграций отсутствует, текущая ревизия неизвестна, или исходная база не совпадает с поддерживаемой цепочкой, остановитесь. Не используйте `alembic stamp head`, не удаляйте таблицы и не очищайте словари для обхода ошибки; сначала выясните ревизию базы и подготовьте отдельный перенос. Baseline seed-ревизия `e8c42b7a19d3` требует пустую `dictionary_words`; если она ещё не применена, а в таблице уже есть записи, сначала нужен отдельный план переноса — не удаляйте эти записи.

3. Примените миграции ровно один раз на этот PostgreSQL:

   ```bash
   docker compose run --rm --no-deps app-ru alembic upgrade head
   docker compose run --rm --no-deps app-ru alembic current
   ```

   Миграции выполняются отдельной командой; старт приложения их автоматически не запускает. Ревизии создают/обновляют схему, справочники городов и маршрутов и загружают словари. Одно выполнение `upgrade head` загружает **оба** языка: русские записи с `language_code='ru'` и английские с `language_code='en'`; повторно запускать миграцию для другого `LANGUAGE` не нужно. Настройка `LANGUAGE` выбирает набор при работе приложения, но не ограничивает импорт словаря.

   Переход к локализованным городам требует русские и английские названия с общими ID. Ревизия `c13d7a2f9e41` обновляет seed-каталог и пересоздаёт строки `cities`/`routes`; внесённые вручную изменения названий, координат или маршрутов будут заменены каталожными данными. До миграций нужен backup. Они не предназначены для запуска поверх неизвестной/частично вручную заполненной схемы. Миграция англоязычного словаря сохраняет существующие русские слова. Если миграция остановилась на ошибке, не запускайте приложение и не исправляйте ситуацию ручным `DELETE`; сохраните ошибку, проверьте `alembic current` и восстановитесь по плану ниже.

4. Проверьте, что revision совпадает с `heads`, а таблицы имеют записи в обоих языках:

   ```bash
   docker compose exec -T db sh -c \
     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT lang, count(*) FROM cities GROUP BY lang ORDER BY lang"'
   docker compose exec -T db sh -c \
     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) AS routes FROM routes"'
   docker compose exec -T db sh -c \
     'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT language_code, count(*) FROM dictionary_words GROUP BY language_code ORDER BY language_code"'
   ```

   В `cities` должны быть строки для `ru` и `en`, в `dictionary_words` — строки для `ru` и `en`, а число маршрутов и оба словаря должны быть больше нуля. Не используйте жёстко заданные количества: CSV-каталоги меняются между релизами.

## 6. Создайте новое поколение и запустите приложение

Убедитесь ещё раз, что оба старых worker остановлены. После этого создайте новое поколение в каждом Redis по одному разу:

```bash
docker compose up --force-recreate game-bootstrap-ru
docker compose up --force-recreate game-bootstrap-en
```

Не запускайте bootstrap параллельно с работающими приложениями и не повторяйте его «для проверки»: каждый запуск публикует новое поколение в соответствующем Redis и лишает старые workers права на игровые операции. Старое временное состояние после переключения не восстанавливается. Очистка затрагивает только ключи предыдущего поколения `railway:{game}` в каждом Redis; если журнал сообщает о пределе очистки в 1000 ключей, не очищайте Redis вручную.

Запустите оба экземпляра приложения, не выполняя dependencies второй раз:

```bash
docker compose up -d --no-deps app-ru app-en
docker compose ps
docker compose logs --tail=100 app-ru app-en
```

Убедитесь, что оба контейнера здоровы и в обоих журналах есть `Using game generation ...`. Если обнаружена недоступность Redis или отсутствие поколения, оставьте Caddy без переключения и проверьте соответствующие журналы `redis-*`, `game-bootstrap-*` и `app-*`. Не запускайте старый и новый код одновременно после смены поколения.

## 7. Подключите Caddy и проверьте сайт

Caddy работает на хосте; в существующем Caddyfile добавьте или обновите два блока для языковых доменов, сохранив остальные сайты. Например:

```caddyfile
railway.example.org {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}

railway-en.example.org {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8080
}
```

Подставьте свои домены. Если в `.env` выбраны другие `APP_RU_HOST_PORT` или `APP_EN_HOST_PORT`, укажите их в соответствующих upstream. Проверьте конфигурацию и примените её:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Проверьте локальный upstream и публичный HTTPS-домен:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8080/health
curl -fsS https://railway.example.org/health
curl -fsS https://railway-en.example.org/health
```

Ожидаемый ответ — `{"status":"ok"}`. `/health` проверяет PostgreSQL, а доступность Redis подтверждается успешным startup приложения и логом поколения. Затем откройте `/login/`, войдите тестовым пользователем, проверьте игровую страницу, карту, создание индивидуальной сессии и загрузку статических ресурсов. Убедитесь, что адрес в браузере остаётся HTTPS.

Проверьте, что Docker опубликовал приложение только на loopback и не изменил публичные порты:

```bash
docker compose port app-ru 8000
docker compose port app-en 8000
sudo ss -ltnp
```

Для `app-ru` и `app-en` должны отображаться соответственно `127.0.0.1:<APP_RU_HOST_PORT>` и `127.0.0.1:<APP_EN_HOST_PORT>`. PostgreSQL также опубликован только на loopback (`127.0.0.1:<DB_HOST_PORT>`), Redis host ports не публикуются. Не настраивайте Caddy на публичный Docker bridge IP.

## 8. Рестарты, обновления и пределы восстановления

- Перезапуск приложений без выпуска новой версии: `docker compose restart app-ru app-en`. Эта команда **не должна** запускать bootstrap или менять поколения; Redis-сессии при обычном рестарте остаются доступны.
- Полный релиз с новыми миграциями/кодом: повторите обслуживание, backup, остановку обоих workers, сборку, однократный `alembic upgrade head` через `app-ru`, по одному bootstrap для каждого Redis и запуск обоих приложений. Миграции выполняются до запуска приложений, которым нужна новая схема.
- Если один Redis перезапустился без сохранённых данных или был очищен, временные сессии соответствующего языка могли исчезнуть. Остановите связанное приложение, проверьте PostgreSQL и этот Redis, выполните его bootstrap и запустите приложение снова. Не трактуйте потерю Redis как пустую игровую сессию и не восстанавливайте Redis ключи командой `FLUSHALL`.
- Не запускайте Alembic в entrypoint или отдельно из каждого worker. Каждый языковой экземпляр остаётся с одним worker; не масштабируйте их без отдельного приёмочного прогона.

### Восстановление PostgreSQL

Сначала проверьте backup в отдельной временной базе, а не поверх production. Для Compose PostgreSQL:

```bash
docker compose exec -T db sh -c \
  'createdb -U "$POSTGRES_USER" railway_restore_check'
docker compose exec -T db sh -c \
  'pg_restore --exit-on-error -U "$POSTGRES_USER" -d railway_restore_check -' < "$backup_file"
docker compose exec -T db sh -c \
  'psql -U "$POSTGRES_USER" -d railway_restore_check -c "SELECT version_num FROM alembic_version"'
```

После проверки удалите только временную восстановленную базу:

```bash
docker compose exec -T db sh -c \
  'dropdb -U "$POSTGRES_USER" railway_restore_check'
```

Если production нужно восстановить, остановите приложение и следуйте отдельной проверенной процедуре для восстановления из backup/снимка в согласованный PostgreSQL. Не выполняйте `pg_restore --clean` в рабочую базу без согласованного плана. PostgreSQL восстановит постоянные данные и подтверждённые результаты; он не возвращает активные Redis-сессии.

### Откат к предыдущему коду

Откат контейнера сам по себе не откатывает схему. Не запускайте старый код против новой схемы, пока совместимость не проверена. Если новая версия не стартовала до bootstrap, оставьте старый worker остановленным, изучите миграцию и восстановите PostgreSQL из проверенной копии/снимка по отдельному плану. Если bootstrap уже создал новое поколение, предыдущие активные сессии уже недоступны; rollback не возвращает их. Не используйте `alembic downgrade` как автоматический способ отката: seed-миграции слов удаляют добавленные ими строки, а некоторые изменения справочников и схемы требуют точной проверки.

## 9. Диагностика

| Симптом | Действие |
|---|---|
| Приложение подключилось к пустой БД | Проверьте Compose project name, volume PostgreSQL и `docker compose ps -a`; не удаляйте новый volume, пока не установлено, где прежние данные. |
| Миграция сообщает о неизвестной ревизии или непустом словаре на baseline seed | Остановитесь; не удаляйте строки и не делайте `stamp head`. Сверьте ревизию с backup и планом миграции. |
| Нет английских слов | Выполните запрос группировки `dictionary_words` по `language_code`; проверьте, что запрос направлен в тот же DB_HOST/DB_NAME и что `alembic current` соответствует `heads`. |
| `app-ru` или `app-en` сообщает `No active game generation` или Redis unavailable | Проверьте соответствующий `redis-ru`/`redis-en`, остановите связанное приложение, успешно выполните `game-bootstrap-ru`/`game-bootstrap-en` и только затем запустите его снова. |
| Caddy отвечает 502 | Проверьте `docker compose ps`, здоровье `app-ru` и `app-en`, `APP_RU_HOST_PORT`/`APP_EN_HOST_PORT`, `docker compose port app-ru 8000` и `docker compose port app-en 8000`, локальные `/health` и журналы приложений. |
| `/health` возвращает 503 | Проверьте логи `app-ru`/`app-en` и PostgreSQL, DB credentials, сетевое имя `db` и readiness PostgreSQL. Успешный `/health` не заменяет проверку старта соответствующего Redis generation. |
| После деплоя пользователь вышел из активной игры | Это ожидаемая граница полного переключения поколения. Убедитесь, что сохранены постоянные аккаунты и результаты в PostgreSQL; активные сессии не восстанавливаются из SQL backup. |

При проблеме держите старые workers остановленными, сохраняйте логи и backup, не очищайте volumes/Redis и не повторяйте bootstrap без понимания текущего поколения.
