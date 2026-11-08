"""
ВСПОМОГАТЕЛЬНЫЙ СКРИПТ: сохраняет страницы сайта Избиркома на диск.

Зачем: чтобы написать точный скрапер, нужно увидеть настоящий HTML-код страниц.
Скрипт открывает обычное окно браузера, вы САМИ кликаете по сайту, а на нужной
странице нажимаете Enter в консоли - страница сохраняется в папку saved_pages/.

Запуск:
  pip install selenium
  python save_pages.py            (Chrome)
  python save_pages.py --yandex   (Яндекс Браузер)
"""
import os
import re
import sys
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

START_URL = "http://www.izbirkom.ru/region/izbirkom"
OUT = Path("saved_pages")

# Яндекс Браузер: берём первый путь, по которому он реально лежит
YANDEX_PATHS = [
    r"C:\Program Files\Yandex\YandexBrowser\Application\browser.exe",
    r"C:\Program Files (x86)\Yandex\YandexBrowser\Application\browser.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe"),
]
YANDEX_BROWSER = next((p for p in YANDEX_PATHS if os.path.exists(p)), YANDEX_PATHS[0])
YANDEX_DRIVER = Path("yandexdriver.exe")


def make_driver():
    if "--yandex" in sys.argv:
        opts = webdriver.ChromeOptions()
        opts.binary_location = YANDEX_BROWSER
        return webdriver.Chrome(service=Service(str(YANDEX_DRIVER)), options=opts)
    return webdriver.Chrome()


def read_html(driver):
    """HTML текущего документа. Пробуем два способа, при неудаче вернём None."""
    try:
        html = driver.page_source
        if html:
            return html
    except Exception:
        pass
    try:
        return driver.execute_script(
            "return document.documentElement ? document.documentElement.outerHTML : null")
    except Exception:
        return None


def save_current(driver, name):
    """Сохраняет текущую страницу (и все её фреймы). Ничего не роняет."""
    # если сайт открыл новую вкладку, работаем с последней
    try:
        driver.switch_to.window(driver.window_handles[-1])
        driver.switch_to.default_content()
    except Exception as err:
        print(f"  (не удалось переключить вкладку: {str(err).splitlines()[0]})")

    saved = 0
    html = read_html(driver)
    if html:
        (OUT / f"{name}.html").write_text(html, encoding="utf-8")
        saved += 1

    # страницы на фреймах: сохраняем каждый фрейм отдельным файлом
    try:
        frames = driver.find_elements(By.CSS_SELECTOR, "frame, iframe")
    except Exception:
        frames = []
    for i in range(len(frames)):
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(i)
            fh = read_html(driver)
            if fh:
                (OUT / f"{name}_frame{i + 1}.html").write_text(fh, encoding="utf-8")
                saved += 1
        except Exception as err:
            print(f"  (фрейм {i + 1} не сохранился: {str(err).splitlines()[0]})")
        finally:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    try:
        driver.save_screenshot(str(OUT / f"{name}.png"))
    except Exception:
        pass
    try:
        (OUT / f"{name}.url.txt").write_text(driver.current_url, encoding="utf-8")
        print(f"  адрес: {driver.current_url}")
    except Exception:
        pass

    if saved == 0:
        print("  ПУСТО: страница ещё грузится или не содержит HTML. "
              "Дождитесь загрузки и нажмите Enter снова.")
    else:
        tables = len(re.findall(r"<table", html or "", flags=re.I))
        print(f"  сохранено файлов: {saved} | таблиц на странице: {tables} | фреймов: {len(frames)}")


def main():
    OUT.mkdir(exist_ok=True)
    driver = make_driver()
    try:
        driver.get(START_URL)
    except Exception as err:
        # не падаем: браузер остаётся открытым, адрес можно ввести вручную
        print(f"Не удалось открыть {START_URL}: {str(err).splitlines()[0]}")
        print("Введите адрес сайта Избиркома вручную в адресной строке браузера.")

    n = 0
    print("Откройте в браузере нужную страницу (кликайте по сайту сами).")
    print("Дождитесь, пока страница полностью загрузится, и только потом жмите Enter.")
    try:
        while True:
            cmd = input("Enter = сохранить текущую страницу, q = выйти: ").strip().lower()
            if cmd == "q":
                break
            n += 1
            time.sleep(1)
            save_current(driver, f"{n:02d}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
