# Архитектура

Однофайловый CLI `cardgen.py`, семь команд-этапов. Каждый этап читает JSONL-журналы
предыдущего и дописывает свой — поэтому любой этап можно обрывать, перезапускать,
батчить `--limit` без переплаты (resume-safe по ключам `product_id` / `(product_id, out_file)`).

```
products.csv + photos/          profile.json (дизайн-система магазина)
        │                                   │
        ▼                                   │
 ┌────────┐   manifest.json   ┌────────┐    │
 │  scan  │ ────────────────► │ brief  │ ◄──┘  LLM-арт-директор (vision+text):
 └────────┘   (годность фото, │        │       kicker/headline/subtitle/badges,
               форма кадра)   └───┬────┘       валидация бюджета текста + авто-перевызов
                                 │ briefs.jsonl
                                 ▼
                          ┌────────┐  requests.jsonl   промпт: компактный (Seedream)
                          │ build  │ ──────────────►   или полный (NB2/gpt-image-2),
                          └────────┘                    все тексты в кавычках дословно
                                 │
                                 ▼
                          ┌────────┐  results.jsonl +   OpenRouter /images, бюджетный
                          │  send  │  cards/*.jpg      потолок --max-cost-usd,
                          └────────┘                   skip уже отправленных
                                 │
                                 ▼
                          ┌────────┐  qa.jsonl +        детерминированные проверки
                          │   qa   │  review_sheet.html (квадрат/размер/однотон) → VLM-
                          └────────┘                    рубрика (hard/soft/pass с поправкой
                                │                       на движок: ENGINE_SOFT_KEYS)
                                ▼
                          ┌────────┐  cards/*_fxN.jpg   Seedream: edit-починка текста по
                          │  fix   │  cards/*_rN.jpg    готовой карточке (сохраняем
                          └────────┘                   good-композицию); иначе — полный
                                                        переген с адресным addendum
                                                        по дефектам QA
```

## Ключевые модули кода

| Блок | Ответственность |
|---|---|
| `DEFAULT_PROFILE` / `load_profile` | дизайн-система и бюджеты текста как данные, не код |
| `design_spec_for` + `validate_design_spec` | арт-директор + приёмка брифа (авто-перевызов с замечаниями) |
| `build_card_payload` | ветвление компактный/полный промпт по семейству модели |
| `send_image_requests` | оплата-гейт: журнал cost, потолок, resume по (id, out_file) |
| `classify_qa` + `hard_keys_for` | QA-вердикт с ENGINE_SOFT_KEYS — защита от зацикливания |
| `build_repair_payload` | edit-режим: «перерисуй только тексты, сохрани всё» |

## Внешние зависимости

Только OpenRouter HTTP API (`/chat/completions`, `/images`, `/images/models/*/endpoints`).
Ключ — из `OPENROUTER_API_KEY`. PEP8-минимализм: stdlib + опциональный Pillow.

## Что изменять при портировании на другой магазин

Почти ничего в коде: профиль (`design_system`, `palette`, `text_budget`, `models`,
`banned_words`). В код приходится лезть только чтобы добавить новое семейство image-моделей
в `is_seedream`-подобную ветку или новый критерий в `QA_RUBRIC`.
