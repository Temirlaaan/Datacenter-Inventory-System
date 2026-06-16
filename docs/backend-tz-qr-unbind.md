# ТЗ: отвязка QR-метки — `POST /api/v1/qr/{qr_id}/unbind`

**Для:** бэкенд qr-dc (FastAPI-прослойка над NetBox)
**Инициатор:** мобильный клиент DC Inventory
**Приоритет:** средний (всплыл в полевом тесте)
**Статус:** РЕАЛИЗОВАНО 2026-06-16.

## Бизнес-кейс

Инженер сканирует QR, метка уже привязана к устройству, но привязка неверна
(тест, ошибка, устройство убрали). Нужно **снять привязку и вернуть метку в
статус FREE**, чтобы переиспользовать её. Сейчас из BOUND есть только:
- `rebind` — сразу на другое устройство (но не всегда известно, на какое),
- `retire` — гасит метку насовсем (переиспользовать нельзя).

Отдельной операции «просто отвязать → FREE» нет.

## Контракт

```http
POST /api/v1/qr/{qr_id}/unbind
Idempotency-Key: <uuid>            ← опционально, мобильный клиент шлёт всегда

{
  "version": "<last_updated текущего устройства>",   // для OCC при очистке qr_id
  "reason": "Метка снята: устройство демонтировано"  // ОБЯЗАТЕЛЬНОЕ, 1..2000
}
```

Ответ `200` — состояние метки после операции (как у `lookup`, но без device):

```jsonc
{ "qr": { "id": "...", "status": "free", ... } }
```

## Семантика

1. QR должен быть **BOUND** (FREE → 409 `QR_NOT_BOUND`; RETIRED — терминал).
2. У текущего устройства очищается `custom_fields.qr_id` (NetBox PATCH с
   optimistic concurrency по `version`) + journal-запись «QR {id} отвязан: {reason}».
3. Регистр QR: статус → FREE, `bound_to_device_id` → null, `bound_at` → null.
4. **Аудит обязателен**: action `qr.unbind` (qr_id, бывший device_id, reason,
   пользователь, смена).
5. Требуется активная смена (`409 NO_ACTIVE_SHIFT`).
6. Idempotency-Key как у bind/rebind.

## Права

Реализовано на роли `dcinv-mobile-user` (роли `dcinv-engineer` в системе нет —
консистентно с rebind). Контроль — обязательный `reason` + полный аудит.

## Ошибки

| Код | HTTP | Когда |
|---|---|---|
| `QR_NOT_FOUND` | 404 | нет такого qr_id |
| `QR_NOT_BOUND` | 409 | QR в статусе FREE или RETIRED |
| `DEVICE_CONFLICT` | 409 | устройство изменилось; клиент перечитает (ТЗ просило `VERSION_CONFLICT`, но бэкенд отдаёт `DEVICE_CONFLICT` для консистентности с bind/rebind/retire — тело то же: `current_state` + `current_version`) |
| `NO_ACTIVE_SHIFT` | 409 | смена не открыта |
| `QR_UNBIND_ROLLED_BACK` | 500 | сбой в середине саги, откатилось чисто — QR всё ещё привязан, можно повторить |
| `QR_UNBIND_INCONSISTENCY` | 500 | редкое: откат не удался, NetBox мог разойтись с реестром — к админу |
| (валидация) | 422 | reason пустой / > 2000 |

## Связанный момент (device NAME вместо id)

Бэкенд уже возвращает **полный объект устройства** (с `name`) везде, где
показывается привязка — мобилке нужно отображать `device.data.name`, а не id:
- `GET /qr/{id}` (lookup) на BOUND QR → `device.data.name`;
- `POST /qr/{id}/bind` и `POST /qr/{id}/rebind` (успех) → `device.data.name`.

В телах ошибок `DEVICE_ALREADY_BOUND` / `QR_ALREADY_BOUND` фигурирует числовой
`device_id` (машиночитаемо). Чтобы показать имя — `GET /devices/{device_id}` →
`data.name`. Имя там специально НЕ резолвится на бэкенде: NetBox под нагрузкой
узкое место, лишний запрос в error-path нежелателен.

## Реализация (для справки)

Сага: очистить qr_id на устройстве (OCC по version) → транзакция реестра
BOUND→FREE + одна `qr.unbind` audit-строка (atomic). При сбое БД после PATCH —
компенсация восстанавливает qr_id на устройстве (`QR_UNBIND_ROLLED_BACK`), при
провале компенсации — `QR_UNBIND_INCONSISTENCY` + danger-журнал. Структурно —
упрощённый `rebind` (одно устройство вместо двух).
