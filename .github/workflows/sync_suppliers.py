"""
PrimePhone — Синхронізація постачальників з Supabase
=====================================================
Запускається автоматично через GitHub Actions щодня о 8:00
Або вручну: python sync_suppliers.py

Постачальники:
  1. E-techno (Prom) — телефони (+16%)
  2. Elektrihka — телефони та аксесуари (+16% / +20%)
  3. ITsellopt — аксесуари (+20%)
"""

import xml.etree.ElementTree as ET
import requests
import json
import re
import os
from datetime import datetime

# ─── Налаштування ───────────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://xfebwpaolthnsmufckqb.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")  # anon public key з GitHub Secrets

MARGIN_PHONES      = 1.16   # +16%
MARGIN_ACCESSORIES = 1.20   # +20%

SUPPLIERS = [
    {
        "name": "etechno",
        "label": "E-techno (Prom)",
        "url": "https://internet-magazin-e-techno-cs3593744.prom.ua/products_feed.xml?hash_tag=4774cbf42861148040defa05a8fed6ba&sales_notes=&product_ids=&label_ids=&exclude_fields=&html_description=0&yandex_cpa=&process_presence_sure=&languages=uk%2Cru&extra_fields=&group_ids=",
        "type": "phones",   # тільки телефони
    },
    {
        "name": "elektrihka",
        "label": "Elektrihka",
        "url": "https://elektrihka.com.ua/content/export/15ae6a32e4e6211f4e961a0afd2e50ab.xml",
        "type": "mixed",    # телефони + аксесуари (розділяємо по категоріям)
        # Категорії телефонів у elektrihka
        "phone_category_ids": {"1055","1056","1058","1061","1064","1069","1076","1077","1078","1084","1086","1106","1152"},
    },
    {
        "name": "itsellopt",
        "label": "ITsellopt",
        "url": "http://itsellopt.com.ua/price_lists/general_price_damskii_uk.xml",
        "type": "accessories",  # тільки аксесуари
    },
]

# ─── Хелпери ────────────────────────────────────────────────────────────────

def clean_html(text):
    """Видаляє HTML теги з опису"""
    if not text:
        return ""
    return re.sub(r'<[^>]+>', '', text).strip()

def apply_margin(price, margin):
    """Застосовує націнку і округлює до 10"""
    result = price * margin
    return round(result / 10) * 10

def supabase_upsert(table, records):
    """Записує/оновлює записи в Supabase"""
    if not records:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",  # upsert by primary key
    }
    # Відправляємо батчами по 500
    batch_size = 500
    total = 0
    for i in range(0, len(records), batch_size):
        batch = records[i:i+batch_size]
        resp = requests.post(url, headers=headers, json=batch)
        if resp.status_code not in (200, 201):
            print(f"  ⚠️  Supabase error {resp.status_code}: {resp.text[:200]}")
        else:
            total += len(batch)
    print(f"  ✅ Збережено {total} записів у таблицю '{table}'")

def fetch_xml(url, supplier_name):
    """Завантажує XML файл"""
    print(f"  📥 Завантаження XML від {supplier_name}...")
    try:
        resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return ET.fromstring(resp.content)
    except Exception as e:
        print(f"  ❌ Помилка завантаження: {e}")
        return None

def parse_offer(offer, supplier_name, supplier_label, categories_map, item_type, margin):
    """Парсить один offer з XML і повертає dict для Supabase"""
    offer_id    = offer.get("id", "")
    available   = offer.get("available", "true").lower() == "true"
    
    name        = offer.findtext("name_ua") or offer.findtext("name") or ""
    name        = re.sub(r'<[^>]+>', '', name).strip()
    vendor      = offer.findtext("vendor") or ""
    vendor_code = offer.findtext("vendorCode") or ""
    
    price_raw   = offer.findtext("price")
    price       = float(price_raw) if price_raw else 0
    price_with_margin = apply_margin(price, margin) if price > 0 else 0
    
    # Залишки
    qty_text    = offer.findtext("quantity_in_stock")
    stock       = int(qty_text) if qty_text and qty_text.isdigit() else (1 if available else 0)
    
    # Фото
    pictures    = [p.text for p in offer.findall("picture") if p.text]
    image       = pictures[0] if pictures else ""
    
    # Опис (беремо ua версію якщо є)
    desc_raw    = offer.findtext("description_ua") or offer.findtext("description") or ""
    description = clean_html(desc_raw)[:2000]  # обмежуємо до 2000 символів
    
    # Категорія
    cat_id      = offer.findtext("categoryId") or ""
    category    = categories_map.get(cat_id, "")

    # Стан товару (б/у визначаємо по категорії або назві)
    condition = "used"
    name_lower = name.lower()
    desc_lower = description.lower()
    if any(x in name_lower or x in desc_lower for x in ["вітрин", "витрин", "б/у", "used", "вживан", "відновлен"]):
        condition = "used"
    else:
        condition = "new"

    return {
        "id":           f"{supplier_name}_{offer_id}",
        "name":         name,
        "brand":        vendor,
        "price":        price_with_margin,
        "old_price":    price_with_margin,  # можна пізніше додати логіку знижок
        "stock":        stock,
        "available":    available and stock > 0,
        "image":        image,
        "images":       pictures[:5],       # максимум 5 фото
        "description":  description,
        "condition":    condition,
        "category":     category,
        "vendor_code":  vendor_code,
        "supplier":     supplier_label,     # ← ім'я постачальника для замовлень
        "supplier_price": price,            # ← закупівельна ціна (для тебе)
        "updated_at":   datetime.utcnow().isoformat(),
    }

def process_supplier(supplier):
    """Обробляє одного постачальника"""
    print(f"\n{'='*50}")
    print(f"🔄 Постачальник: {supplier['label']}")
    
    root = fetch_xml(supplier["url"], supplier["label"])
    if root is None:
        return

    # Будуємо map категорій id → назва
    categories_map = {}
    for cat in root.findall(".//category"):
        categories_map[cat.get("id", "")] = cat.text or ""

    offers = root.findall(".//offer")
    print(f"  📦 Знайдено товарів: {len(offers)}")

    phones_data = []
    accessories_data = []

    for offer in offers:
        available = offer.get("available", "true").lower() == "true"
        cat_id = offer.findtext("categoryId") or ""

        if supplier["type"] == "phones":
            rec = parse_offer(offer, supplier["name"], supplier["label"], categories_map, "phone", MARGIN_PHONES)
            phones_data.append(rec)

        elif supplier["type"] == "accessories":
            rec = parse_offer(offer, supplier["name"], supplier["label"], categories_map, "accessory", MARGIN_ACCESSORIES)
            accessories_data.append(rec)

        elif supplier["type"] == "mixed":
            phone_cat_ids = supplier.get("phone_category_ids", set())
            if cat_id in phone_cat_ids:
                rec = parse_offer(offer, supplier["name"], supplier["label"], categories_map, "phone", MARGIN_PHONES)
                phones_data.append(rec)
            else:
                rec = parse_offer(offer, supplier["name"], supplier["label"], categories_map, "accessory", MARGIN_ACCESSORIES)
                accessories_data.append(rec)

    print(f"  📱 Телефонів: {len(phones_data)}, 🔌 Аксесуарів: {len(accessories_data)}")

    if phones_data:
        supabase_upsert("products", phones_data)
    if accessories_data:
        supabase_upsert("accessories", accessories_data)

# ─── Головна функція ────────────────────────────────────────────────────────

def main():
    if not SUPABASE_KEY:
        print("❌ SUPABASE_KEY не встановлено! Додай його в GitHub Secrets.")
        return

    print(f"🚀 Старт синхронізації: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    for supplier in SUPPLIERS:
        process_supplier(supplier)

    print(f"\n✅ Синхронізація завершена: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

if __name__ == "__main__":
    main()
