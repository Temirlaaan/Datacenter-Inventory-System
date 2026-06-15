# ТЗ: elevation стойки — `GET /api/v1/racks/{rack_id}/elevation`

**Для:** бэкенд qr-dc (FastAPI-прослойка над NetBox)
**Инициатор:** мобильный клиент DC Inventory (фаза 2 визуализации стоек)
**Приоритет:** средний-высокий (фаза 1 уже в проде у мобилки и имеет известные искажения)

## Контекст

Мобильный клиент уже рисует стойку (фаза 1), собирая картинку из
`GET /api/v1/devices/search?rack=N`. У этого способа три дефекта, которые
лечатся только серверным эндпоинтом:

1. **Нет стороны стойки (face).** Устройства front и rear на одних юнитах
   накладываются — рисуется «кто первый попался».
2. **`u_height` устройства часто null.** Пример с прода: device id=238
   (PowerEdge R640, физически 1U) приходит с `"u_height": null` — похоже,
   сериализатор не разыменовывает `device_type.u_height`. Мобилка рисует
   такие как 1U наугад. *Это стоит починить и в `DeviceData` независимо
   от данного ТЗ.*
3. **Нет резерваций юнитов** (NetBox rack reservations) — зарезервированные
   юниты выглядят свободными.

## Контракт

```http
GET /api/v1/racks/{rack_id}/elevation
```

Ответ `200`:

```jsonc
{
  "rack": { "id": 4, "name": "Server-Rack-1.12", "site_id": 1, "u_height": 42 },
  "devices": [
    {
      "id": 238,
      "name": "dc01-ast-comp-gen-srv51.t-cloud.kz",
      "status": { "value": "active", "label": "Active" },
      "role_name": "Server",
      "device_type_model": "PowerEdge R640",
      "position": 34,            // нижний юнит; null не отдаём — такие
                                  // устройства в массив не включаем,
                                  // см. unpositioned_count
      "u_height": 1,              // ИЗ device_type, всегда >= 1, не null
      "face": "front"            // "front" | "rear"
    }
  ],
  "reservations": [
    { "units": [10, 11, 12], "description": "Под новый SAN, заявка №123" }
  ],
  "unpositioned_count": 2,        // привязаны к стойке без позиции
  "occupied_units": 28
}
```

## Семантика и источники

- NetBox отдаёт elevation из коробки
  (`/api/dcim/racks/{id}/elevation/?face=front|rear`) — можно агрегировать его
  либо собрать из devices + device_type.u_height; на усмотрение исполнителя,
  важен только контракт выше.
- Резервации: `/api/dcim/rack-reservations/?rack_id=…`.
- Кэш 5 минут, как у остальных meta-эндпоинтов (Architecture §кэширование).
- Доступ: любой аутентифицированный пользователь, read-only, смена не нужна.
- `404 RACK_NOT_FOUND` для несуществующей стойки.

## Мобильный клиент (после готовности)

Фаза 2 на мобилке: переключатель Front/Rear над elevation, серые
зарезервированные юниты с подписью, честные высоты блоков, счётчик
заполненности в списке стоек (если добавите лёгкий батч-эндпоинт или
поле occupied в `/meta/racks` — нарисуем прогресс-бары и там).
