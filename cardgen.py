#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""marketplace-card-generator — карточки товаров в стиле маркетплейса (WB/Ozon) для ЛЮБОГО магазина.

Однофайловый CLI на стандартной библиотеке Python 3.9+ (Pillow опционально).
Весь стиль магазина вынесен в JSON-профиль (profile.json): палитра, дизайн-система,
текстовые бюджеты, модели, стоп-слова. Код-в-символ-в-символ рендеринг текстов,
QA-гейт и точечная починка текста — «извлечённые мозги» production-пайплайна pm.market.

Этапы (все resume-safe: повторный прогон не переплачивает за уже сделанное):
  scan    проверить CSV и фото -> manifest.json (годные/битые, форма кадра)
  brief   LLM-арт-директор: дизайн-бриф карточки на каждый товар -> briefs.jsonl
  build   собрать image-запросы (компактный режим для Seedream, полный для остальных)
  send    отправить в OpenRouter с бюджетным потолком -> results.jsonl + cards/*.jpg
  qa      приёмка: детерминированные проверки + VLM-рубрика -> qa.jsonl + review_sheet.html
  fix     hard-fail: edit-починка текста (Seedream) или перегенерация с адресной подсказкой
  cost    сводка затрат по журналам

Типовой прогон:
  python cardgen.py scan  --products products.csv --profile profile.json
  python cardgen.py brief --max-cost-usd 1.0
  python cardgen.py build
  python cardgen.py send  --max-cost-usd 8.0
  python cardgen.py qa
  python cardgen.py fix   --max-cost-usd 3.0
  python cardgen.py qa          # допроверит новые _rN/_fx файлы

Зависимости: urllib/json/argparse из stdlib; Pillow — только для сжатия фото под base64
и детерминированных проверок (без него qa деградирует до VLM-_only).
Ключ: $env:OPENROUTER_API_KEY='...'  (никогда не храните ключ в репозитории).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

# Windows-консоль: печатаем юникод без кракозябр
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def uprint(*a: Any) -> None:
    print(*a, flush=True)

HERE = os.path.dirname(os.path.abspath(__file__))
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
IMAGES_URL = "https://openrouter.ai/api/v1/images"
UA = "marketplace-card-generator/1.0"
SQ_TOLERANCE = 0.06

DEFAULT_OUT = os.path.join(HERE, "out")

# ---------------------------------------------------------------- профиль магазина
DEFAULT_PROFILE: Dict[str, Any] = {
    "store_name": "My Store",
    "niche": "товары для дома",
    "language": "Русский безупречный, с ё",
    "marketplaces": "WB/Ozon",
    "palette": {"headline": "#C77B8B", "text": "#1B1B1B", "bg": "белый",
                "accent_badge_bg": "#1B1B1B", "accent_badge_fg": "#C77B8B"},
    "design_system": (
        "три яруса слева вверху: 1) тип товара тонким гротеском ВЕРХНИМИ буквами; "
        "2) гигантский жирный заголовок-доминанта цветом palette.headline; "
        "3) спокойный чёрный подзаголовок. Товар справа с наклоном 20-40 град., "
        "его верхняя кромка НАЕЗЖАЕТ на заголовок (заголовок ПОД товаром, читается целиком). "
        "Внизу слева плашки столбиком: скруглённая рамка цветом palette.headline, "
        "тонкая линейная иконка по смыслу, две строки ВЕРХНИМИ буквами. "
        "Внизу справа чёрный бейдж с розовыми цифрами и тонкая цветная пилюля. "
        "Самая нижняя строка — теги через точку."),
    "text_budget": {"max_words_subtitle": 7, "max_words_badge_line": 4,
                    "max_chars_badge_line": 24, "badge_count": [3, 4]},
    "banned_words": ["акция", "купить", "хит", "скидк", "цена"],
    "banes_in_prompt": "без цен, без «купить/акция/хит», без артикулов, SKU и ID",
    "models": {"design": "google/gemini-3-flash-preview",
               "image": "bytedance-seed/seedream-5-0-flash",
               "qa": "qwen/qwen3.8-omni-flash"},
    "aspect": "1:1",
    "resolution": "2K",
    "min_side_px": 1000,
}


def load_profile(path: str) -> Dict[str, Any]:
    prof = json.loads(json.dumps(DEFAULT_PROFILE))   # deep copy
    if path and os.path.exists(path):
        user = json.loads(open(path, encoding="utf-8").read())
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(prof.get(k), dict):
                prof[k].update(v)
            else:
                prof[k] = v
    return prof


# ---------------------------------------------------------------- товары и фото
def clean_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&[a-z]+;", "", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def load_products(path: str) -> List[Dict[str, Any]]:
    """CSV с колонками id,title,description,category,image (image — путь к фото)."""
    import csv as _csv
    out = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in _csv.DictReader(f):
            pid = (row.get("id") or "").strip()
            if not pid:
                continue
            out.append({"id": pid, "title": clean_html(row.get("title") or ""),
                        "description": clean_html(row.get("description") or "")[:2500],
                        "category": clean_html(row.get("category") or ""),
                        "image": (row.get("image") or "").strip()})
    return out


def context_block(p: Dict[str, Any]) -> str:
    parts = [f"Название: {p['title']}"]
    if p.get("category"):
        parts.append(f"Категория: {p['category']}")
    if p.get("description"):
        parts.append(f"Описание: {p['description']}")
    return "\n".join(parts)


def maybe_optimize(raw: bytes, max_edge: int, quality: int) -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return raw
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_edge:
            r = max_edge / max(w, h)
            img = img.resize((int(w * r), int(h * r)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return raw


def photo_data_url(path: str, max_edge: int = 1280, quality: int = 82) -> str:
    data = maybe_optimize(open(path, "rb").read(), max_edge, quality)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    elif not t.startswith("{"):
        s, e = t.find("{"), t.rfind("}")
        if s == -1 or e <= s:
            return None
        t = t[s:e + 1]
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def chat(messages: List[Dict[str, Any]], key: str, model: str,
         temperature: float = 0.4, max_tokens: int = 1400, title: str = "cardgen") -> Dict[str, Any]:
    body = json.dumps({"model": model, "temperature": temperature,
                       "max_tokens": max_tokens, "messages": messages}).encode("utf-8")
    req = urllib.request.Request(CHAT_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/bouncemonster/marketplace-card-generator",
        "X-Title": title, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- этап brief: арт-директор
BRIEF_PROMPT = """Ты — арт-директор и копирайтер карточек маркетплейса ({marketplaces}) магазина
«{store_name}» ({niche}). По фото товара и его тексту собери ДИЗАЙН-БРИФ главной карточки.
Верни СТРОГО JSON без пояснений вне JSON:
{{
 "kicker": "тип товара ВЕРХНИМИ буквами, 1-3 слова",
 "headline": "1-2 слова бренд/модель для гигантского цветного заголовка-доминанты",
 "subtitle": "продающий подзаголовок 4-{max_sub} КОРОТКИХ слов (модели надёжно рендерят только короткие фразы), без длинных составных слов",
 "badges": [{{"line1": "...", "line2": "...", "icon": "описание тонкой линейной иконки"}}],
 "badge_black": "главная числовая характеристика ТОЛЬКО из текста, иначе пустая строка",
 "badge_pill": "вторая числовая характеристика ТОЛЬКО из текста, иначе пустая строка",
 "bottom_tags": ["бренд", "тип товара"],
 "fonts": "1-2 строки: шрифтовая пара",
 "angle": "одна строка: главный selling-аргумент"
}}
Плашек ровно {badge_min}-{badge_max}. Каждая строка плашки — ВЕРХНИМИ буквами,
не больше {max_bw} слов и {max_bc} символов; line1 — факт/выгода, line2 — что это даёт покупателю.
Только КОНКРЕТИКА из характеристик и фото; банальности запрещены («высокое качество»,
«надёжность», «удобство», «инновационность»). Не выдумывай свойства, состав, цифры.
Запрещено: {banned}. {language}. Текст должен быть заманчив, как у бестселлеров {marketplaces}."""


def validate_design_spec(d: Dict[str, Any], prof: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    if not isinstance(d, dict) or not d:
        return ["бриф не распарсился"]
    for k in ("kicker", "headline", "subtitle"):
        if not str(d.get(k) or "").strip():
            issues.append(f"пустое {k}")
    tb = prof["text_budget"]
    badges = d.get("badges") or []
    lo, hi = tb.get("badge_count", [3, 4])
    if not (lo <= len(badges) <= hi):
        issues.append(f"плашек {len(badges)} (нужно {lo}-{hi})")
    for i, b in enumerate(badges, 1):
        l1 = str((b or {}).get("line1") or "").strip() if isinstance(b, dict) else ""
        l2 = str((b or {}).get("line2") or "").strip() if isinstance(b, dict) else ""
        if not l1 or not l2:
            issues.append(f"плашка {i}: пустая строка")
        for l in (l1, l2):
            if l and len(l) > tb["max_chars_badge_line"]:
                issues.append(f"плашка {i}: {len(l)} симв. > {tb['max_chars_badge_line']}")
            if l and len(l.split()) > tb["max_words_badge_line"]:
                issues.append(f"плашка {i}: {len(l.split())} слов > {tb['max_words_badge_line']} (битый текст)")
            for w in prof["banned_words"]:
                if w in (" " + l.lower() + " "):
                    issues.append(f"плашка {i}: запрещённое слово «{w}»")
    if len(str(d.get("subtitle") or "").split()) > tb["max_words_subtitle"]:
        issues.append(f"подзаголовок длиннее {tb['max_words_subtitle']} слов — модели коверкают длинные фразы")
    return issues


def design_spec_for(p: Dict[str, Any], photo_path: str, key: str, prof: Dict[str, Any],
                    extra: str = "") -> Dict[str, Any]:
    tb = prof["text_budget"]
    prompt = BRIEF_PROMPT.format(
        store_name=prof["store_name"], niche=prof["niche"], marketplaces=prof["marketplaces"],
        language=prof["language"], banned=", ".join(prof["banned_words"]),
        max_sub=tb["max_words_subtitle"] - 1, max_bw=tb["max_words_badge_line"] - 1,
        max_bc=tb["max_chars_badge_line"], badge_min=tb["badge_count"][0], badge_max=tb["badge_count"][1])
    if extra:
        prompt += "\n" + extra
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "Контекст товара:\n" + context_block(p)},
        {"type": "text", "text": "Фото товара:"},
        {"type": "image_url", "image_url": {"url": photo_data_url(photo_path, 1024, 82)}},
        {"type": "text", "text": prompt}]}]
    data = chat(messages, key, prof["models"]["design"], title="cardgen brief")
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    d = _extract_json(content) or {}
    if d:
        d["_usage"] = data.get("usage") or {}
    return d


# ---------------------------------------------------------------- текстовые блоки промптов
def design_text_lines(d: Dict[str, Any]) -> str:
    """Все тексты брифа одной строкой в кавычках (компактный Seedream-промпт)."""
    parts: List[str] = []
    for key in ("kicker", "headline", "subtitle", "badge_black", "badge_pill"):
        v = str(d.get(key) or "").strip()
        if v:
            parts.append(f'"{v}"')
    for b in (d.get("badges") or [])[:4]:
        if not isinstance(b, dict):
            continue
        for lk in ("line1", "line2"):
            v = str(b.get(lk) or "").strip()
            if v:
                parts.append(f'"{v}"')
    tags = [str(t).strip().upper() for t in (d.get("bottom_tags") or []) if str(t).strip()][:3]
    if tags:
        parts.append(" · ".join(f'"{t}"' for t in tags))
    return ", ".join(parts)


CARD_PROMPT_COMPACT = """Оформи фото товара (первое изображение) как премиальную карточку маркетплейса.
Кадр: {frame}, разрешение {resolution}. Товар — строго 1:1 с фото (та же форма и цвет, не перекрашивай).
Композиция: {design_system}
Рендери ТОЛЬКО эти надписи, дословно, символ в символ, простым современным гротеском: {texts}
Никаких других текстов и чисел: {banned}.
Источник чисел для бейджей (ничего сверх не выдумывай): {context}"""

CARD_PROMPT_FULL = """Сделай инфографику-карточку маркетплейса на основе фото товара (первое изображение).
Кадр: {frame}, {resolution_note}. Товар строго 1:1: та же форма, цвет, материал, детали; не перекрашивай,
не заменяй товар. Фон чистый и светлый, товар читаемый.
Дизайн-система магазина (обязательна): {design_system}
Тексты — дословно, символ в символ, только из кавычек; без своих слов, без перефразирований: {texts}
Запрещено в кадре: {banned}, служебные коды, «{store_name}» в углу, артикулы.
Данные товара (источник фактов и чисел, ничего не выдумывай): {context}"""

REPAIR_PROMPT = """Первое изображение — готовая карточка товара. Полностью сохрани товар, фон,
композицию, плашки, бейджи, цвета и шрифты — ничего не переставляй и не перерисовывай.
Перерисуй ТОЛЬКО надписи — дословно, символ в символ, тем же шрифтом, того же размера и цвета,
каждая на своём месте: {texts}
{garble}Никаких других текстов, чисел, артикулов и цен."""


def is_seedream(model: str) -> bool:
    m = (model or "").lower()
    return "seedream" in m or "bytedance-seed" in m


def build_card_payload(p: Dict[str, Any], ref_url: str, design: Dict[str, Any],
                       prof: Dict[str, Any], cap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    model = prof["models"]["image"]
    frame = f"квадрат 1:1" if prof["aspect"] == "1:1" else f"кадр {prof['aspect']}"
    texts = design_text_lines(design)
    ctx = context_block(p).replace("\n", " ")[:900]
    if is_seedream(model):
        # гайды Seedream: короткий фокусный промпт рендерит тексты надёжнее простыни ТЗ
        prompt = CARD_PROMPT_COMPACT.format(frame=frame, resolution=prof["resolution"],
                                            design_system=prof["design_system"], texts=texts,
                                            banned=prof["banes_in_prompt"], context=ctx)
    else:
        prompt = CARD_PROMPT_FULL.format(frame=frame, resolution_note=prof["resolution"],
                                         design_system=prof["design_system"], texts=texts,
                                         banned=prof["banes_in_prompt"], context=ctx,
                                         store_name=prof["store_name"])
    payload: Dict[str, Any] = {"model": model, "prompt": prompt,
                               "input_references": [{"type": "image_url", "image_url": {"url": ref_url}}],
                               "aspect_ratio": prof["aspect"], "n": 1}
    return attach_params(payload, prof, cap)


def build_repair_payload(card_ref: str, design: Dict[str, Any], garbled: List[str],
                         prof: Dict[str, Any],
                         cap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    garble = ""
    if garbled:
        garble = ("Исправь битые слова, таких в кадре быть не должно: "
                  + ", ".join(f"«{g}»" for g in garbled[:6]) + ". ")
    prompt = REPAIR_PROMPT.format(texts=design_text_lines(design), garble=garble)
    payload: Dict[str, Any] = {"model": prof["models"]["image"], "prompt": prompt,
                               "input_references": [{"type": "image_url", "image_url": {"url": card_ref}}],
                               "aspect_ratio": prof["aspect"], "n": 1}
    return attach_params(payload, prof, cap)


def attach_params(payload: Dict[str, Any], prof: Dict[str, Any],
                  cap: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Шлём только то, что эндпоинт объявил поддерживаемым (иначе 400)."""
    res = prof.get("resolution")
    if cap and not cap.get("error"):
        if res and res in (cap.get("resolutions") or []):
            payload["resolution"] = res
    else:
        if res:
            payload["resolution"] = res
    return payload


def image_capabilities(model: str, key: str) -> Dict[str, Any]:
    url = f"https://openrouter.ai/api/v1/images/models/{model}/endpoints"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        d = data.get("data") if isinstance(data.get("data"), dict) else data
        eps = (d or {}).get("endpoints") or []
        if not eps:
            return {"error": "endpoints пуст"}
        sp = eps[0].get("supported_parameters") or {}
        prices = []
        for ep in eps:
            for pr in ep.get("pricing") or []:
                if pr.get("billable") == "output_image":
                    prices.append(float(pr.get("cost_usd") or 0))
        return {"resolutions": ((sp.get("resolution") or {}).get("values")) or [],
                "aspect_ratios": ((sp.get("aspect_ratio") or {}).get("values")) or [],
                "price_usd_min": min(prices) if prices else None}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- QA
QA_RUBRIC = """Ты — ревизор e-commerce карточек. Первое изображение — ОРИГИНАЛЬНОЕ фото товара.
Второе — сгенерированная карточка. {brief_note}Верни СТРОГО JSON:
{{
 "product_match": bool, "product_color_ok": bool, "text_readable_ru": bool,
 "follows_design_brief": bool, "no_sku_leak": bool, "headline_legible": bool,
 "title_product_overlap": bool, "font_style_ok": bool, "no_invented_facts": bool,
 "garbled_words": ["..."],
 "verdict": "pass|soft|hard", "hard_fails": ["ключи с false, считаем грубым браком"]
}}
Грубым браком (verdict=hard) считай: product_match=false, product_color_ok=false,
no_sku_leak=false, text_readable_ru=false, no_invented_facts=false.
no_invented_facts: свойства и числа подтверждены исходным фото или описанием товара;
дизайн-бриф сам по себе не подтверждает свойства. Остальное — soft, если не портит покупку.
{language}."""

QA_HARD_KEYS = ("product_match", "product_color_ok", "no_sku_leak", "text_readable_ru",
                "no_invented_facts")
# Приёмы, которые движок физически не выполняет, понижаем до soft — иначе зацикливание
# перегенераций. Seedream: товар-поверх-заголовка и перетасовка пар фраз в плашках.
ENGINE_SOFT_KEYS = {"seedream": ("title_product_overlap", "follows_design_brief")}


def hard_keys_for(model: Optional[str]) -> tuple:
    drop = ENGINE_SOFT_KEYS["seedream"] if (model and is_seedream(model)) else ()
    return tuple(k for k in QA_HARD_KEYS if k not in drop)


def classify_qa(det: List[str], verdict: Dict[str, Any], gen_model: Optional[str] = None) -> str:
    if det:
        return "hard"
    v = (verdict.get("verdict") or "").lower()
    if v == "error":
        return "error"
    if any(verdict.get(k) is False for k in hard_keys_for(gen_model)):
        return "hard"
    if v == "hard":
        return "soft"      # VLM назвал hard, но значимые для движка ключи ok
    if any(verdict.get(k) is False for k in
           ("follows_design_brief", "title_product_overlap", "font_style_ok", "headline_legible")):
        return "soft"
    return "pass"


def deterministic_checks(path: str, prof: Dict[str, Any]) -> List[str]:
    issues: List[str] = []
    try:
        from PIL import Image
    except ImportError:
        return issues
    try:
        img = Image.open(path)
        img.load()
    except Exception as exc:
        return [f"не открывается: {type(exc).__name__}: {exc}"]
    w, h = img.size
    if not w or not h:
        return ["пустое изображение"]
    if prof["aspect"] == "1:1" and abs(w - h) / max(w, h) > SQ_TOLERANCE:
        issues.append(f"не квадрат {w}x{h}")
    if min(w, h) < int(prof.get("min_side_px", 0)):
        issues.append(f"короткая сторона {min(w, h)}px < {prof['min_side_px']}px")
    try:
        ex = img.convert("L").getextrema()
        if ex[1] - ex[0] < 12:
            issues.append(f"однотонный кадр (яркость {ex[0]}-{ex[1]})")
    except Exception:
        pass
    return issues


def vlm_qa(prod: Dict[str, Any], photo_path: str, card_path: str, key: str, prof: Dict[str, Any],
           design: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    brief_note = ""
    if design:
        brief_note = ("ДИЗАЙН-БРИФ (эталон текстов, карточка обязана следовать дословно):\n"
                      + design_text_lines(design) + "\n\n")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": f"Товар: {prod['title']}\n"
                                 + f"Описание товара: {clean_html(prod.get('description') or '')}\n"
                                 + QA_RUBRIC.format(brief_note=brief_note, language=prof["language"])},
        {"type": "text", "text": "Изображение 1 — ОРИГИНАЛЬНОЕ фото товара:"},
        {"type": "image_url", "image_url": {"url": photo_data_url(photo_path, 1024, 82)}},
        {"type": "text", "text": "Изображение 2 — СГЕНЕРИРОВАННАЯ карточка (проверяемая):"},
        {"type": "image_url", "image_url": {"url": photo_data_url(card_path, 1280, 85)}}]}]
    data = chat(messages, key, prof["models"]["qa"], temperature=0.0, max_tokens=700,
                title="cardgen qa")
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    verdict = _extract_json(content) or {"verdict": "error", "reason": "VLM не вернул JSON"}
    verdict["_usage"] = data.get("usage") or {}
    return verdict


DEFECT_ADDENDUM = {
    "product_match": "Товар искажён/заменён: верни ТОЧНО тот же товар с фото-референса.",
    "product_color_ok": "Цвет/материал товара изменён: верни оригинальный цвет.",
    "no_sku_leak": "В кадр попал артикул/SKU/ID/цена: полностью убери служебные строки.",
    "no_invented_facts": "Выдуманные характеристики: убери свойства и числа, не подтверждённые исходным фото или описанием товара.",
    "text_readable_ru": "Русский текст нечитаем/с ошибками: перерисуй все надписи аккуратно, "
                        "строго из кавычек, без битых и выдуманных слов.",
    "title_product_overlap": "Товар должен перекрывать правый нижний край заголовка (10-25% ширины), "
                             "заголовок при этом читается целиком.",
    "follows_design_brief": "Тексты разошлись с брифом: рендери ровно тексты из кавычек, символ в символ.",
    "headline_legible": "Заголовок нечитаем под товаром: уменьши наложение.",
    "font_style_ok": "Шрифты не по брифу: заголовок heavy, остальное light, плашки капсом.",
}


def build_fix_addendum(verdict: Dict[str, Any], gen_model: Optional[str]) -> str:
    keys = [k for k in hard_keys_for(gen_model) + tuple(ENGINE_SOFT_KEYS["seedream"])
            if verdict.get(k) is False]
    lines = [DEFECT_ADDENDUM[k] for k in keys if k in DEFECT_ADDENDUM]
    garbled = [g for g in (verdict.get("garbled_words") or []) if isinstance(g, str)]
    if garbled:
        lines.append("Исправь битые слова: " + ", ".join(garbled[:6]) + ".")
    if not lines:
        lines.append("Карточка с браком — перегенерируй строго по ТЗ.")
    return "ПЕРЕГЕНЕРАЦИЯ — УСТРАНИ КОНКРЕТНЫЕ ДЕФЕКТЫ:\n- " + "\n- ".join(lines)


# ---------------------------------------------------------------- журналы
def journal_append(path: str, rec: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def journal_read(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    for l in open(path, encoding="utf-8"):
        try:
            out.append(json.loads(l))
        except Exception:
            pass
    return out


def send_image_requests(reqs: List[Dict[str, Any]], key: str, out_path: str, cards_dir: str,
                        delay: float, limit: Optional[int], max_cost: Optional[float]) -> None:
    os.makedirs(cards_dir, exist_ok=True)
    done, spent = set(), 0.0
    for rec in journal_read(out_path):
        done.add((str(rec.get("product_id")), rec.get("out_file")))
        spent += float((rec.get("usage") or {}).get("cost") or 0)
    sent = 0
    for req in reqs:
        if limit and sent >= limit:
            break
        if (str(req["product_id"]), req.get("out_file")) in done:
            continue
        if max_cost is not None and spent >= max_cost:
            uprint(f"СТОП: бюджет ${max_cost} достигнут (потрачено ${spent:.4f}). "
                   f"Продолжить — повтори команду с большим --max-cost-usd.")
            break
        body = json.dumps(req["payload"]).encode("utf-8")
        http_req = urllib.request.Request(IMAGES_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/bouncemonster/marketplace-card-generator",
            "X-Title": "cardgen", "User-Agent": UA})
        rec = {"product_id": req["product_id"], "out_file": req["out_file"],
               "model": req["payload"].get("model"), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        data: Dict[str, Any] = {}
        try:
            with urllib.request.urlopen(http_req, timeout=300) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            item = (data.get("data") or [{}])[0]
            b64 = item.get("b64_json") or ""
            if not b64 and item.get("url"):
                with urllib.request.Request(item["url"], headers={"User-Agent": UA}) as r2, \
                        urllib.request.urlopen(r2, timeout=180) as resp2:
                    b64 = base64.b64encode(resp2.read()).decode("ascii")
            if b64:
                path = os.path.join(cards_dir, req["out_file"])
                with open(path, "wb") as f:
                    f.write(base64.b64decode(b64))
                rec.update({"status": "ok", "saved": req["out_file"]})
            else:
                rec.update({"status": "error", "error": "пустой ответ"})
        except Exception as exc:
            rec.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]})
        usage = data.get("usage") or {}
        cost = float(usage.get("cost") or 0)
        spent += cost
        rec["usage"] = {"cost": cost}
        journal_append(out_path, rec)
        sent += 1
        uprint(f"[{sent}] {req['product_id']} {rec.get('status')} cost=${cost:.4f} итого=${spent:.4f}")
        if delay:
            time.sleep(delay)


# ---------------------------------------------------------------- команды
def common_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--profile", default=os.path.join(HERE, "profile.json"))
    sp.add_argument("--out-dir", dest="out_dir", default=DEFAULT_OUT)
    sp.add_argument("--api-key", default=None, help="или env OPENROUTER_API_KEY")


def get_key(args: argparse.Namespace) -> Optional[str]:
    key = args.api_key or os.environ.get("OPENROUTER_API_KEY") or ""
    if not key:
        uprint("OPENROUTER_API_KEY не задан")
    return key or None


def cmd_scan(args: argparse.Namespace) -> int:
    prof = load_profile(args.profile)
    products = load_products(args.products)
    rows, bad = [], 0
    for p in products:
        path = p["image"] if os.path.isabs(p["image"]) else os.path.join(
            os.path.dirname(os.path.abspath(args.products)), p["image"])
        ok = bool(p["image"]) and os.path.exists(path)
        shape = "unknown"
        if ok:
            try:
                from PIL import Image
                w, h = Image.open(path).size
                shape = "square" if abs(w - h) / max(w, h) <= SQ_TOLERANCE else \
                    ("portrait" if h > w else "landscape")
            except Exception:
                pass
        if not ok:
            bad += 1
        rows.append({"id": p["id"], "title": p["title"], "image_file": os.path.basename(path) if ok else "",
                     "image_abs": path if ok else "", "shape": shape,
                     "status": "ok" if ok else "no_photo"})
    mf = os.path.join(args.out_dir, "manifest.json")
    os.makedirs(args.out_dir, exist_ok=True)
    open(mf, "w", encoding="utf-8").write(json.dumps(rows, ensure_ascii=False, indent=1))
    uprint(f"Товаров: {len(rows)}, с фото: {sum(1 for r in rows if r['status'] == 'ok')}, без фото: {bad}")
    uprint(f"manifest -> {mf}  (профиль: {prof['store_name']}, image-модель: {prof['models']['image']})")
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    key = get_key(args)
    if not key:
        return 2
    prof = load_profile(args.profile)
    products = {p["id"]: p for p in load_products(args.products)}
    manifest = {m["id"]: m for m in json.loads(open(os.path.join(args.out_dir, "manifest.json"),
                                                    encoding="utf-8").read())}
    out_path = os.path.join(args.out_dir, "briefs.jsonl")
    done = {str(r["product_id"]) for r in journal_read(out_path)}
    spent, n = 0.0, 0
    for pid, p in products.items():
        if pid in done:
            continue
        m = manifest.get(pid) or {}
        if m.get("status") != "ok":
            continue
        if args.limit and n >= args.limit:
            break
        if args.max_cost_usd is not None and spent >= args.max_cost_usd:
            uprint(f"СТОП: бюджет ${args.max_cost_usd} (потрачено ${spent:.4f})")
            break
        try:
            d = design_spec_for(p, m["image_abs"], key, prof)
        except Exception as exc:
            uprint(f"  {pid}: ОШИБКА {type(exc).__name__}: {exc}")
            continue
        usage = d.pop("_usage", None) or {}
        spent += float(usage.get("cost") or 0)
        issues = validate_design_spec(d, prof)
        if issues:                        # одна перезапись с замечаниями приёмки
            d2 = {}
            try:
                d2 = design_spec_for(p, m["image_abs"], key, prof,
                                     extra="Прежний бриф отклонён: " + "; ".join(issues) +
                                           ". Исправь и верни новый JSON.")
            except Exception:
                pass
            u2 = d2.pop("_usage", None) or {}
            spent += float(u2.get("cost") or 0)
            if d2 and not validate_design_spec(d2, prof):
                d, issues = d2, []
        rec = {"product_id": pid, "title": p["title"], "model": prof["models"]["design"],
               "design": d, "usage": usage, "issues": issues}
        journal_append(out_path, rec)
        n += 1
        uprint(f"[{n}] {pid} {'OK' if not issues else 'ISSUES ' + str(issues)} "
               f"| {d.get('headline', '')} — {d.get('subtitle', '')}")
        if args.delay:
            time.sleep(args.delay)
    uprint(f"\nБрифов создано: {n}, потрачено ${spent:.4f}. Журнал -> {out_path}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    prof = load_profile(args.profile)
    products = {p["id"]: p for p in load_products(args.products)}
    manifest = {m["id"]: m for m in json.loads(open(os.path.join(args.out_dir, "manifest.json"),
                                                    encoding="utf-8").read())}
    designs = {str(r["product_id"]): r.get("design") or {}
               for r in journal_read(os.path.join(args.out_dir, "briefs.jsonl")) if r.get("design")}
    key = get_key(args) or ""
    cap = image_capabilities(prof["models"]["image"], key) if key else {"error": "нет ключа"}
    if cap.get("error"):
        uprint(f"WARNING: возможности модели не проверены ({cap['error']}) — идём с параметрами профиля")
    reqs, skipped = [], 0
    for pid, p in products.items():
        m = manifest.get(pid) or {}
        if m.get("status") != "ok" or pid not in designs:
            skipped += 1
            continue
        try:
            ref = photo_data_url(m["image_abs"], 2048, 90)
        except Exception as exc:
            uprint(f"  {pid}: фото не читается ({exc}) — пропуск")
            skipped += 1
            continue
        payload = build_card_payload(p, ref, designs[pid], prof, cap)
        stem = os.path.splitext(m["image_file"])[0]
        reqs.append({"product_id": pid, "title": p["title"],
                     "out_file": f"{stem}_card.jpg", "payload": payload})
    out_path = os.path.join(args.out_dir, "requests.jsonl")
    open(out_path, "w", encoding="utf-8").write(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in reqs) + ("\n" if reqs else ""))
    mode = "компактный (Seedream)" if is_seedream(prof["models"]["image"]) else "полный"
    uprint(f"Запросов собрано: {len(reqs)} (пропущено без брифа/фото: {skipped}), режим промпта: {mode}")
    uprint(f"requests -> {out_path}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    key = get_key(args)
    if not key:
        return 2
    prof = load_profile(args.profile)
    reqs = journal_read(os.path.join(args.out_dir, "requests.jsonl"))
    if not reqs:
        uprint("Нет requests.jsonl — сначала build")
        return 2
    for r in reqs:
        r["payload"]["model"] = prof["models"]["image"]
    uprint(f"Модель {prof['models']['image']}, запросов {len(reqs)}, "
           f"потолок: ${args.max_cost_usd if args.max_cost_usd else 'не задан'}")
    send_image_requests(reqs, key, os.path.join(args.out_dir, "results.jsonl"),
                        os.path.join(args.out_dir, "cards"), args.delay, args.limit, args.max_cost_usd)
    return 0


def cmd_qa(args: argparse.Namespace) -> int:
    key = get_key(args)
    if not key:
        return 2
    prof = load_profile(args.profile)
    products = {p["id"]: p for p in load_products(args.products)}
    manifest = {m["id"]: m for m in json.loads(open(os.path.join(args.out_dir, "manifest.json"),
                                                    encoding="utf-8").read())}
    designs = {str(r["product_id"]): r.get("design") or {}
               for r in journal_read(os.path.join(args.out_dir, "briefs.jsonl")) if r.get("design")}
    results = journal_read(os.path.join(args.out_dir, "results.jsonl"))
    out_path = os.path.join(args.out_dir, "qa.jsonl")
    done = {(str(r["product_id"]), r.get("card")) for r in journal_read(out_path)}
    cards_dir = os.path.join(args.out_dir, "cards")
    counts = {"hard": 0, "soft": 0, "pass": 0, "error": 0, "skip": 0}
    n = 0
    for r in results:
        if r.get("status") != "ok" or not r.get("saved"):
            continue
        pid = str(r["product_id"])
        if (pid, r["saved"]) in done:
            counts["skip"] += 1
            continue
        if args.limit and n >= args.limit:
            break
        card_path = os.path.join(cards_dir, r["saved"])
        m = manifest.get(pid) or {}
        if not os.path.exists(card_path) or m.get("status") != "ok":
            continue
        det = deterministic_checks(card_path, prof)
        prod = products.get(pid) or {"id": pid, "title": r.get("title", ""), "description": "",
                                     "category": "", "image": ""}
        try:
            verdict = vlm_qa(prod, m["image_abs"], card_path, key, prof, designs.get(pid))
            final = classify_qa(det, verdict, r.get("model"))
        except Exception as exc:
            verdict, final = {"verdict": "error", "reason": str(exc)[:200]}, "error"
        rec = {"product_id": pid, "card": r["saved"], "det_issues": det, "verdict": verdict,
               "final": final, "model": r.get("model")}
        journal_append(out_path, rec)
        counts[final] = counts.get(final, 0) + 1
        n += 1
        uprint(f"[{n}] {pid} {final} {r['saved']}")
        if args.delay:
            time.sleep(args.delay)
    render_sheet(journal_read(out_path), cards_dir, os.path.join(args.out_dir, "review_sheet.html"))
    uprint(f"\nQA: hard={counts['hard']} soft={counts['soft']} pass={counts['pass']} "
           f"error={counts['error']} skip={counts['skip']}")
    uprint(f"Журнал -> {out_path}\nЛист ручной проверки -> {os.path.join(args.out_dir, 'review_sheet.html')}")
    return 0


def render_sheet(records: List[Dict[str, Any]], cards_dir: str, out_html: str) -> None:
    rows = []
    order = {"hard": 0, "error": 1, "soft": 2, "pass": 3}
    for r in sorted(records, key=lambda x: order.get(x.get("final", ""), 4)):
        col = {"hard": "#c0392b", "soft": "#e08a00", "pass": "#2e8b57", "error": "#7f8c8d"}.get(r.get("final", ""), "#333")
        v = r.get("verdict") or {}
        detail = "; ".join(r.get("det_issues") or []) or "; ".join(v.get("hard_fails") or []) or r.get("final", "")
        rows.append(f"<tr><td>{r.get('product_id','')}</td>"
                    f"<td><img src='cards/{r.get('card','')}'></td>"
                    f"<td><b style='color:{col}'>{r.get('final','')}</b><br><small>{detail}</small></td></tr>")
    html = ("<!doctype html><meta charset='utf-8'><title>QA карточек</title>"
            "<style>body{font-family:system-ui}table{border-collapse:collapse}"
            "td{border:1px solid #ddd;padding:6px;vertical-align:top}img{width:280px;object-fit:cover}</style>"
            "<h2>Ручная проверка карточек</h2><table><tr><th>id</th><th>Карточка</th><th>QA</th></tr>"
            + "".join(rows) + "</table>")
    open(out_html, "w", encoding="utf-8").write(html)


def cmd_fix(args: argparse.Namespace) -> int:
    """hard-fail: Seedream чиним edit-проходом по готовой карточке (бит только текст),
    остальные приёмы/движки — полная перегенерация с адресной подсказкой."""
    key = get_key(args)
    if not key:
        return 2
    prof = load_profile(args.profile)
    qa = journal_read(os.path.join(args.out_dir, "qa.jsonl"))
    latest: Dict[str, Dict[str, Any]] = {}
    for r in qa:
        latest[str(r["product_id"])] = r
    hard = {pid: r for pid, r in latest.items() if r.get("final") == "hard"}
    if not hard:
        uprint("hard-fail нет — чинить нечего.")
        return 0
    products = {p["id"]: p for p in load_products(args.products)}
    manifest = {m["id"]: m for m in json.loads(open(os.path.join(args.out_dir, "manifest.json"),
                                                    encoding="utf-8").read())}
    designs = {str(r["product_id"]): r.get("design") or {}
               for r in journal_read(os.path.join(args.out_dir, "briefs.jsonl")) if r.get("design")}
    reqs = {str(r["product_id"]): r for r in journal_read(os.path.join(args.out_dir, "requests.jsonl"))}
    cap = image_capabilities(prof["models"]["image"], key)
    cards_dir = os.path.join(args.out_dir, "cards")
    built, n_repair = [], 0
    for pid, r in hard.items():
        prod, m, design = products.get(pid), manifest.get(pid) or {}, designs.get(pid)
        if not prod or not design:
            continue
        v = r.get("verdict") or {}
        card_path = os.path.join(cards_dir, r.get("card") or "")
        repair = (is_seedream(prof["models"]["image"]) and os.path.isfile(card_path)
                  and v.get("text_readable_ru") is False
                  and v.get("product_match") is not False and v.get("product_color_ok") is not False
                  and not r.get("det_issues"))
        attempt = sum(1 for q in qa if str(q.get("product_id")) == pid) + 1
        if repair:
            payload = build_repair_payload(photo_data_url(card_path, 2048, 90), design,
                                           [g for g in (v.get("garbled_words") or []) if isinstance(g, str)],
                                           prof, cap)
            out_file = f"{os.path.splitext(r['card'])[0]}_fx{attempt}.jpg"
            n_repair += 1
        else:
            base = reqs.get(pid) or {}
            src = None
            try:
                src = (base.get("payload", {}).get("input_references", [{}])[0]
                       .get("image_url", {}).get("url") or None)
            except Exception:
                src = None
            if not src and m.get("image_abs"):
                src = photo_data_url(m["image_abs"], 2048, 90)
            if not src:
                continue
            payload = build_card_payload(prod, src, design, prof, cap)
            add = build_fix_addendum(v, r.get("model"))
            payload["prompt"] = payload["prompt"] + "\n\n" + add
            out_file = f"{os.path.splitext(base.get('out_file') or m.get('image_file', 'card'))[0]}_r{attempt}.jpg"
        built.append({"product_id": pid, "out_file": out_file, "payload": payload})
    uprint(f"К починке: {len(built)} (edit-repair текста: {n_repair}), модель {prof['models']['image']}")
    if args.dry_run:
        for b in built[:3]:
            uprint(f"  {b['product_id']} -> {b['out_file']}: {b['payload']['prompt'][-200:]}")
        uprint("DRY RUN — оплата не производилась.")
        return 0
    send_image_requests(built, key, os.path.join(args.out_dir, "results.jsonl"),
                        cards_dir, args.delay, args.limit, args.max_cost_usd)
    uprint("Повтори qa — он допроверит новые _fx/_r файлы.")
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    total = 0.0
    for name in ("results.jsonl", "qa.jsonl", "briefs.jsonl"):
        s = sum(float((r.get("usage") or {}).get("cost") or 0)
                for r in journal_read(os.path.join(args.out_dir, name)))
        n = len(journal_read(os.path.join(args.out_dir, name)))
        total += s
        uprint(f"{name:16} записей {n:5} | cost ${s:.4f}")
    uprint(f"ИТОГО по журналам: ${total:.4f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="проверить CSV и фото -> manifest.json")
    s.add_argument("--products", default=os.path.join(HERE, "products.csv"))
    common_args(s)
    s.set_defaults(func=cmd_scan)

    b = sub.add_parser("brief", help="дизайн-брифы арт-директора -> briefs.jsonl")
    b.add_argument("--products", default=os.path.join(HERE, "products.csv"))
    b.add_argument("--limit", type=int, default=None)
    b.add_argument("--delay", type=float, default=1.0)
    b.add_argument("--max-cost-usd", dest="max_cost_usd", type=float, default=None)
    common_args(b)
    b.set_defaults(func=cmd_brief)

    u = sub.add_parser("build", help="собрать image-запросы -> requests.jsonl")
    u.add_argument("--products", default=os.path.join(HERE, "products.csv"))
    common_args(u)
    u.set_defaults(func=cmd_build)

    d = sub.add_parser("send", help="генерация в OpenRouter -> results.jsonl + cards/")
    d.add_argument("--limit", type=int, default=None)
    d.add_argument("--delay", type=float, default=1.0)
    d.add_argument("--max-cost-usd", dest="max_cost_usd", type=float, default=None)
    common_args(d)
    d.set_defaults(func=cmd_send)

    q = sub.add_parser("qa", help="приёмка карточек -> qa.jsonl + review_sheet.html")
    q.add_argument("--products", default=os.path.join(HERE, "products.csv"))
    q.add_argument("--limit", type=int, default=None)
    q.add_argument("--delay", type=float, default=0.5)
    common_args(q)
    q.set_defaults(func=cmd_qa)

    f = sub.add_parser("fix", help="починить hard-fail: edit-repair текста или перегенерация")
    f.add_argument("--products", default=os.path.join(HERE, "products.csv"))
    f.add_argument("--limit", type=int, default=None)
    f.add_argument("--delay", type=float, default=1.0)
    f.add_argument("--max-cost-usd", dest="max_cost_usd", type=float, default=None)
    f.add_argument("--dry-run", action="store_true")
    common_args(f)
    f.set_defaults(func=cmd_fix)

    c = sub.add_parser("cost", help="сводка затрат по журналам")
    common_args(c)
    c.set_defaults(func=cmd_cost)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
