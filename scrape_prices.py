"""
СБОР ЦЕН НА ПРОДУКТЫ (инфляция): selenium + beautifulsoup4 + re
================================================================

Что делает программа при каждом запуске (запускать раз в неделю):
  1. Читает список товаров из products.toml (категории -> группы -> ссылки).
  2. Открывает каждую ссылку в браузере Chrome (selenium).
  3. Достаёт цену и объём/вес из страницы (bs4 + регулярные выражения).
  4. Пересчитывает цену в рубли за 1 кг или 1 литр.
  5. Добавляет в таблицу output_prices/prices.csv ОДНУ новую строку:
        n, date, <цена группы 1>, <цена группы 2>, ..., basket_total, basket_ok
  6. Проверяет, что стоимость корзины = 1/4 МРОТ плюс-минус 5%.

Команды:
  python scrape_prices.py --setup   # один раз: выбрать город/магазин на сайте
  (добавьте --browser yandex, чтобы работать через Яндекс Браузер)
  python scrape_prices.py --visible # тестовый запуск с видимым браузером
  python scrape_prices.py           # обычный (рабочий) запуск без окна
"""

import argparse
import csv
import json
import os
import re
import time
import tomllib                    
from datetime import date, datetime
from pathlib import Path

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

# ----------------------------- НАСТРОЙКИ -----------------------------
CONFIG = Path("products.toml")
OUT_DIR = Path("output_prices")
PROFILE_BASE = "profile"               # тут браузер помнит выбранный магазин (profile_chrome, profile_yandex)
# Яндекс Браузер: путь к программе и к драйверу yandexdriver.exe (лежит в папке проекта)
YANDEX_PATHS = [
    r"C:\Program Files\Yandex\YandexBrowser\Application\browser.exe",
    r"C:\Program Files (x86)\Yandex\YandexBrowser\Application\browser.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe"),
]
# берём первый путь, по которому реально лежит браузер
YANDEX_BROWSER = next((p for p in YANDEX_PATHS if os.path.exists(p)), YANDEX_PATHS[0])
YANDEX_DRIVER = Path("yandexdriver.exe")
DEBUG_DIR = OUT_DIR / "debug"
PAUSE = 3.0                            # пауза между страницами (секунды)
START_URL = "https://5ka.ru/"

# единицы измерения: название -> (базовая единица, множитель до базовой)
UNITS = {
    "кг": ("кг", 1.0), "г": ("кг", 0.001), "гр": ("кг", 0.001),
    "л": ("л", 1.0), "мл": ("л", 0.001),
}


# ------------------------- ЧАСТЬ 1. КОНФИГ ---------------------------
def load_config(path):
    """Читает products.toml и возвращает (настройки, список товаров)."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    settings = cfg.pop("settings", {})
    items, seen = [], set()
    for category, groups in cfg.items():          # категория
        for group, info in groups.items():        # группа товаров
            if group in seen:
                raise ValueError(f"Название группы '{group}' повторяется")
            seen.add(group)
            items.append({"category": category, "group": group, **info})
    return settings, items


# ------------------------- ЧАСТЬ 2. БРАУЗЕР --------------------------
def make_driver(headless, browser="chrome"):
    """browser: 'chrome' или 'yandex'."""
    opts = Options()
    profile = Path(f"{PROFILE_BASE}_{browser}")
    opts.add_argument(f"--user-data-dir={profile.resolve()}")
    opts.add_argument("--window-size=1366,900")
    opts.add_argument("--lang=ru-RU")
    opts.add_argument("--log-level=3")
    if headless:
        opts.add_argument("--headless=new")
    if browser == "yandex":
        if not YANDEX_DRIVER.exists():
            raise SystemExit("Не найден yandexdriver.exe в папке проекта (см. инструкцию)")
        opts.binary_location = YANDEX_BROWSER      # запускаем Яндекс Браузер
        return webdriver.Chrome(service=Service(str(YANDEX_DRIVER)), options=opts)
    return webdriver.Chrome(options=opts)


def get_page(driver, url):
    """Открывает страницу и ждёт, пока на ней появятся цены."""
    driver.get(url)
    try:
        WebDriverWait(driver, 20).until(
            lambda d: "₽" in d.page_source or "application/ld+json" in d.page_source
        )
    except Exception:
        pass
    time.sleep(PAUSE)
    return driver.page_source


# --------------------- ЧАСТЬ 3. ПОИСК ЦЕНЫ В HTML --------------------
def find_key(obj, key):
    """Ищет значения по ключу во вложенных словарях/списках (для JSON-LD)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_key(v, key)


def to_price(text):
    """'1 299,90' -> 1299.9"""
    try:
        return float(str(text).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def find_text_prices(chunk):
    """Находит цены в куске текста регулярными выражениями."""
    found = []
    # формат "131 59 ₽": рубли и копейки через пробел
    for m in re.finditer(r"(\d{1,3}(?: \d{3})*) (\d{2}) ?₽", chunk):
        found.append(to_price(m.group(1)) + int(m.group(2)) / 100)
    # формат "89,99 ₽" или "89.99 руб"
    if not found:
        for m in re.finditer(r"(\d{1,3}(?: \d{3})*[.,]\d{2}) ?(?:₽|руб)", chunk):
            found.append(to_price(m.group(1)))
    return found


def extract_prices(html):
    """
    Возвращает (название, [цены-кандидаты], откуда_взяли).
    Ищем тремя способами - от самого надёжного к запасному:
      1) JSON-LD (скрытые данные для поисковиков),
      2) meta-теги,
      3) регулярное выражение по видимому тексту ("131 59 ₽" или "89,99 руб").
    """
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")

    # 1) JSON-LD
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for value in find_key(data, "price"):
            p = to_price(value)
            if p:
                found.append(p)
    if found:
        return title, found, "json-ld"

    # 2) meta-теги
    for attrs in ({"itemprop": "price"}, {"property": "product:price:amount"},
                  {"property": "og:price:amount"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content") and to_price(tag["content"]):
            return title, [to_price(tag["content"])], "meta"

    # 3) видимый текст
    text = re.sub(r"\s+", " ", soup.get_text(" "))
    # Цена основного товара стоит рядом с заголовком. Сначала ищем в куске текста
    # сразу после заголовка (800 символов), чтобы не схватить цены из блока
    # "Похожие товары". Если там пусто - ищем по всей странице.
    title_norm = re.sub(r"\s+", " ", title)
    pos = text.find(title_norm) if title_norm else -1
    chunks = [text[pos: pos + 800]] if pos >= 0 else []
    chunks.append(text)
    found = []
    for chunk in chunks:
        found = find_text_prices(chunk)
        if found:
            break
    return title, found, "text"


def pick_price(candidates, mode):
    """Если цен несколько (обычная и по карте), выбираем по правилу из настроек."""
    if mode == "min":
        return min(candidates)
    if mode == "max":
        return max(candidates)
    return candidates[0]          # "first" - первая найденная на странице


# ----------------- ЧАСТЬ 4. ВЕС/ОБЪЁМ И ЦЕНА ЗА КГ/ЛИТР -------------
def get_amount(item, title):
    """
    Возвращает (количество в кг или л, 'кг'/'л').
    Берём из products.toml (amount + unit), а если там нет - из названия товара,
    например 'Молоко ... 1700мл' -> 1.7 л.
    """
    if "amount" in item and "unit" in item:
        amount, unit = float(item["amount"]), item["unit"].lower()
    else:
        found = re.findall(r"(\d+(?:[.,]\d+)?)\s*(кг|гр|г|мл|л)\b", title.lower())
        if not found:
            raise ValueError("не нашёл вес/объём в названии, укажите amount и unit в products.toml")
        raw, unit = found[-1]               # вес обычно в конце названия
        amount = float(raw.replace(",", "."))
    if unit not in UNITS:
        raise ValueError(f"единица '{unit}' не поддерживается (кг, г, л, мл)")
    base, factor = UNITS[unit]
    return amount * factor, base


def scrape_item(driver, item, mode):
    html = get_page(driver, item["url"])
    title, candidates, source = extract_prices(html)
    if not candidates:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / f"{item['group']}.html").write_text(html, encoding="utf-8")
        raise ValueError(f"цена не найдена, HTML сохранён в {DEBUG_DIR}/")
    price = pick_price(candidates, mode)
    amount, base = get_amount(item, title)
    return {
        "group": item["group"],
        "title": title,
        "price_shelf": price,
        "candidates": candidates,
        "source": source,
        "amount": amount,
        "base_unit": base,
        "price_per_unit": round(price / amount, 2),     # руб за 1 кг или 1 л
    }


# ----------------------- ЧАСТЬ 5. ТАБЛИЦА ЦЕН ------------------------
def append_row(columns, row):
    """Добавляет строку в prices.csv. Если набор товаров изменился - делает копию старого файла."""
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / "prices.csv"
    header = ["n", "date"] + columns + ["basket_total", "basket_ok"]

    n = 1
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        if rows and rows[0] != header:
            backup = OUT_DIR / f"prices_old_{datetime.now():%Y%m%d_%H%M%S}.csv"
            path.rename(backup)
            print(f"Набор товаров изменился, старый файл сохранён как {backup.name}")
        else:
            n = len(rows)             # строки с данными = len(rows)-1, новая = +1

    new_file = not path.exists()
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(header)
        w.writerow([n, date.today().isoformat()] + row)
    return n


# ------------------------------ ГЛАВНОЕ ------------------------------
def main():
    ap = argparse.ArgumentParser(description="Недельный сбор цен на продукты")
    ap.add_argument("--setup", action="store_true",
                    help="открыть сайт, чтобы один раз выбрать город и магазин")
    ap.add_argument("--visible", action="store_true", help="показывать окно браузера")
    ap.add_argument("--browser", choices=["chrome", "yandex"], default="chrome",
                    help="каким браузером собирать цены")
    ap.add_argument("--config", default=str(CONFIG), help="файл со списком товаров")
    args = ap.parse_args()

    if args.setup:
        driver = make_driver(headless=False, browser=args.browser)
        driver.get(START_URL)
        input("Выберите город/магазин на сайте, затем нажмите Enter здесь...")
        driver.quit()
        print(f"Готово: выбор сохранён в папке {PROFILE_BASE}_{args.browser}/")
        return

    settings, items = load_config(args.config)
    mode = settings.get("price_mode", "first")

    # лимит корзины: 1/4 МРОТ +-5%
    limit = settings.get("mrot_rub", 27093) * settings.get("basket_share", 0.25)
    tol = settings.get("tolerance", 0.05)
    lo, hi = limit * (1 - tol), limit * (1 + tol)
    print(f"Лимит корзины: {limit:.0f} ₽ (допустимо {lo:.0f}-{hi:.0f} ₽)")

    driver = make_driver(headless=not args.visible, browser=args.browser)
    results = {}
    try:
        for i, item in enumerate(items, 1):
            try:
                res = scrape_item(driver, item, mode)
                results[item["group"]] = res
                print(f"[{i}/{len(items)}] {item['group']}: {res['price_per_unit']} ₽/{res['base_unit']}"
                      f"  (полка {res['price_shelf']} ₽, {res['amount']:.3g} {res['base_unit']},"
                      f" источник {res['source']}, кандидаты {res['candidates'][:3]})")
            except Exception as err:
                print(f"[{i}/{len(items)}] {item['group']}: ОШИБКА - {err}")
    finally:
        driver.quit()

    # строка таблицы: цена за кг/л по каждой группе
    columns = [it["group"] for it in items]
    row, total, complete = [], 0.0, True
    for it in items:
        res = results.get(it["group"])
        if res is None:
            row.append("")
            complete = False
        else:
            row.append(res["price_per_unit"])
            total += res["price_per_unit"] * float(it.get("qty", 1))   # qty - кг/л в корзине

    basket_total = round(total, 2) if complete else ""
    basket_ok = (lo <= total <= hi) if complete else ""
    n = append_row(columns, row + [basket_total, basket_ok])

    # подробности последнего запуска - для проверки
    (OUT_DIR / "last_run.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nЗапись n={n} добавлена в {OUT_DIR / 'prices.csv'}")
    if not complete:
        print("ВНИМАНИЕ: часть цен не собрана, стоимость корзины не посчитана")
    else:
        print(f"Корзина: {total:.2f} ₽ -> {'В ЛИМИТЕ' if basket_ok else 'ВНЕ ЛИМИТА, измените qty в products.toml'}")


if __name__ == "__main__":
    main()
