# Tilda → Saby Presto Bridge

Webhook-сервис для передачи оплаченных заказов из Tilda в Saby Presto.

## Что делает сервис

- принимает webhook от Tilda;
- проверяет secret-ключ;
- не создаёт дубли по payment.orderid;
- сопоставляет товары Tilda с номенклатурой Saby;
- создаёт заказ в Saby Presto;
- хранит историю обработки в SQLite;
- работает через Docker Compose + Caddy + HTTPS.

## Рабочий URL webhook

https://62-109-26-144.sslip.io/webhooks/tilda?secret=<WEBHOOK_SECRET>

## Проверка сервиса

curl -i https://62-109-26-144.sslip.io/health

Ожидаемый ответ:

{"status":"ok","dryRun":false,"registerPaymentEnabled":false,"pointId":277,"priceListId":6}

## Запуск

docker compose up -d --build

## Проверка контейнеров

docker compose ps

## Логи

docker compose logs --tail=100 app
docker compose logs --tail=100 caddy

## Проверка заказов

sqlite3 data/orders.db "SELECT id,tilda_order_id,status,saby_order_number,error,created_at,updated_at FROM orders ORDER BY id DESC LIMIT 10;"

## Важно

Файл .env не хранится в репозитории.

База data/orders.db не хранится в репозитории.

Перед запуском нужно создать .env на основе .env.example.

## Текущий статус

- VPS: Ubuntu 24.04
- Reverse proxy: Caddy
- Runtime: Docker Compose
- HTTPS: 62-109-26-144.sslip.io
- DRY_RUN=false
- REGISTER_PAYMENT_ENABLED=false
