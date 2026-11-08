"""
СКРАПЕР ВЫБОРОВ: протоколы УИК по федеральному округу (партийные списки)
========================================================================
Сайт: izbirkom.ru, выборы в Госдуму (IX созыв, 2026).
Одна запись в csv = один УИК, строки протокола 1-22.

Как работает (по шагам):
  1. Открывает страницу любого УИК этих выборов (стартовая страница).
  2. На странице есть дерево комиссий: субъект (ИКСРФ) -> ОИК -> ТИК -> УИК.
     Скрапер раскрывает нужную ветку (параметр --path) кликами selenium.
  3. По очереди кликает по каждому УИК и ждёт, пока загрузится его протокол.
  4. Читает таблицу. ВАЖНО: часть чисел на сайте подменена - в ячейке лежит
     невидимый мусорный текст, а настоящее число стоит в другой ячейке
     поверх неё. Мы читаем то, что видит человек (по координатам).
  5. Проверяет контрольные суммы протокола и дописывает строку в csv.

Запуск (примеры):
  python scrape_elections.py --browser yandex --limit 3
  python scrape_elections.py --browser yandex --path "город Москва / Тушинский / Хорошево-Мневники"
Если прервать (Ctrl+C), уже собранное сохранится; при повторном запуске
уже собранные УИК пропускаются.
"""

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# ----------------------------- НАСТРОЙКИ -----------------------------
DEFAULT_START_URL = (
    "http://www.izbirkom.ru/election/587813923/commission/"
    "b5f93ec1-774e-43fd-b559-747ea4e83ca4/results?type=6&report=242"
)
REPORT_QS = "type=6&report=242"      # отчёт "Итоги по федеральному округу"
DEFAULT_PATH = "город Москва / Тушинский / Хорошево-Мневники"
N_LINES = 22                         # строки протокола 1..22
OUT_DIR = Path("output_elections")

YANDEX_PATHS = [
    r"C:\Program Files\Yandex\YandexBrowser\Application\browser.exe",
    r"C:\Program Files (x86)\Yandex\YandexBrowser\Application\browser.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe"),
]
YANDEX_BROWSER = next((p for p in YANDEX_PATHS if os.path.exists(p)), YANDEX_PATHS[0])
YANDEX_DRIVER = Path("yandexdriver.exe")

# --------------- JavaScript, который читает таблицу протокола ---------------
JS_READY = """
const t = document.querySelector('.v-result-table table');
const b = document.querySelector('.i-info-plate b');
const h = document.querySelector('h3.subheader');
if (!t || !b || t.rows.length < 15) return null;
return {name: b.textContent.trim(), report: h ? h.textContent.trim() : ''};
"""

JS_EXTRACT = """
const table = document.querySelector('.v-result-table table');
if (!table) return null;
const bodies = table.tBodies;
const main = bodies[0];
function hidden(cell) {
  const c = getComputedStyle(cell).color;
  return c === 'rgba(0, 0, 0, 0)' || c === 'transparent';
}
// наложенные ячейки с настоящими значениями (2-й tbody); ловушки (opacity 0) пропускаем
const overlays = [];
for (let i = 1; i < bodies.length; i++) {
  for (const td of bodies[i].querySelectorAll('td')) {
    const cs = getComputedStyle(td);
    if (cs.opacity === '0' || cs.fontSize === '0px') continue;
    const txt = td.textContent.trim();
    if (!txt) continue;
    const r = td.getBoundingClientRect();
    overlays.push({el: td, text: txt, top: Math.round(r.top), left: r.left});
  }
}
overlays.sort((a, b) => (a.top - b.top) || (a.left - b.left));
// способ 1: скрытые ячейки и наложенные идут в одном порядке - сопоставляем по номеру
const hiddenCells = Array.from(main.querySelectorAll('td')).filter(hidden);
const byOrder = new Map();
const orderOk = hiddenCells.length === overlays.length;
if (orderOk) hiddenCells.forEach((c, i) => byOrder.set(c, overlays[i].text));
// способ 2 (запасной): по координатам
function overlayFor(cell) {
  if (byOrder.has(cell)) return byOrder.get(cell);
  const b = cell.getBoundingClientRect();
  let best = null, bestD = 1e9;
  for (const o of overlays) {
    const r = o.el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x >= b.left - 1 && x <= b.right + 1 && y >= b.top - 1 && y <= b.bottom + 1) return o.text;
    const d = Math.abs(r.left - b.left) + Math.abs(r.top - b.top);
    if (d < bestD) { bestD = d; best = o.text; }
  }
  return bestD <= 12 ? best : null;
}
const rows = [];
for (const tr of main.rows) {
  if (tr.querySelector('th')) continue;
  const valueCell = tr.querySelector('td.v-result-table__value');
  const subCell = tr.querySelector('td.v-result-table__subvalue');
  const tds = Array.from(tr.cells);
  if (valueCell) {
    const spans = tr.querySelectorAll('td.v-result-table__rowspan');
    const h = hidden(valueCell);
    rows.push({kind: 'party', label: spans[1] ? spans[1].textContent.trim() : '',
               value: valueCell.textContent.trim(), hidden: h, overlay: h ? overlayFor(valueCell) : null});
  } else if (subCell) {
    continue;  // проценты нам не нужны
  } else if (tds.length >= 3) {
    const vc = tds[2];
    const h = hidden(vc);
    rows.push({kind: 'count', label: tds[1].textContent.replace(/\\s+/g, ' ').trim(),
               value: vc.textContent.trim(), hidden: h, overlay: h ? overlayFor(vc) : null});
  }
}
return {rows: rows, hiddenCount: hiddenCells.length, overlayCount: overlays.length, orderOk: orderOk};
"""


# ------------------------- ЧАСТЬ 1. БРАУЗЕР --------------------------
def make_driver(browser, driver_path=None):
    if browser == "yandex":
        exe = Path(driver_path) if driver_path else YANDEX_DRIVER
        if not exe.exists():
            raise SystemExit(f"Не найден драйвер {exe}")
        opts = webdriver.ChromeOptions()
        opts.binary_location = YANDEX_BROWSER
        driver = webdriver.Chrome(service=Service(str(exe)), options=opts)
    else:
        driver = webdriver.Chrome()
    try:
        driver.command_executor._client_config.timeout = 60
    except Exception:
        pass
    return driver


def js_click(driver, element):
    """Клик через JavaScript: работает даже если панель с деревом скрыта."""
    driver.execute_script("arguments[0].click();", element)


# --------------------- ЧАСТЬ 2. ХОД ПО ДЕРЕВУ КОМИССИЙ ----------------
def norm(text):
    """Приводит название к виду для сравнения (тире, пробелы, регистр)."""
    text = (text or "").lower().replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", text).strip()


def get_tree(driver, timeout=30):
    """Корень дерева комиссий (в боковой панели, где есть значки ИКСРФ/ОИК/ТИК/УИК)."""
    end = time.time() + timeout
    while True:
        for aside in driver.find_elements(By.CSS_SELECTOR, "aside.q-drawer"):
            if aside.find_elements(By.CSS_SELECTOR, ".i-election-badge"):
                trees = aside.find_elements(By.CSS_SELECTOR, ".q-tree")
                if trees:
                    return trees[0]
        if time.time() >= end:
            raise RuntimeError("Дерево комиссий не найдено на странице")
        time.sleep(0.5)


def child_nodes(parent):
    """Прямые дочерние узлы дерева (для корня или для узла)."""
    cls = parent.get_attribute("class") or ""
    if "q-tree__node" in cls:
        xpath = ("./div[contains(@class,'q-tree__node-collapsible')]"
                 "/div[contains(@class,'q-tree__children')]"
                 "/div[contains(@class,'q-tree__node')]")
    else:
        xpath = "./div[contains(@class,'q-tree__node')]"
    return parent.find_elements(By.XPATH, xpath)


def node_header(node):
    return node.find_element(By.XPATH, "./div[contains(@class,'q-tree__node-header')]")


def node_title(node):
    el = node_header(node).find_element(By.CSS_SELECTOR, ".title")
    return re.sub(r"\s+", " ", el.get_attribute("textContent") or "").strip()


def node_type(node):
    badges = node_header(node).find_elements(By.CSS_SELECTOR, ".i-election-badge")
    return (badges[0].get_attribute("textContent") or "").strip() if badges else ""


def expand(driver, node, timeout=25):
    """Раскрывает узел и ждёт, пока подгрузятся дочерние."""
    header = node_header(node)
    if header.get_attribute("aria-expanded") == "true" and child_nodes(node):
        return
    arrows = header.find_elements(By.CSS_SELECTOR, ".q-tree__arrow")
    if not arrows:
        return                                  # листовой узел (УИК)
    if header.get_attribute("aria-expanded") != "true":
        js_click(driver, arrows[0])
    end = time.time() + timeout
    while time.time() < end:
        if child_nodes(node):
            return
        time.sleep(0.3)


def pick(nodes, segment):
    """Выбирает узел по названию: сначала точное совпадение, потом по вхождению."""
    want = norm(segment)
    titles = [(n, norm(node_title(n))) for n in nodes]
    exact = [n for n, t in titles if t == want]
    if len(exact) == 1:
        return exact[0]
    part = [n for n, t in titles if want in t]
    if len(part) == 1:
        return part[0]
    names = [t for _, t in titles][:15]
    raise ValueError(f"Не удалось однозначно выбрать '{segment}'. Варианты: {names}")


def walk(driver, path):
    """Идёт по дереву по списку названий, раскрывая узлы. Возвращает последний узел."""
    parent, node = get_tree(driver), None
    for segment in path:
        if node is not None:
            expand(driver, node)
            parent = node
        node = pick(child_nodes(parent), segment)
    return node


def list_children(driver, path):
    """[(название, тип)] детей узла, заданного путём."""
    if path:
        node = walk(driver, path)
        expand(driver, node)
        kids = child_nodes(node)
    else:
        kids = child_nodes(get_tree(driver))
    return [(node_title(k), node_type(k)) for k in kids]


def iter_uik_paths(get_driver, path):
    """Генератор путей до всех УИК под узлом path (обход в глубину).
    get_driver() каждый раз отдаёт живой браузер (при сбое он перезапускается)."""
    for title, kind in list_children(get_driver(), path):
        sub = path + [title]
        if kind == "УИК":
            yield sub
        else:
            yield from iter_uik_paths(get_driver, sub)


# -------------------- ЧАСТЬ 3. ЧТЕНИЕ ПРОТОКОЛА УИК --------------------
def wait_table(driver, uik_title, timeout=30):
    """Ждёт, пока на странице появится протокол именно этого УИК."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            ready = driver.execute_script(JS_READY)
        except Exception:
            ready = None
        if (ready and norm(ready["name"]) == norm(uik_title)
                and "федеральн" in ready["report"].lower()):
            return True
        time.sleep(0.5)
    return False


DESCRIBE_JS = """
const t = document.querySelector('.v-result-table table');
const b = document.querySelector('.i-info-plate b');
const h = document.querySelector('h3.subheader');
const body = (document.body.innerText || '').split(String.fromCharCode(10)).join(' ').slice(0, 220);
const api = performance.getEntriesByType('resource')
  .filter(e => e.initiatorType === 'fetch' || e.initiatorType === 'xmlhttprequest')
  .slice(-5).map(e => (e.responseStatus || '?') + ' ' + e.name.split('?')[0].slice(-45)).join(' ; ');
return {table: !!t, rows: t ? t.rows.length : 0, name: b ? b.textContent.trim() : '',
        report: h ? h.textContent.trim() : '', text: body, api: api};
"""


def describe_page(driver):
    """Короткое описание того, что сейчас на странице (для понятных сообщений об ошибках)."""
    try:
        i = driver.execute_script(DESCRIBE_JS)
        return (f"адрес={driver.current_url} | таблица={i['table']} строк={i['rows']} | "
                f"УИК на странице='{i['name']}' | запросы: {i['api']} | текст: {i['text'][:90]}")
    except Exception as err:
        return f"не удалось прочитать страницу ({str(err).splitlines()[0]})"


def open_uik(driver, path, base):
    """Кликает по УИК в дереве и дожидается его протокола."""
    uik_title = path[-1]
    node = walk(driver, path)
    title_el = node_header(node).find_element(By.CSS_SELECTOR, ".title")
    before = driver.current_url
    js_click(driver, title_el)

    end = time.time() + 15
    while time.time() < end and driver.current_url == before:
        time.sleep(0.3)

    if wait_table(driver, uik_title):
        return
    # запасной путь: собрать адрес протокола из идентификатора комиссии
    m = re.search(r"/election/(\d+)/commission/([0-9a-f-]{36})", driver.current_url)
    if not m or driver.current_url == before:
        raise RuntimeError("после клика страница не переключилась на этот УИК | "
                           + describe_page(driver))
    driver.get(f"{base}/election/{m.group(1)}/commission/{m.group(2)}/results?{REPORT_QS}")
    if not wait_table(driver, uik_title):
        raise RuntimeError("протокол не загрузился | " + describe_page(driver))


def read_raw(driver):
    """Читает таблицу; если подмены не нашлись сразу - повторяет несколько раз."""
    raw = None
    for _ in range(4):
        raw = driver.execute_script(JS_EXTRACT)
        bad = [r for r in (raw or {}).get("rows", [])
               if r["hidden"] and r["overlay"] is None]
        if raw and not bad:
            return raw
        time.sleep(1)
    return raw


def scrape_protocol(driver, uik_title, debug_dir, dumps):
    """
    Читает протокол. Если что-то не прочиталось или контрольные суммы не сошлись,
    перезагружает страницу (до 2 раз): подмена значений на сайте случайная.
    Для неудачных УИК сохраняет данные для разбора в debug/.
    """
    best = None
    for attempt in range(3):
        raw = read_raw(driver)
        rec = build_record(raw)
        unread = sum(1 for i in range(1, N_LINES + 1) if rec[0].get(i) is None)
        if best is None or (rec[2], -unread) > (best[0][2], -best[1]):
            best = (rec, unread, raw)
        if rec[2] or attempt == 2:
            break
        driver.refresh()
        if not wait_table(driver, uik_title):
            break
        time.sleep(1)
    rec, unread, raw = best
    if not rec[2] and dumps["n"] < 3:
        dumps["n"] += 1
        debug_dir.mkdir(exist_ok=True)
        name = re.sub(r"\W+", "_", uik_title)
        (debug_dir / f"{name}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
        try:
            (debug_dir / f"{name}.html").write_text(driver.page_source, encoding="utf-8")
        except Exception:
            pass
    return rec


# ------------------- ЧАСТЬ 4. РАЗБОР ЗНАЧЕНИЙ И ПРОВЕРКИ -------------------
def to_int(text):
    """'1 299' -> 1299; мусор вроде 'и5я.ь78ы%' -> None."""
    if text is None:
        return None
    digits = re.fullmatch(r"\s*(\d[\d\s\u00a0]*)\s*", str(text))
    return int(re.sub(r"\D", "", digits.group(1))) if digits else None


def build_record(raw):
    """
    Из сырых строк делает словарь {номер строки: число} и проверяет протокол.
    Номера строк берём ПО ПОРЯДКУ (на сайте номера иногда испорчены).
    """
    values, labels, inferred = {}, {}, []
    line = 0
    for r in raw["rows"]:
        line += 1
        labels[line] = r["label"]
        text = r["overlay"] if r["hidden"] else r["value"]
        values[line] = to_int(text)

    # одно потерянное значение за партию можно восстановить по сумме
    party_lines = [i for i in range(13, min(line, N_LINES) + 1)]
    missing = [i for i in party_lines if values.get(i) is None]
    if len(missing) == 1 and values.get(10) is not None:
        known = sum(values[i] for i in party_lines if values.get(i) is not None)
        values[missing[0]] = values[10] - known
        inferred.append(missing[0])

    def get(i):
        return values.get(i)

    checks = []
    if all(get(i) is not None for i in party_lines) and get(10) is not None:
        checks.append(sum(get(i) for i in party_lines) == get(10))      # голоса = действительные
    if all(get(i) is not None for i in (7, 8, 9, 10)):
        checks.append(get(7) + get(8) == get(9) + get(10))              # ящики = голоса
    problems = []
    if all(get(i) is not None for i in party_lines) and get(10) is not None \
            and sum(get(i) for i in party_lines) != get(10):
        problems.append(f"сумма голосов за партии {sum(get(i) for i in party_lines)}"
                        f" != строка 10 ({get(10)})")
    if all(get(i) is not None for i in (7, 8, 9, 10)) and get(7) + get(8) != get(9) + get(10):
        problems.append(f"строки 7+8 ({get(7) + get(8)}) != строки 9+10 ({get(9) + get(10)})")
    unread = [i for i in range(1, N_LINES + 1) if values.get(i) is None]
    if unread:
        problems.append(f"не прочитаны строки: {unread}")
    ok = bool(checks) and all(checks) and not unread
    return values, labels, ok, inferred, problems


# ----------------------- ЧАСТЬ 5. ФЕДЕРАЛЬНЫЕ ОКРУГА -----------------------
FEDERAL_DISTRICTS = [
    ("ЦФО", ["белгородск", "брянск", "владимирск", "воронежск", "ивановск", "калужск",
             "костромск", "курск", "липецк", "московская", "орловск", "рязанск",
             "смоленск", "тамбовск", "тверск", "тульск", "ярославск", "город москва"]),
    ("СЗФО", ["карелия", "коми", "архангельск", "вологодск", "калининградск", "ленинградск",
              "мурманск", "новгородск", "псковск", "санкт-петербург", "ненецк"]),
    ("ЮФО", ["адыгея", "калмыкия", "крым", "краснодарск", "астраханск", "волгоградск",
             "ростовск", "севастопол", "донецк", "луганск", "запорожск", "херсонск", "сириус"]),
    ("СКФО", ["дагестан", "ингушет", "кабардино", "карачаево", "осети", "чечен", "ставропольск"]),
    ("ПФО", ["башкортостан", "марий", "мордов", "татарстан", "удмурт", "чуваш", "пермск",
             "кировск", "нижегородск", "оренбургск", "пензенск", "самарск", "саратовск",
             "ульяновск"]),
    ("УФО", ["курганск", "свердловск", "тюменск", "ханты", "ямало", "челябинск"]),
    ("СФО", ["республика алтай", "алтайский", "тыва", "хакасия", "красноярск", "иркутск",
             "кемеровск", "новосибирск", "омск", "томск"]),
    ("ДФО", ["бурятия", "саха", "якутия", "забайкальск", "камчатск", "приморск", "хабаровск",
             "амурск", "магаданск", "сахалинск", "еврейск", "чукотск"]),
]


def federal_district(region):
    name = norm(region)
    for district, keys in FEDERAL_DISTRICTS:
        if any(k in name for k in keys):
            return district
    return ""


# ------------------------------ ГЛАВНОЕ ------------------------------
def csv_header():
    return (["federal_district", "region", "oik", "tik", "uik_name", "uik_number", "url"]
            + [f"line_{i}" for i in range(1, N_LINES + 1)] + ["check_ok", "inferred_lines"])


def load_done(path):
    """Ключи уже собранных УИК (чтобы не собирать повторно)."""
    done = set()
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                done.add((row["region"], row["tik"], row["uik_name"]))
    return done


def alive(driver):
    """True, если браузер и его окно ещё живы."""
    try:
        driver.current_url
        driver.window_handles
        return True
    except Exception:
        return False


def start_browser(args):
    driver = make_driver(args.browser, args.driver)
    driver.get(args.start_url)
    get_tree(driver, 60)
    time.sleep(2)
    return driver


def recover(driver, args):
    """Мягкий сброс: перезагружаем стартовую страницу и ждём, пока вернётся дерево."""
    driver.get(args.start_url)
    get_tree(driver, 60)
    time.sleep(3)


def main():
    ap = argparse.ArgumentParser(description="Скрапер протоколов УИК (izbirkom.ru)")
    ap.add_argument("--browser", choices=["chrome", "yandex"], default="yandex")
    ap.add_argument("--driver", default=None,
                    help="путь к драйверу для Яндекса (по умолчанию yandexdriver.exe)")
    ap.add_argument("--start-url", default=DEFAULT_START_URL, help="адрес страницы любого УИК")
    ap.add_argument("--path", default=DEFAULT_PATH,
                    help='ветка дерева через " / ": субъект / ОИК / ТИК (ОИК может отсутствовать)')
    ap.add_argument("--limit", type=int, default=None, help="максимум УИК за запуск")
    ap.add_argument("--restart-every", type=int, default=5,
                    help="перезапускать браузер после стольких УИК (сайт ограничивает сессию)")
    ap.add_argument("--pause", type=float, default=4.0, help="пауза между УИК, сек")
    args = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    out_path = OUT_DIR / "uik_protocols.csv"
    done = load_done(out_path)
    parts = urlparse(args.start_url)
    base = f"{parts.scheme}://{parts.netloc}"
    path = [p.strip() for p in args.path.split("/") if p.strip()]

    state = {"driver": start_browser(args)}

    def get_driver():
        """Живой браузер; если окно закрылось - запускаем заново."""
        if not alive(state["driver"]):
            print("  (браузер закрылся, запускаю заново)")
            try:
                state["driver"].quit()
            except Exception:
                pass
            state["driver"] = start_browser(args)
        return state["driver"]

    def restart():
        """Полный перезапуск браузера: новая сессия, чистые cookie."""
        try:
            state["driver"].quit()
        except Exception:
            pass
        state["driver"] = start_browser(args)

    saved = 0
    since_restart = 0
    dumps = {"n": 0}
    try:
        new_file = not out_path.exists()
        with open(out_path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(csv_header())
            legend_written = (OUT_DIR / "legend.csv").exists()

            for uik_path in iter_uik_paths(get_driver, path):
                if args.limit and saved >= args.limit:
                    break
                region, uik_name = uik_path[0], uik_path[-1]
                tik = uik_path[-2] if len(uik_path) >= 2 else ""
                oik = " / ".join(uik_path[1:-2])
                if (region, tik, uik_name) in done:
                    continue
                if since_restart >= args.restart_every:
                    print("  (плановый перезапуск браузера)")
                    try:
                        restart()
                    except Exception as err:
                        print(f"  (не удалось перезапустить: {str(err).splitlines()[0]})")
                    since_restart = 0

                for attempt in (1, 2, 3):              # до трёх попыток при сбоях сайта
                    driver = get_driver()
                    try:
                        open_uik(driver, uik_path, base)
                        values, labels, ok, inferred, problems = scrape_protocol(
                            driver, uik_name, OUT_DIR / "debug", dumps)
                        number = re.search(r"№\s*([\w/-]+)", uik_name)
                        row = ([federal_district(region), region, oik, tik, uik_name,
                                number.group(1) if number else "", driver.current_url]
                               + [values[i] if values.get(i) is not None else ""
                                  for i in range(1, N_LINES + 1)]
                               + [ok, ";".join(map(str, inferred))])
                        writer.writerow(row)
                        f.flush()
                        saved += 1
                        since_restart += 1
                        flag = "OK" if ok else "ПРОВЕРЬТЕ"
                        note = (" (восстановлена строка " + ",".join(map(str, inferred)) + ")"
                                if inferred else "")
                        print(f"[{saved}] {region} / {tik} / {uik_name}: {flag}{note}")
                        for problem in problems:
                            print(f"      - {problem}")
                        if not legend_written:
                            with open(OUT_DIR / "legend.csv", "w", encoding="utf-8-sig",
                                      newline="") as lf:
                                lw = csv.writer(lf)
                                lw.writerow(["line", "label"])
                                for i in sorted(labels):
                                    lw.writerow([i, labels[i]])
                            legend_written = True
                        break
                    except Exception as err:
                        reason = str(err).splitlines()[0] if str(err) else type(err).__name__
                        short = reason if len(reason) <= 260 else reason[:260] + "..."
                        if attempt < 3:
                            wait = 15 if attempt == 1 else 20
                            how = "" if attempt == 1 else ", с перезапуском браузера"
                            print(f"      (сбой: {short}; пробую заново через {wait} с{how})")
                            time.sleep(wait)
                            try:
                                if attempt == 1:
                                    recover(get_driver(), args)
                                else:
                                    restart()
                                    since_restart = 0
                            except Exception:
                                pass
                            continue
                        print(f"{uik_name}: ОШИБКА - {short}")
                        dbg = OUT_DIR / "debug"
                        dbg.mkdir(exist_ok=True)
                        safe_name = re.sub(r"\W+", "_", uik_name)
                        try:
                            state["driver"].save_screenshot(str(dbg / (safe_name + ".png")))
                        except Exception:
                            pass
                time.sleep(args.pause)
    finally:
        try:
            state["driver"].quit()
        except Exception:
            pass
    print(f"\nГотово: добавлено {saved} записей -> {out_path}")


if __name__ == "__main__":
    main()
