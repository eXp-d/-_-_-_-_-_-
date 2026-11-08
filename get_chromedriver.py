"""
СКАЧИВАЕТ ChromeDriver под версию Chromium внутри Яндекс Браузера.

Зачем: Яндекс Браузер сам обновляется, а его собственный драйвер
(yandexdriver.exe) выходит с задержкой. Яндекс построен на Chromium,
поэтому подойдёт обычный chromedriver той же версии.

Как узнать версию: откройте в Яндекс Браузере  browser://version
и посмотрите User Agent: там написано Chrome/152.0.0.0 -> нужна версия 152.
(Первую строку "Yandex 26.x" не используйте - это версия самого Яндекса.)

Запуск:
  python get_chromedriver.py 152
  python get_chromedriver.py 152.0.7977        (если хотите точнее)

Результат: файл chromedriver.exe рядом со скриптами.
"""
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

KNOWN = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"
LAST = "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json"


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)


def win64_driver(downloads):
    for d in (downloads or {}).get("chromedriver", []):
        if d.get("platform") == "win64":
            return d["url"]
    return None


def find_driver(prefix):
    """Возвращает (версия, ссылка): сначала точное совпадение по началу версии, потом по номеру."""
    major = prefix.split(".")[0]
    exact = by_major = None
    for v in fetch_json(KNOWN)["versions"]:
        ver, url = v["version"], win64_driver(v.get("downloads"))
        if not url:
            continue
        if ver.startswith(prefix):
            exact = (ver, url)
        if ver.split(".")[0] == major:
            by_major = (ver, url)
    if exact or by_major:
        return exact or by_major
    # запасной вариант: свежие версии по каналам (Stable/Beta/Dev/Canary)
    for channel in fetch_json(LAST)["channels"].values():
        url = win64_driver(channel.get("downloads"))
        if url and channel["version"].split(".")[0] == major:
            return channel["version"], url
    return None


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else input("Версия Chromium (например 152): ").strip()
    found = find_driver(prefix)
    if not found:
        raise SystemExit(f"Драйвер для версии {prefix} не найден. Попробуйте позже или другую версию.")
    version, url = found
    print(f"Скачиваю chromedriver {version} ...")
    with urllib.request.urlopen(url, timeout=300) as r:
        blob = r.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.endswith("chromedriver.exe"))
        Path("chromedriver.exe").write_bytes(z.read(name))
    print("Готово: chromedriver.exe лежит в текущей папке.")
    print("Запуск: python scrape_elections.py --browser yandex --driver chromedriver.exe --limit 3")


if __name__ == "__main__":
    main()
