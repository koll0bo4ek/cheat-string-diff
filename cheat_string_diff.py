#!/usr/bin/env python3
"""
cheat_string_diff.py

Поиск стабильных "packed/obfuscated" строк в cheat-дампах Minecraft
с исключением всего, что встречается хотя бы в одном clean-дампе.

Пример:
    python cheat_string_diff.py ^
        --clean clean1.dmp clean2.dmp clean3.dmp ^
        --cheat cheat1.dmp cheat2.dmp cheat3.dmp ^
        -o candidates.txt

Для поиска строк, которые могут быть рандомизированы между сессиями:
    python cheat_string_diff.py ^
        --clean clean1.dmp clean2.dmp ^
        --cheat cheat1.dmp cheat2.dmp cheat3.dmp ^
        --min-cheat-hits 1

Режим для графического интерфейса (Electron):
    python cheat_string_diff.py --json --clean ... --cheat ...
В этом режиме в stdout уходят события в формате NDJSON (один JSON-объект
на строку): start, file_progress, file_done, result. Логи ([INFO]/[WARN])
по-прежнему идут в stderr. Без --json результат тот же, что у первой версии.
Отличие одно: нечитаемый или пустой дамп теперь останавливает анализ с
ошибкой, а не пропускается с предупреждением (иначе clean-дамп, который
не прочитался, молча ослабил бы исключение строк).

Как это устроено внутри (результат тот же, что у первой версии, но быстрее):

  1. Фильтр работает с bytes, а не str: декодируем только то, что прошло.
  2. Строка попадает в ответ, только если прошла фильтр. Поэтому в
     остальных дампах фильтр не нужен: достаточно проверить, встречается
     ли там уже найденный кандидат. Это операция со множеством, она в
     разы дешевле полного разбора.
  3. Дампы, которым нужна только проверка вхождения, обрабатываются
     параллельно (--jobs).

Только стандартная библиотека Python 3.
"""

from __future__ import annotations

import argparse
import json
import math
import mmap
import multiprocessing
import os
import re
import signal
import sys
import tempfile
import time
from collections import Counter
from typing import Callable, Iterable, Optional


DEFAULT_MIN_LEN = 8
DEFAULT_MIN_ENTROPY = 2.6
DEFAULT_MAX_VOWEL_RATIO = 0.30
DEFAULT_MIN_UNIQUE = 4
DEFAULT_JSON_LIMIT = 100000
MAX_JOBS = 8

# Во сколько раз полный разбор дороже проверки вхождения (замер на 220 МБ):
# нужно только для честного общего прогресса.
FILTER_COST = 2.4

# Битовые флаги кодировки, в которой найдена строка.
ENC_ASCII = 1
ENC_UTF16 = 2

# Функция прогресса: (фаза 0/1, смещение в файле, размер файла).
ProgressFn = Callable[[int, int, int], None]

# Как часто дёргать колбэк прогресса (в найденных совпадениях).
PROGRESS_EVERY = 4096


# ASCII printable:
#   \x20 = space
#   \x7e = ~
ASCII_RE_TEMPLATE = rb"[\x20-\x7e]{%d,}"
UTF16_RE_TEMPLATE = rb"(?:[\x20-\x7e]\x00){%d,}"


# Очевидные фрагменты читаемого текста / путей / URL.
# Проверки те же, что в первой версии, но по bytes: содержимое строк
# всегда в диапазоне 0x20-0x7e, поэтому поведение совпадает с str.
URL_RE = re.compile(
    rb"^(?:https?|ftp)://",
    re.IGNORECASE,
)

WINDOWS_PATH_RE = re.compile(
    rb"^(?:[A-Za-z]:[\\/]|\\\\)",
)

UNIX_PATH_RE = re.compile(
    rb"^/(?:[^/\s]+/)+",
)

# Частые расширения файлов/ресурсов.
FILE_EXT_RE = re.compile(
    rb"\.(?:dll|exe|jar|zip|7z|rar|class|json|xml|cfg|ini|log|txt|"
    rb"png|jpg|jpeg|gif|webp|wav|mp3|ogg|dat|db|sqlite|pak)$",
    re.IGNORECASE,
)

SPACE = 0x20
BACKSLASH = 0x5C
SLASH = 0x2F

# Буквы A-Z и a-z: набор нужен, чтобы отбросить строки из одних букв.
ALPHA_BYTES = frozenset(range(0x41, 0x5B)) | frozenset(range(0x61, 0x7B))

# Таблица для bytes.translate: оставляет только гласные, остальное удаляет.
# Так число гласных считается одним вызовом на стороне C.
VOWEL_BYTES = b"aeiouAEIOU"
DELETE_NON_VOWELS = bytes(i for i in range(256) if i not in VOWEL_BYTES)

LOG2 = math.log2


def shannon_entropy(s) -> float:
    """Возвращает энтропию Шеннона по символам строки (str или bytes)."""
    if not s:
        return 0.0

    counts = Counter(s)
    length = len(s)

    entropy = 0.0
    for count in counts.values():
        p = count / length
        entropy -= p * LOG2(p)

    return entropy


def vowel_ratio(s) -> float:
    """Доля латинских гласных среди всех символов строки."""
    if not s:
        return 1.0

    if isinstance(s, bytes):
        return len(s.translate(None, DELETE_NON_VOWELS)) / len(s)

    return sum(1 for ch in s.lower() if ch in "aeiou") / len(s)


def has_non_alpha(s) -> bool:
    """Хотя бы один символ не является буквой."""
    if isinstance(s, bytes):
        return bool(frozenset(s) - ALPHA_BYTES)

    return any(not ch.isalpha() for ch in s)


def looks_like_path_or_url(s: bytes) -> bool:
    """Отбрасывает URL, Windows/Unix пути и известные файловые имена."""
    stripped = s.strip()

    if not stripped:
        return True

    if URL_RE.match(stripped):
        return True

    if WINDOWS_PATH_RE.match(stripped):
        return True

    if UNIX_PATH_RE.match(stripped):
        return True

    if FILE_EXT_RE.search(stripped):
        return True

    # Очевидные разделители путей.
    if BACKSLASH in stripped and SLASH in stripped:
        return True

    return False


def is_degenerate(s: bytes, min_unique: int) -> bool:
    """Отбрасывает повторяющиеся/вырожденные строки."""
    unique = len(frozenset(s))

    if unique < min_unique:
        return True

    # Одинаковый символ.
    if unique == 1:
        return True

    # Простые повторяющиеся паттерны:
    # abab abab, abc abc, 123123 и т.п.
    n = len(s)
    for period in range(1, min(n // 2 + 1, 8)):
        if n % period == 0:
            pattern = s[:period]
            if pattern * (n // period) == s:
                return True

    return False


def is_packed_candidate(
    s: bytes,
    *,
    min_len: int,
    min_entropy: float,
    max_vowel: float,
    min_unique: int,
) -> bool:
    """
    Проверяет, похожа ли строка на packed/obfuscated мусор.

    Этот фильтр НЕ определяет принадлежность чита к процессу.
    Он только отбрасывает читаемый/технический текст.

    Проверки идут от дешёвых к дорогим: результат тот же, что при любом
    другом порядке, но до тяжёлых шагов доходит меньше строк.
    """
    n = len(s)

    if n < min_len:
        return False

    # Много пробелов обычно означает обычный текст/сообщение.
    if s.count(SPACE) > max(1, n // 5):
        return False

    # Читаемый текст обычно имеет более высокую долю гласных.
    if len(s.translate(None, DELETE_NON_VOWELS)) / n > max_vowel:
        return False

    counts = Counter(s)
    unique = len(counts)

    # Минимальное разнообразие символов.
    if unique < min_unique:
        return False

    # Нужен хотя бы один символ, который не является буквой.
    if not (counts.keys() - ALPHA_BYTES):
        return False

    # Проверка энтропии.
    entropy = 0.0
    for count in counts.values():
        p = count / n
        entropy -= p * LOG2(p)

    if entropy < min_entropy:
        return False

    if not s.strip():
        return False

    if looks_like_path_or_url(s):
        return False

    # Повторяющиеся паттерны: уникальность уже проверена выше.
    for period in range(1, min(n // 2 + 1, 8)):
        if n % period == 0:
            pattern = s[:period]
            if pattern * (n // period) == s:
                return False

    return True


def decode_ascii(raw: bytes) -> str:
    """Безопасно декодирует ASCII."""
    return raw.decode("ascii", errors="ignore")


def decode_utf16le(raw: bytes) -> str:
    """Безопасно декодирует UTF-16LE."""
    try:
        return raw.decode("utf-16le", errors="ignore")
    except UnicodeDecodeError:
        return ""


def iter_ascii_strings(mm, min_len: int, start: int, end: int):
    """Извлекает ASCII printable-строки; возвращает (смещение, байты)."""
    pattern = re.compile(ASCII_RE_TEMPLATE % min_len)

    for match in pattern.finditer(mm, start, end):
        yield match.start(), match.group()


def iter_utf16le_strings(mm, min_len: int, start: int, end: int):
    """
    Извлекает printable ASCII в UTF-16LE.

    Возвращает (смещение, байты без нулей): дальше строка ничем не
    отличается от ASCII-варианта, поэтому сравнивать их можно напрямую.
    """
    pattern = re.compile(UTF16_RE_TEMPLATE % min_len)

    for match in pattern.finditer(mm, start, end):
        yield match.start(), match.group()[::2]


PASSES = (
    (iter_ascii_strings, ENC_ASCII),
    (iter_utf16le_strings, ENC_UTF16),
)


class DumpError(Exception):
    """Дамп нельзя прочитать: сообщение уже готово для пользователя."""


def open_dump(path: str):
    """Проверяет файл и возвращает (file, mmap). Бросает DumpError."""
    if not os.path.exists(path):
        raise DumpError(f"Файл не существует: {path}")

    if not os.path.isfile(path):
        raise DumpError(f"Это не файл: {path}")

    if os.path.getsize(path) == 0:
        raise DumpError(f"Файл пустой: {path}")

    handle = open(path, "rb")

    try:
        return handle, mmap.mmap(handle.fileno(), length=0, access=mmap.ACCESS_READ)
    except (OSError, ValueError) as exc:
        handle.close()
        raise DumpError(f"Не удалось прочитать {path}: {exc}") from exc


def extract_candidates(
    path: str,
    start: int = 0,
    end: Optional[int] = None,
    *,
    min_len: int,
    min_entropy: float,
    max_vowel: float,
    min_unique: int,
    progress: Optional[ProgressFn] = None,
) -> dict[bytes, int]:
    """
    Полный разбор куска дампа: извлекает строки и прогоняет их фильтром.

    Возвращает {строка в байтах: маска кодировок}.
    """
    result: dict[bytes, int] = {}
    check = is_packed_candidate
    get = result.get

    handle, mm = open_dump(path)
    size = os.path.getsize(path)

    if end is None:
        end = size

    try:
        for phase, (iterator, flag) in enumerate(PASSES):
            seen = 0

            for offset, value in iterator(mm, min_len, start, end):
                seen += 1

                if progress is not None and seen % PROGRESS_EVERY == 0:
                    progress(phase, offset, size)

                if check(
                    value,
                    min_len=min_len,
                    min_entropy=min_entropy,
                    max_vowel=max_vowel,
                    min_unique=min_unique,
                ):
                    result[value] = get(value, 0) | flag
    finally:
        mm.close()
        handle.close()

    return result


def match_candidates(
    path: str,
    start: int,
    end: Optional[int],
    candidates: dict[bytes, int],
    *,
    min_len: int,
    progress: Optional[ProgressFn] = None,
) -> tuple[bytearray, bytearray]:
    """
    Быстрый проход: ищет в дампе только уже известных кандидатов.

    Фильтр здесь не нужен. Если строка не прошла бы фильтр, её не было бы
    и среди кандидатов, поэтому проверка вхождения даёт тот же ответ.

    Возвращает (битовая карта найденного, маски кодировок по индексам).
    """
    total = len(candidates)
    hits = bytearray((total + 7) // 8)
    flags = bytearray(total)

    handle, mm = open_dump(path)
    size = os.path.getsize(path)

    if end is None:
        end = size

    try:
        for phase, (iterator, flag) in enumerate(PASSES):
            seen = 0
            lookup = candidates.get

            for offset, value in iterator(mm, min_len, start, end):
                seen += 1

                if progress is not None and seen % PROGRESS_EVERY == 0:
                    progress(phase, offset, size)

                index = lookup(value)

                if index is not None:
                    hits[index >> 3] |= 1 << (index & 7)
                    flags[index] |= flag
    finally:
        mm.close()
        handle.close()

    return hits, flags


# ==================================
# РАЗБИЕНИЕ ФАЙЛА НА КУСКИ
# ==================================
# Чтобы занять все ядра даже на одном дампе, файл режется на части.
# Резать можно не где угодно: найденная строка не должна попасть
# на границу, иначе результат отличался бы от однопоточного.
#
# Безопасная граница - байт, который не может быть частью строки:
# он не печатный (вне 0x20-0x7e) и не ноль (нули входят в UTF-16).
# Ни ASCII-, ни UTF-16-строка через такой байт не проходит.

HARD_SEPARATOR_RE = re.compile(rb"[^\x00\x20-\x7e]")

MIN_CHUNK_BYTES = 8 * 1024 * 1024


def plan_chunks(size: int, parts: int, find_cut) -> list[tuple[int, int]]:
    """
    Делит [0, size) не более чем на parts частей по безопасным границам.

    find_cut(position) должен вернуть позицию ближайшей безопасной
    границы справа или None, если её нет.
    """
    if parts <= 1 or size <= MIN_CHUNK_BYTES:
        return [(0, size)]

    parts = min(parts, max(1, size // MIN_CHUNK_BYTES))

    if parts <= 1:
        return [(0, size)]

    step = size // parts
    bounds = [0]

    for i in range(1, parts):
        cut = find_cut(step * i)

        if cut is None or cut >= size:
            break

        if cut > bounds[-1]:
            bounds.append(cut)

    bounds.append(size)

    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def chunks_for(path: str, parts: int) -> list[tuple[int, int]]:
    """Планирует куски для файла, подглядывая в него ради границ."""
    size = os.path.getsize(path)

    if parts <= 1 or size <= MIN_CHUNK_BYTES:
        return [(0, size)]

    handle, mm = open_dump(path)

    try:
        def find_cut(position: int):
            match = HARD_SEPARATOR_RE.search(mm, position)
            return match.start() if match else None

        return plan_chunks(size, parts, find_cut)
    finally:
        mm.close()
        handle.close()


# ==================================
# ПАРАЛЛЕЛЬНАЯ ОБРАБОТКА
# ==================================
# Обмен с рабочими процессами держим компактным:
#   - список кандидатов уходит один раз через временный файл
#     (строки печатные, \n внутри них быть не может);
#   - назад приходят битовые карты, а не наборы строк.

_WORKER: dict = {}


def _worker_init(queue, candidates_path, params):
    """Инициализация рабочего процесса: один раз читает кандидатов."""
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass

    _WORKER["queue"] = queue
    _WORKER["params"] = params

    if candidates_path:
        with open(candidates_path, "rb") as f:
            blob = f.read()

        parts = blob.split(b"\n") if blob else []
        _WORKER["candidates"] = {value: i for i, value in enumerate(parts)}
    else:
        _WORKER["candidates"] = None


def _progress_sender(task):
    """Колбэк прогресса: шлёт в очередь, сколько байт куска пройдено."""
    queue = _WORKER.get("queue")

    if queue is None:
        return None

    _, group, file_index, total, path, chunk_index, start, end = task
    span = max(1, end - start)
    last = [0.0]

    def callback(phase: int, offset: int, _size: int) -> None:
        now = time.monotonic()

        if now - last[0] < 0.2:
            return

        last[0] = now
        done = phase * span + min(span, max(0, offset - start))

        try:
            queue.put_nowait(("progress", group, file_index, total, path, chunk_index, done))
        except Exception:
            pass

    return callback


def _worker_scan(task):
    """Одна задача: кусок дампа, полный разбор или проверка вхождения."""
    mode, group, file_index, total, path, chunk_index, start, end = task
    params = _WORKER["params"]
    head = (group, path, file_index, chunk_index)

    try:
        progress = _progress_sender(task)

        if mode == "filter":
            found = extract_candidates(
                path, start, end, progress=progress, **params
            )

            # Отдаём одним куском: пересылка не зависит от числа строк.
            return ("filter", *head, b"\n".join(found), bytes(found.values()))

        hits, flags = match_candidates(
            path,
            start,
            end,
            _WORKER["candidates"],
            min_len=params["min_len"],
            progress=progress,
        )

        return ("match", *head, bytes(hits), bytes(flags))

    except DumpError as exc:
        return ("error", *head, str(exc), b"")


class _DirectQueue:
    """Очередь-заглушка для однопоточного режима: сразу в отчёт."""

    def __init__(self, sink):
        self.sink = sink

    def put_nowait(self, item):
        self.sink(item)


def prefer_windowless_python(context) -> None:
    """
    На Windows рабочие процессы запускаем через pythonw.exe: так не
    мелькают окна консоли (способ описан в документации multiprocessing).
    """
    if os.name != "nt":
        return

    exe = sys.executable

    if exe.lower().endswith("python.exe"):
        candidate = exe[:-len("python.exe")] + "pythonw.exe"

        if os.path.exists(candidate):
            try:
                context.set_executable(candidate)
            except Exception:
                pass


def run_tasks(tasks, params, candidates_path, jobs, on_progress, on_done):
    """
    Выполняет задачи: без процессов при jobs=1, иначе в пуле.

    Порядок вызовов on_done не гарантирован.
    """
    if jobs <= 1 or len(tasks) <= 1:
        _worker_init(_DirectQueue(on_progress) if on_progress else None,
                     candidates_path, params)

        for task in tasks:
            on_done(_worker_scan(task))

        return

    context = multiprocessing.get_context("spawn")
    prefer_windowless_python(context)

    try:
        queue = context.Queue()
        pool = context.Pool(
            processes=jobs,
            initializer=_worker_init,
            initargs=(queue, candidates_path, params),
        )
    except (OSError, ValueError, ImportError) as exc:
        log(f"[WARN] Не удалось запустить рабочие процессы ({exc}): работаю в одном процессе.")
        run_tasks(tasks, params, candidates_path, 1, on_progress, on_done)
        return

    def drain():
        while True:
            try:
                item = queue.get_nowait()
            except Exception:
                return

            if on_progress and item and item[0] == "progress":
                on_progress(item)

    pending = []

    try:
        for task in tasks:
            pending.append(pool.apply_async(_worker_scan, (task,)))

        pool.close()

        remaining = list(pending)

        while remaining:
            drain()
            waiting = []

            for item in remaining:
                if item.ready():
                    on_done(item.get())
                else:
                    waiting.append(item)

            remaining = waiting

            if remaining:
                time.sleep(0.05)

        drain()
    finally:
        pool.terminate()
        pool.join()

        try:
            queue.close()
        except Exception:
            pass


def choose_jobs(requested: Optional[int], ceiling: int) -> int:
    """Сколько процессов запускать: не больше задач и не больше ядер."""
    if requested is not None:
        return max(1, min(requested, ceiling))

    cpus = os.cpu_count() or 1

    return max(1, min(cpus, ceiling, MAX_JOBS))


# ==================================
# ОТЧЁТ ДЛЯ ИНТЕРФЕЙСА
# ==================================


class Reporter:
    """Пишет события в stdout в формате NDJSON (режим --json)."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def emit(self, event: dict) -> None:
        if not self.enabled:
            return

        sys.stdout.write(
            json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"
        )
        sys.stdout.flush()


def log(message: str) -> None:
    print(message, file=sys.stderr)


class Progress:
    """
    Сводит прогресс по кускам в прогресс по файлам и общий процент.

    Работа над файлом - это два прохода по всем его байтам, поэтому
    полный объём считаем как удвоенный размер. Полный разбор дороже
    проверки вхождения (FILTER_COST), это учитывается в общем проценте.
    """

    def __init__(self, reporter: Reporter):
        self.reporter = reporter
        self.chunks: dict[tuple[str, int], int] = {}
        self.sums: dict[str, int] = {}
        self.total: dict[str, int] = {}
        self.weight: dict[str, float] = {}
        self.last = 0.0

    def register(self, path: str, size: int) -> None:
        self.total[path] = max(1, 2 * size)
        self.weight[path] = 1.0
        self.sums[path] = 0

    def overall(self) -> float:
        everything = sum(self.total[p] * self.weight[p] for p in self.total)
        finished = sum(
            min(self.sums[p], self.total[p]) * self.weight[p] for p in self.total
        )

        return min(1.0, finished / everything) if everything else 0.0

    def finish(self, path: str) -> None:
        if path in self.total:
            self.sums[path] = self.total[path]

    def update(self, item) -> None:
        _, group, file_index, total, path, chunk_index, done = item

        if path not in self.total:
            return

        key = (path, chunk_index)
        self.sums[path] += done - self.chunks.get(key, 0)
        self.chunks[key] = done

        now = time.monotonic()

        if now - self.last < 0.15:
            return

        self.last = now

        self.reporter.emit({
            "type": "file_progress",
            "group": group,
            "index": file_index,
            "total": total,
            "path": path,
            "fraction": round(min(1.0, self.sums[path] / self.total[path]), 4),
            "overall": round(self.overall(), 4),
        })


# ==================================
# АНАЛИЗ
# ==================================


POPCOUNT = bytes(bin(i).count("1") for i in range(256))


def popcount(data) -> int:
    """Сколько единиц в битовой карте (сколько кандидатов найдено)."""
    table = POPCOUNT
    return sum(table[value] for value in data)


def bits_to_int(data: bytes) -> int:
    return int.from_bytes(data, "little")


def int_to_bits(value: int, length: int) -> bytes:
    return value.to_bytes((length + 7) // 8, "little")


def write_candidates_file(candidates: list[bytes]) -> str:
    """Сохраняет кандидатов для рабочих процессов одним файлом."""
    handle, path = tempfile.mkstemp(prefix="csd-cand-", suffix=".bin")

    with os.fdopen(handle, "wb") as f:
        f.write(b"\n".join(candidates))

    return path


def build_tasks(mode: str, files: list[tuple[str, str, int, int]], jobs: int):
    """
    Готовит задачи так, чтобы кусков было примерно по числу процессов.

    files: список (группа, путь, номер файла, всего файлов в группе).
    """
    per_file = max(1, -(-jobs // max(1, len(files))))
    tasks = []

    for group, path, index, total in files:
        for chunk_index, (start, end) in enumerate(chunks_for(path, per_file)):
            tasks.append((mode, group, index, total, path, chunk_index, start, end))

    return tasks


def analyze(clean, cheat, params, min_hits, jobs, reporter):
    """
    Считает итоговый набор строк.

    Возвращает (строки в байтах, маски кодировок, в скольких cheat-дампах
    найдена строка, счётчики по clean-дампам, счётчики по cheat-дампам).
    """
    progress = Progress(reporter)
    errors: list[str] = []

    cheat_counts = [0] * len(cheat)
    clean_counts = [0] * len(clean)

    for path in clean + cheat:
        if os.path.isfile(path):
            progress.register(path, os.path.getsize(path))

    def note_file(group: str, index: int, total: int, path: str, count: int) -> None:
        label = "CLEAN" if group == "clean" else "CHEAT"
        log(f"[INFO] {label} {index}/{total}: {path} -> {count} совпадений")
        progress.finish(path)

        reporter.emit({
            "type": "file_done",
            "group": group,
            "index": index,
            "total": total,
            "path": path,
            "candidates": count,
            "overall": round(progress.overall(), 4),
        })

    # --- шаг 1: полный разбор --------------------------------------------
    # По умолчанию строка обязана быть во всех cheat-дампах, поэтому
    # полностью разбираем только один (самый маленький), а в остальных
    # ищем уже найденное. Если порог ниже, разбираем все.
    full_scan_all = min_hits < len(cheat)

    if full_scan_all:
        seed_files = list(cheat)
    else:
        smallest = min(
            cheat,
            key=lambda p: os.path.getsize(p) if os.path.isfile(p) else 0,
        )
        seed_files = [smallest]

    for path in seed_files:
        if path in progress.weight:
            progress.weight[path] = FILTER_COST

    seed_jobs = choose_jobs(jobs, MAX_JOBS)
    seed_tasks = build_tasks(
        "filter",
        [("cheat", path, cheat.index(path) + 1, len(cheat)) for path in seed_files],
        seed_jobs,
    )

    per_file_found: dict[str, dict[bytes, int]] = {path: {} for path in seed_files}

    def collect_filter(result):
        kind, group, path, index, _chunk, keys, flags = result

        if kind == "error":
            errors.append(keys)
            log(f"[WARN] {keys}")
            return

        found = per_file_found[path]

        for value, flag in zip(keys.split(b"\n") if keys else [], flags):
            found[value] = found.get(value, 0) | flag

    run_tasks(
        seed_tasks,
        params,
        None,
        choose_jobs(jobs, len(seed_tasks)),
        progress.update if reporter.enabled else None,
        collect_filter,
    )

    if errors:
        raise DumpError(errors[0])

    merged: dict[bytes, int] = {}
    counts: dict[bytes, int] = {}

    for path in seed_files:
        found = per_file_found[path]
        index = cheat.index(path) + 1
        cheat_counts[index - 1] = len(found)
        note_file("cheat", index, len(cheat), path, len(found))

        for value, flag in found.items():
            merged[value] = merged.get(value, 0) | flag
            counts[value] = counts.get(value, 0) + 1

    if not merged:
        for index, path in enumerate(clean, start=1):
            note_file("clean", index, len(clean), path, 0)

        for index, path in enumerate(cheat, start=1):
            if path not in seed_files:
                note_file("cheat", index, len(cheat), path, 0)

        return [], {}, {}, clean_counts, cheat_counts

    candidates = sorted(merged)
    total_candidates = len(candidates)
    index_of = {value: i for i, value in enumerate(candidates)}
    flags_by_index = bytearray(merged[value] for value in candidates)

    # keep: битовая карта «строка ещё в игре».
    # Собираем её байтами: сдвиги большого числа в цикле дали бы
    # квадратичную сложность на сотнях тысяч кандидатов.
    keep_bytes = bytearray(b"\xff" * ((total_candidates + 7) // 8))

    if full_scan_all:
        keep_bytes = bytearray((total_candidates + 7) // 8)

        for i, value in enumerate(candidates):
            if counts[value] >= min_hits:
                keep_bytes[i >> 3] |= 1 << (i & 7)

    keep = bits_to_int(bytes(keep_bytes))

    # --- шаг 2: быстрые проходы ------------------------------------------
    rest = [
        ("cheat", path, cheat.index(path) + 1, len(cheat))
        for path in cheat
        if path not in seed_files
    ] + [
        ("clean", path, index, len(clean))
        for index, path in enumerate(clean, start=1)
    ]

    if rest:
        match_jobs = choose_jobs(jobs, MAX_JOBS)
        tasks = build_tasks("match", rest, match_jobs)
        jobs_count = choose_jobs(jobs, len(tasks))
        candidates_path = None

        # Битовые карты кусков одного файла сначала объединяем, и как только
        # получены все куски, файл сразу учитывается и считается готовым.
        file_hits: dict[str, bytearray] = {}
        file_flags: dict[str, bytearray] = {}
        blank = (total_candidates + 7) // 8

        expected: dict[str, int] = {}

        for task in tasks:
            expected[task[4]] = expected.get(task[4], 0) + 1

        received: dict[str, int] = {}
        meta = {path: (group, index, total) for group, path, index, total in rest}

        def finish_file(path: str) -> None:
            nonlocal keep

            group, index, total = meta[path]
            hits = file_hits.get(path, bytearray(blank))
            found = popcount(hits)
            bits = bits_to_int(bytes(hits))

            if group == "clean":
                clean_counts[index - 1] = found
                keep &= ~bits
            else:
                cheat_counts[index - 1] = found
                keep &= bits

                for i, flag in enumerate(file_flags.get(path, b"")):
                    if flag:
                        flags_by_index[i] |= flag

            note_file(group, index, total, path, found)

        def collect_match(result):
            kind, group, path, index, _chunk, hits, flags = result

            if kind == "error":
                errors.append(hits)
                log(f"[WARN] {hits}")
                return

            combined = file_hits.get(path)

            if combined is None:
                file_hits[path] = bytearray(hits)
                file_flags[path] = bytearray(flags)
            else:
                for i, value in enumerate(hits):
                    if value:
                        combined[i] |= value

                other = file_flags[path]

                for i, value in enumerate(flags):
                    if value:
                        other[i] |= value

            received[path] = received.get(path, 0) + 1

            if received[path] == expected[path]:
                finish_file(path)

        try:
            if jobs_count > 1:
                candidates_path = write_candidates_file(candidates)
                run_tasks(
                    tasks,
                    params,
                    candidates_path,
                    jobs_count,
                    progress.update if reporter.enabled else None,
                    collect_match,
                )
            else:
                _worker_init(
                    _DirectQueue(progress.update) if reporter.enabled else None,
                    None,
                    params,
                )
                _WORKER["candidates"] = index_of

                for task in tasks:
                    collect_match(_worker_scan(task))
        finally:
            if candidates_path:
                try:
                    os.remove(candidates_path)
                except OSError:
                    pass

        if errors:
            raise DumpError(errors[0])

    mask = int_to_bits(keep, total_candidates)
    survivors = [
        value
        for i, value in enumerate(candidates)
        if mask[i >> 3] >> (i & 7) & 1
    ]

    encodings = {
        value: flags_by_index[index_of[value]] or ENC_ASCII
        for value in survivors
    }
    hit_counts = {
        value: (counts[value] if full_scan_all else len(cheat))
        for value in survivors
    }

    return survivors, encodings, hit_counts, clean_counts, cheat_counts


# ==================================
# ВЫВОД
# ==================================


def build_json_items(survivors, encodings, hit_counts, limit):
    """
    Готовит строки для GUI: s - строка, e - кодировки, h - в скольких
    cheat-дампах найдена, n - энтропия. Сначала самые «надёжные»:
    больше попаданий, затем длиннее.
    """
    rows = [
        {
            "s": decode_ascii(value),
            "e": encodings[value],
            "h": hit_counts[value],
            "n": round(shannon_entropy(value), 3),
        }
        for value in survivors
    ]

    rows.sort(key=lambda row: (-row["h"], -len(row["s"]), row["s"]))

    total = len(rows)

    if limit > 0:
        rows = rows[:limit]

    return rows, total


def write_output(values: Iterable[str], output_path: Optional[str]) -> None:
    """
    Пишет строки по одной на строку, без дополнительных данных.
    """
    sorted_values = sorted(values)

    if output_path:
        try:
            with open(
                output_path,
                "w",
                encoding="utf-8",
                errors="replace",
                newline="\n",
            ) as f:
                f.write("\n".join(sorted_values))

                if sorted_values:
                    f.write("\n")
        except OSError as exc:
            log(f"[ERROR] Не удалось записать результат в {output_path}: {exc}")
            raise SystemExit(1)
    else:
        out = sys.stdout

        for value in sorted_values:
            out.write(value)
            out.write("\n")


# ==================================
# CLI
# ==================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ищет packed/obfuscated строки в cheat-дампах "
            "и исключает всё, что встречается в clean-дампах."
        )
    )

    parser.add_argument(
        "--clean",
        nargs="+",
        required=True,
        metavar="PATH",
        help="Clean-дампы.",
    )

    parser.add_argument(
        "--cheat",
        nargs="+",
        required=True,
        metavar="PATH",
        help="Cheat-дамп(ы). Можно указать только один.",
    )

    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="Файл вывода. Иначе stdout.",
    )

    parser.add_argument("--min-len", type=int, default=DEFAULT_MIN_LEN)
    parser.add_argument("--min-entropy", type=float, default=DEFAULT_MIN_ENTROPY)
    parser.add_argument("--max-vowel-ratio", type=float, default=DEFAULT_MAX_VOWEL_RATIO)
    parser.add_argument("--min-unique", type=int, default=DEFAULT_MIN_UNIQUE)

    parser.add_argument(
        "--min-cheat-hits",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Минимальное число cheat-дампов. "
            "При одном cheat-дампе автоматически используется 1."
        ),
    )

    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Сколько кусков обрабатывать одновременно. "
            "По умолчанию по числу ядер, 1 отключает параллельность."
        ),
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Режим для GUI: события NDJSON в stdout вместо списка строк.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_JSON_LIMIT,
        metavar="N",
        help=(
            "Максимум строк в JSON-результате (0 = без ограничения). "
            "Лучшие строки идут первыми."
        ),
    )

    args = parser.parse_args()

    # Дубликаты внутри группы только замедляют работу.
    args.clean = list(dict.fromkeys(args.clean))
    args.cheat = list(dict.fromkeys(args.cheat))

    if len(args.clean) < 2:
        parser.error("--clean должен содержать минимум 2 файла.")

    if not args.cheat:
        parser.error("Нужно указать хотя бы один cheat-дамп.")

    if args.min_len < 1:
        parser.error("--min-len должен быть >= 1.")

    if args.min_entropy < 0:
        parser.error("--min-entropy должен быть >= 0.")

    if not 0.0 <= args.max_vowel_ratio <= 1.0:
        parser.error("--max-vowel-ratio должен быть от 0 до 1.")

    if args.min_unique < 1:
        parser.error("--min-unique должен быть >= 1.")

    if args.limit < 0:
        parser.error("--limit должен быть >= 0.")

    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs должен быть >= 1.")

    # Один cheat-дамп -> автоматически ищем все его кандидаты.
    if args.min_cheat_hits is None:
        args.min_cheat_hits = 1 if len(args.cheat) == 1 else len(args.cheat)

    if args.min_cheat_hits < 1:
        parser.error("--min-cheat-hits должен быть >= 1.")

    if args.min_cheat_hits > len(args.cheat):
        parser.error(
            "--min-cheat-hits не может быть больше количества cheat-дампов."
        )

    return args


def install_signal_handlers() -> None:
    """Отмена из интерфейса: завершаемся тихо, без трассировки."""

    def stop(_signum, _frame):
        raise SystemExit(130)

    for name in ("SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)

        if number is not None:
            try:
                signal.signal(number, stop)
            except (ValueError, OSError):
                pass


def main() -> int:
    args = parse_args()
    install_signal_handlers()

    reporter = Reporter(args.json)

    if args.json:
        # Пути и логи могут содержать кириллицу: не зависим от кодировки консоли.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    started = time.monotonic()

    if len(args.cheat) < 2:
        log("[WARN] Используется только 1 cheat-дамп: "
            "статистика по стабильности отсутствует.")

    params = {
        "min_len": args.min_len,
        "min_entropy": args.min_entropy,
        "max_vowel": args.max_vowel_ratio,
        "min_unique": args.min_unique,
    }

    reporter.emit({
        "type": "start",
        "clean": len(args.clean),
        "cheat": len(args.cheat),
    })

    try:
        survivors, encodings, hit_counts, clean_counts, cheat_counts = analyze(
            args.clean,
            args.cheat,
            params,
            args.min_cheat_hits,
            args.jobs,
            reporter,
        )
    except DumpError as exc:
        log(f"[ERROR] {exc}")
        reporter.emit({"type": "error", "message": str(exc)})
        return 2

    if args.output or not args.json:
        write_output((decode_ascii(value) for value in survivors), args.output)

    destination = args.output if args.output else "stdout"
    log(f"[INFO] Итоговых кандидатов: {len(survivors)} -> {destination}")

    if args.json:
        items, total = build_json_items(survivors, encodings, hit_counts, args.limit)

        reporter.emit({
            "type": "result",
            "items": items,
            "total": total,
            "truncated": total > len(items),
            "clean_counts": clean_counts,
            "cheat_counts": cheat_counts,
            "min_cheat_hits": args.min_cheat_hits,
            "elapsed": round(time.monotonic() - started, 2),
        })

    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
