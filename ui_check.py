"""
Проверка интерфейса в настоящем Chromium без Electron.

    python test/ui_check.py

Вместо window.api подставляется заглушка, которая имитирует главный процесс:
она отдаёт пути файлов и «проигрывает» события движка.
"""

import json
import os
import random
import string
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = "file://" + os.path.join(ROOT, "renderer", "index.html")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ui"
os.makedirs(OUT, exist_ok=True)

random.seed(7)
alphabet = string.ascii_letters + string.digits + "!@#$%^&*()_+-="

items = [
    # Нагрузка, которая была бы опасна при innerHTML.
    {"s": "<img src=x onerror=window.__pwned=1>", "e": 1, "h": 3, "n": 4.1},
    {"s": "<script>window.__pwned=2</script>", "e": 2, "h": 3, "n": 4.3},
]

for _ in range(1300):
    length = random.randint(8, 40)
    items.append({
        "s": "".join(random.choice(alphabet) for _ in range(length)),
        "e": random.choice([1, 1, 1, 2, 3]),
        "h": random.choice([3, 3, 2, 1]),
        "n": round(random.uniform(2.7, 5.4), 3),
    })

items.sort(key=lambda r: (-r["h"], -len(r["s"]), r["s"]))

MOCK = """
(() => {
  const ITEMS = %s;
  let listener = null;
  window.__events = [];
  window.__export = null;
  window.__cancelled = false;

  const emit = (evt) => setTimeout(() => listener && listener(evt), 0);

  const apiObject = {
    checkPython: async () => ({ ok: true, version: "3.12.3", command: "python" }),
    pickFiles: async (group) => group === "clean"
      ? ["C:\\\\dumps\\\\clean_run1.dmp", "C:\\\\dumps\\\\clean_run2.dmp"]
      : ["C:\\\\dumps\\\\cheat_vape_1.dmp", "C:\\\\dumps\\\\cheat_vape_2.dmp", "C:\\\\dumps\\\\cheat_vape_3.dmp"],
    pathForFile: (f) => "C:\\\\dropped\\\\" + f.name,
    onEngineEvent: (cb) => { listener = cb; return () => { listener = null; }; },
    cancelEngine: async () => { window.__cancelled = true; emit({ type: "finished", cancelled: true }); return { ok: true }; },
    exportItems: async (payload) => { window.__export = payload; return { ok: true, path: "C:\\\\out\\\\cheat_strings.yar", skipped: 0 }; },
    runEngine: async (req) => {
      window.__request = req;
      emit({ type: "start", clean: req.clean.length, cheat: req.cheat.length });
      emit({ type: "log", line: "[INFO] CLEAN 1/2: C:\\\\dumps\\\\clean_run1.dmp -> 812 кандидатов" });
      emit({ type: "file_done", group: "clean", index: 1, total: 2, path: "a", candidates: 812 });
      emit({ type: "file_progress", group: "clean", index: 2, total: 2, path: "C:\\\\dumps\\\\clean_run2.dmp", fraction: 0.5 });
      window.__resume = () => {
        emit({ type: "file_done", group: "clean", index: 2, total: 2, path: "b", candidates: 790 });
        for (let i = 1; i <= 3; i++) emit({ type: "file_done", group: "cheat", index: i, total: 3, path: "c", candidates: 900 });
        emit({ type: "result", items: ITEMS, total: ITEMS.length + 4200, truncated: true,
               clean_counts: [812, 790], cheat_counts: [900, 900, 900], min_cheat_hits: 3, elapsed: 41.27 });
        emit({ type: "finished", cancelled: false });
      };
      return { ok: true };
    },
  };

  // Как contextBridge: свойство неизменяемое, поэтому `const api = ...` в app.js упало бы.
  Object.defineProperty(window, "api", { value: apiObject, writable: false, configurable: false });
})();
""" % json.dumps(items)

problems = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1240, "height": 820})

    console = []
    page.on("console", lambda m: console.append((m.type, m.text)))
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

    page.add_init_script(MOCK)
    page.goto(PAGE)
    page.wait_for_selector("#pyStatus[data-state=ok]")

    # Шрифты лежат в папке приложения и должны реально загрузиться (font-src 'self').
    loaded = page.evaluate("""async () => {
      await document.fonts.ready;
      return [...new Set(Array.from(document.fonts).filter(f => f.status === 'loaded').map(f => f.family.replace(/"/g, '')))];
    }""")
    assert "Golos Text" in loaded and "JetBrains Mono" in loaded, f"шрифты не загрузились: {loaded}"

    # 1. Начальное состояние.
    assert page.locator("#runBtn").is_disabled(), "кнопка запуска должна быть выключена без файлов"
    assert page.locator("#viewEmpty").is_visible() and page.locator("#viewResults").is_hidden()
    assert "Добавьте ещё clean-дампов: 2" in page.locator("#runHint").text_content()
    assert page.locator("#count-clean").text_content() == "0 из 2"
    page.screenshot(path=f"{OUT}/1_empty.png")

    # 2. Добавляем файлы через кнопки и через drag-and-drop.
    page.click('button[data-action="add"][data-group="clean"]')
    assert page.locator('.filelist[data-group="clean"] li').count() == 2
    assert page.locator("#runBtn").is_disabled(), "cheat-дампов ещё нет"
    page.click('button[data-action="add"][data-group="cheat"]')
    assert page.locator('.filelist[data-group="cheat"] li').count() == 3
    assert page.locator("#runBtn").is_enabled(), "теперь запуск должен быть доступен"

    # Удаление одного файла и возврат в исходное состояние.
    page.click('.filelist[data-group="cheat"] li:first-child button')
    assert page.locator('.filelist[data-group="cheat"] li').count() == 2
    page.click('button[data-action="add"][data-group="cheat"]')  # добавит недостающий
    assert page.locator('.filelist[data-group="cheat"] li').count() == 3

    # Файл из одного списка нельзя добавить в другой.
    page.evaluate("""() => {
      const zone = document.querySelector('.dropzone[data-group="cheat"]');
      const dt = new DataTransfer();
      dt.items.add(new File(['x'], 'sample.dmp'));
      zone.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
    }""")
    assert page.locator('.filelist[data-group="cheat"] li').count() == 4, "drag-and-drop должен добавить файл"
    page.click('.filelist[data-group="cheat"] li:last-child button')
    assert page.locator('.filelist[data-group="cheat"] li').count() == 3

    page.screenshot(path=f"{OUT}/2_files.png")

    # 3. Запуск: прогресс.
    page.click(".params summary")  # блок параметров свёрнут по умолчанию
    page.fill("#minHits", "2")
    assert "в 2 дампах" in page.locator("#paramsSummary").text_content()
    page.click("#runBtn")
    page.wait_for_selector("#cancelBtn:not([hidden])")
    for _ in range(50):  # опрос вместо wait_for_function: CSP запрещает eval
        if page.locator("#percent").text_content() != "0%":
            break
        page.wait_for_timeout(100)
    assert page.locator("#percent").text_content() != "0%", "прогресс не сдвинулся"
    request = page.evaluate("window.__request")
    assert request["minHits"] == 2 and request["minLen"] == 8 and request["minEntropy"] == 2.6
    assert len(request["clean"]) == 2 and len(request["cheat"]) == 3
    assert page.locator("#minHits").is_disabled(), "параметры блокируются на время анализа"
    assert page.locator("#viewRunning").is_visible(), "во время анализа справа экран прогресса"
    assert page.locator("#steps span").count() == 5, "по сегменту на каждый из 5 дампов"
    assert page.locator("#steps .done").count() == 1 and page.locator("#steps .now").count() == 1
    page.click(".params summary")  # сворачиваем обратно, как на макете
    page.screenshot(path=f"{OUT}/3_running.png")

    # 4. Результат.
    page.evaluate("window.__resume()")
    page.wait_for_selector("#viewResults:not([hidden])")
    assert page.locator("#percent").text_content() == "100%"
    assert page.locator("#runBtn").is_visible() and page.locator("#cancelBtn").is_hidden()

    rows = page.locator("#rows tr").count()
    assert rows == 500, f"должно быть 500 строк на странице, а не {rows}"
    assert page.locator("#moreBtn").is_visible()

    # Безопасность: HTML из дампа не должен исполняться и не должен создавать элементы.
    assert page.evaluate("window.__pwned") is None, "XSS: код из строки дампа выполнился"
    assert page.locator("#rows img, #rows script").count() == 0, "XSS: в таблице появились теги"
    page.fill("#search", "onerror")
    assert page.locator("#rows tr").count() == 1
    shown = page.locator("#rows tr code").text_content()
    assert shown == "<img src=x onerror=window.__pwned=1>", "строка должна отображаться как текст"
    page.screenshot(path=f"{OUT}/4_xss_row.png")
    page.fill("#search", "")

    # Пагинация.
    page.click("#moreBtn")
    assert page.locator("#rows tr").count() == 1000
    page.click("#moreBtn")
    assert page.locator("#rows tr").count() == 1302 and page.locator("#moreBtn").is_hidden()

    # Фильтры и сортировка.
    page.click('.seg button[data-enc="utf16"]')
    n_utf = page.locator("#rows tr").count()
    assert 0 < n_utf < 1302
    assert all(t in ("UTF-16", "ASCII + UTF-16") for t in page.locator("#rows .badge").all_text_contents())
    assert page.locator('.seg button[data-enc="utf16"]').get_attribute("aria-pressed") == "true"
    page.click('.seg button[data-enc=""]')
    assert page.locator("#rows tr").count() == 500
    page.select_option("#sortBy", "length")
    lens = [int(x) for x in page.locator("#rows tr td:nth-child(4)").all_text_contents()[:50]]
    assert lens == sorted(lens, reverse=True), "сортировка по длине"

    # Отметки и экспорт.
    page.uncheck('#rows tr:first-child input[type=checkbox]')
    selected = page.locator("#statSelected").text_content().replace("\u00a0", " ").replace("\u202f", " ")
    assert selected == "1 301", selected
    assert page.locator("#rows tr:first-child").get_attribute("class") == "off", "снятая строка тускнеет"
    assert "лучшие 1 302 из 5 502" in page.locator("#truncNote").text_content().replace("\u00a0", " ").replace("\u202f", " ")

    page.click("#uncheckView")
    page.click("#exportYara")
    page.wait_for_selector('#status[data-kind="warn"]')
    assert "хотя бы одну" in page.locator("#status").text_content()

    page.click("#checkView")
    page.fill("#ruleName", "vape_strings")
    page.fill("#minMatches", "5")
    page.click("#exportYara")
    page.wait_for_selector('#status[data-kind="ok"]')
    export = page.evaluate("window.__export")
    assert export["kind"] == "yara" and export["ruleName"] == "vape_strings" and export["minMatches"] == 5
    assert len(export["items"]) == 1302
    assert "cheat_strings.yar" in page.locator("#status").text_content()

    page.click("#exportTxt")
    assert page.evaluate("window.__export")["kind"] == "txt"

    # Всё должно помещаться в одно окно без прокрутки страницы.
    def inside(selector, width, height):
        box = page.locator(selector).bounding_box()
        assert box, selector
        assert box["y"] >= 0 and box["y"] + box["height"] <= height + 1, f"{selector} выходит за окно по высоте"
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1, f"{selector} выходит за окно по ширине"

    for sel in ("#exportYara", "#exportTxt", "#search", "#steps", ".brand"):
        inside(sel, 1240, 820)
    page.screenshot(path=f"{OUT}/5_results.png")

    # 5. Отмена.
    page.click("#runBtn")
    page.wait_for_selector("#cancelBtn:not([hidden])")
    page.click("#cancelBtn")
    page.wait_for_selector("#runBtn:not([hidden])")
    assert page.evaluate("window.__cancelled") is True
    assert "отменён" in page.locator("#stage").text_content()

    # 6. Минимальный размер окна (см. main.js: 980x640): ничего не должно ломаться.
    page.set_viewport_size({"width": 980, "height": 640})
    page.wait_for_timeout(200)
    for sel in ("#runBtn", "#steps", ".brand"):
        inside(sel, 980, 640)
    overflow = page.evaluate("""() => {
      const main = document.querySelector('.main');
      return main.scrollWidth > main.clientWidth + 1 || document.documentElement.scrollWidth > innerWidth + 1;
    }""")
    assert not overflow, "горизонтальная прокрутка в минимальном окне"
    page.screenshot(path=f"{OUT}/6_min_window.png")

    browser.close()

csp = [t for kind, t in console if "Content Security Policy" in t or "Refused" in t]
errors = [t for kind, t in console if kind == "error"]

if csp:
    problems.append(f"нарушения CSP: {csp}")
if errors:
    problems.append(f"ошибки в консоли: {errors}")

if problems:
    print("ПРОБЛЕМЫ:")
    for item in problems:
        print(" -", item)
    sys.exit(1)

print("Интерфейс: все проверки пройдены, ошибок CSP и консоли нет.")
