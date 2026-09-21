"""
Сквозная проверка: настоящий Electron + настоящий Python-движок.

    xvfb-run -a python test/e2e_electron.py        (Linux, нужен playwright)

Запускает приложение с --remote-debugging-port, подключается к окну
и проходит весь сценарий: добавить файлы -> анализ -> экспорт YARA.
Диалоги выбора файла подменены в test/e2e_main.js.
"""

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ELECTRON = os.path.join(ROOT, "node_modules", ".bin", "electron")
PORT = 9333

COMMON = "Xk9$Qz!vLm2"
STABLE = "aZ9#qP!x7Lw"
WIDE = "wQ7!zL#p3Mx"


def dump(parts):
    data = b"\0" * 64
    for kind, value in parts:
        data += (value.encode("utf-16le") if kind == "w" else value.encode()) + b"\0" * 64
    return data


work = tempfile.mkdtemp(prefix="e2e ")
files = {
    "clean1.dmp": [("a", COMMON)],
    "clean2.dmp": [("a", COMMON)],
    "cheat1.dmp": [("a", COMMON), ("a", STABLE), ("w", WIDE)],
    "cheat2.dmp": [("a", COMMON), ("a", STABLE), ("w", WIDE)],
    "cheat3.dmp": [("a", COMMON), ("a", STABLE), ("w", WIDE)],
}
for name, parts in files.items():
    with open(os.path.join(work, name), "wb") as f:
        f.write(dump(parts))

env = dict(os.environ, E2E_DIR=work)
app = subprocess.Popen(
    [ELECTRON, "--no-sandbox", f"--remote-debugging-port={PORT}", os.path.join(ROOT, "test", "e2e_main.js")],
    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)

problems = []

try:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise RuntimeError("Electron не поднял порт отладки")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        page = None
        for _ in range(50):
            pages = [pg for ctx in browser.contexts for pg in ctx.pages if "index.html" in pg.url]
            if pages:
                page = pages[0]
                break
            time.sleep(0.2)
        assert page, "окно приложения не найдено"

        page.wait_for_selector('#pyStatus:not([data-state="wait"])')
        assert page.locator("#pyStatus").get_attribute("data-state") == "ok", "Python не найден приложением"
        print("Python:", page.locator("#pyStatus").text_content())

        # Изоляция: у страницы нет Node.js, есть только наш мост.
        isolation = page.evaluate(
            "({req: typeof require, proc: typeof process, api: Object.keys(window.api).sort()})"
        )
        assert isolation["req"] == "undefined" and isolation["proc"] == "undefined", isolation
        assert isolation["api"] == sorted([
            "checkPython", "pickFiles", "pathForFile", "runEngine",
            "cancelEngine", "exportItems", "onEngineEvent",
        ]), isolation
        print("Изоляция: require/process недоступны, мост:", isolation["api"])

        # Шрифты из папки приложения загружаются при открытии страницы как file:// под строгой CSP.
        fonts = page.evaluate("""async () => {
          await document.fonts.ready;
          return [...new Set(Array.from(document.fonts).filter(f => f.status === 'loaded').map(f => f.family.replace(/"/g, '')))];
        }""")
        assert "Golos Text" in fonts and "JetBrains Mono" in fonts, fonts
        print("Шрифты загружены:", sorted(fonts))

        page.click('button[data-action="add"][data-group="clean"]')
        page.click('button[data-action="add"][data-group="cheat"]')
        assert page.locator('.filelist[data-group="clean"] li').count() == 2
        assert page.locator('.filelist[data-group="cheat"] li').count() == 3

        page.click("#runBtn")
        page.wait_for_selector("#viewResults:not([hidden])", timeout=60000)
        assert page.locator("#percent").text_content() == "100%"

        found = page.locator("#rows tr code").all_text_contents()
        assert sorted(found) == sorted([STABLE, WIDE]), found
        badges = dict(zip(found, page.locator("#rows .badge").all_text_contents()))
        assert badges[STABLE] == "ASCII" and badges[WIDE] == "UTF-16", badges
        print("Результат:", badges)

        # Экспорт YARA через настоящий IPC и запись файла главным процессом.
        page.fill("#ruleName", "vape_strings")
        page.fill("#minMatches", "2")
        page.click("#exportYara")
        page.wait_for_selector('#status[data-kind="ok"]')

        rule_path = os.path.join(work, "vape_strings.yar")
        assert os.path.isfile(rule_path), "главный процесс не сохранил .yar"
        print(open(rule_path, encoding="utf-8").read())

        page.click("#exportTxt")
        page.wait_for_selector('#status[data-kind="ok"]')
        txt = open(os.path.join(work, "candidates.txt"), encoding="utf-8").read().split()
        assert sorted(txt) == sorted([STABLE, WIDE]), txt

        # Повторный запуск с ошибкой: убираем файл с диска, сервер должен ответить понятным текстом.
        os.remove(os.path.join(work, "cheat3.dmp"))
        page.click("#runBtn")
        page.wait_for_selector('#status[data-kind="error"]')
        message = page.locator("#status").text_content()
        assert "не найден" in message, message
        print("Ошибка валидации показана:", message[:80])

        browser.close()

    try:
        import yara

        rules = yara.compile(filepath=rule_path)
        hits = {n: [m.rule for m in rules.match(os.path.join(work, n))] for n in files if os.path.exists(os.path.join(work, n))}
        assert hits["clean1.dmp"] == [] and hits["cheat1.dmp"] == ["vape_strings"], hits
        print("YARA-правило проверено реальным yara-python:", hits)
    except ImportError:
        print("yara-python не установлен: проверка правила пропущена")

except Exception as error:
    problems.append(repr(error))
finally:
    app.terminate()
    try:
        output, _ = app.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        app.kill()
        output, _ = app.communicate()
    shutil.rmtree(work, ignore_errors=True)

interesting = [
    line for line in (output or "").splitlines()
    if any(word in line for word in ("Error", "error", "Refused", "Uncaught", "CSP"))
    and "dbus" not in line.lower() and "gpu" not in line.lower()
]

if interesting:
    print("Вывод Electron (стоит посмотреть):")
    for line in interesting[:15]:
        print("  ", line)

if problems:
    print("ПРОВАЛ:", problems)
    sys.exit(1)

print("Сквозная проверка пройдена.")
