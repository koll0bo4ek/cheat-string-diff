# ЭТАЛОН ДЛЯ ТЕСТОВ. Первая версия скрипта (до оптимизаций), не менять.
# test/equivalence.py сверяет с ним результат нового движка.
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
по-прежнему идут в stderr. Без --json поведение скрипта не изменилось.

Только стандартная библиотека Python 3.
"""

from __future__ import annotations

import argparse
import json
import math
import mmap
import os
import re
import sys
import time
from collections import Counter
from typing import Callable, Iterable, Optional


DEFAULT_MIN_LEN = 8
DEFAULT_MIN_ENTROPY = 2.6
DEFAULT_MAX_VOWEL_RATIO = 0.30
DEFAULT_MIN_UNIQUE = 4
DEFAULT_JSON_LIMIT = 100000

# Битовые флаги кодировки, в которой найдена строка.
ENC_ASCII = 1
ENC_UTF16 = 2

# Функция прогресса: (фаза 0/1, смещение в файле, размер файла).
ProgressFn = Callable[[int, int, int], None]


# ASCII printable:
#   \x20 = space
#   \x7e = ~
ASCII_RE_TEMPLATE = rb"[\x20-\x7e]{%d,}"
UTF16_RE_TEMPLATE = rb"(?:[\x20-\x7e]\x00){%d,}"


# Очевидные фрагменты читаемого текста / путей / URL.
URL_RE = re.compile(
    r"^(?:https?|ftp)://",
    re.IGNORECASE,
)

WINDOWS_PATH_RE = re.compile(
    r"^(?:[A-Za-z]:[\\/]|\\\\)",
)

UNIX_PATH_RE = re.compile(
    r"^/(?:[^/\s]+/)+",
)

# Частые расширения файлов/ресурсов.
FILE_EXT_RE = re.compile(
    r"\.(?:dll|exe|jar|zip|7z|rar|class|json|xml|cfg|ini|log|txt|"
    r"png|jpg|jpeg|gif|webp|wav|mp3|ogg|dat|db|sqlite|pak)$",
    re.IGNORECASE,
)


def shannon_entropy(s: str) -> float:
    """Возвращает энтропию Шеннона по символам строки."""
    if not s:
        return 0.0

    counts = Counter(s)
    length = len(s)

    entropy = 0.0
    for count in counts.values():
        p = count / length
        entropy -= p * math.log2(p)

    return entropy


def vowel_ratio(s: str) -> float:
    """Доля латинских гласных среди всех символов строки."""
    if not s:
        return 1.0

    vowels = sum(1 for ch in s.lower() if ch in "aeiou")
    return vowels / len(s)


def has_non_alpha(s: str) -> bool:
    """Хотя бы один символ не является буквой."""
    return any(not ch.isalpha() for ch in s)


def looks_like_path_or_url(s: str) -> bool:
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
    if "\\" in stripped and "/" in stripped:
        return True

    return False


def is_degenerate(s: str, min_unique: int) -> bool:
    """Отбрасывает повторяющиеся/вырожденные строки."""
    if len(set(s)) < min_unique:
        return True

    # Одинаковый символ.
    if len(set(s)) == 1:
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
    s: str,
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
    """
    if len(s) < min_len:
        return False

    if not s.strip():
        return False

    # Много пробелов обычно означает обычный текст/сообщение.
    if s.count(" ") > max(1, len(s) // 5):
        return False

    if looks_like_path_or_url(s):
        return False

    # Нужен хотя бы один символ, который не является буквой.
    if not has_non_alpha(s):
        return False

    # Минимальное разнообразие символов.
    if len(set(s)) < min_unique:
        return False

    if is_degenerate(s, min_unique):
        return False

    # Читаемый текст обычно имеет более высокую долю гласных.
    if vowel_ratio(s) > max_vowel:
        return False

    # Проверка энтропии.
    if shannon_entropy(s) < min_entropy:
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


def iter_ascii_strings(mm: mmap.mmap, min_len: int) -> Iterable[tuple[int, str]]:
    """Извлекает ASCII printable-строки; возвращает (смещение, строка)."""
    pattern = re.compile(ASCII_RE_TEMPLATE % min_len)

    for match in pattern.finditer(mm):
        value = decode_ascii(match.group())
        if value:
            yield match.start(), value


def iter_utf16le_strings(mm: mmap.mmap, min_len: int) -> Iterable[tuple[int, str]]:
    """Извлекает printable ASCII в UTF-16LE; возвращает (смещение, строка)."""
    pattern = re.compile(UTF16_RE_TEMPLATE % min_len)

    for match in pattern.finditer(mm):
        value = decode_utf16le(match.group())
        if value:
            yield match.start(), value


def extract_strings(
    path: str,
    *,
    min_len: int,
    min_entropy: float,
    max_vowel: float,
    min_unique: int,
    progress: Optional[ProgressFn] = None,
) -> dict[str, int]:
    """
    Извлекает и фильтрует строки из одного дампа.

    Возвращает словарь {строка: битовая маска кодировок}
    (ENC_ASCII / ENC_UTF16). Внутри одного файла результат дедуплицируется.
    """
    result: dict[str, int] = {}

    try:
        if not os.path.exists(path):
            print(f"[WARN] Файл не существует: {path}", file=sys.stderr)
            return result

        if not os.path.isfile(path):
            print(f"[WARN] Это не файл: {path}", file=sys.stderr)
            return result

        size = os.path.getsize(path)

        if size == 0:
            print(f"[WARN] Файл пустой: {path}", file=sys.stderr)
            return result

        with open(path, "rb") as f:
            with mmap.mmap(
                f.fileno(),
                length=0,
                access=mmap.ACCESS_READ,
            ) as mm:

                passes = (
                    (iter_ascii_strings, ENC_ASCII),
                    (iter_utf16le_strings, ENC_UTF16),
                )

                for phase, (iterator, flag) in enumerate(passes):
                    seen = 0

                    for offset, value in iterator(mm, min_len):
                        seen += 1

                        if progress is not None and seen % 2048 == 0:
                            progress(phase, offset, size)

                        if is_packed_candidate(
                            value,
                            min_len=min_len,
                            min_entropy=min_entropy,
                            max_vowel=max_vowel,
                            min_unique=min_unique,
                        ):
                            result[value] = result.get(value, 0) | flag

    except (OSError, ValueError) as exc:
        print(f"[WARN] Не удалось прочитать {path}: {exc}", file=sys.stderr)

    return result


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

    def file_progress(
        self,
        group: str,
        index: int,
        total: int,
        path: str,
    ) -> ProgressFn:
        """Возвращает колбэк прогресса для одного файла (не чаще 5 раз/сек)."""
        last = 0.0

        def callback(phase: int, offset: int, size: int) -> None:
            nonlocal last

            now = time.monotonic()
            if now - last < 0.2:
                return
            last = now

            part = (offset / size) if size else 1.0

            self.emit({
                "type": "file_progress",
                "group": group,
                "index": index,
                "total": total,
                "path": path,
                "fraction": round((phase + part) / 2, 4),
            })

        return callback


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

    parser.add_argument(
        "--min-len",
        type=int,
        default=DEFAULT_MIN_LEN,
    )

    parser.add_argument(
        "--min-entropy",
        type=float,
        default=DEFAULT_MIN_ENTROPY,
    )

    parser.add_argument(
        "--max-vowel-ratio",
        type=float,
        default=DEFAULT_MAX_VOWEL_RATIO,
    )

    parser.add_argument(
        "--min-unique",
        type=int,
        default=DEFAULT_MIN_UNIQUE,
    )

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
        "--json",
        action="store_true",
        help=(
            "Режим для GUI: события NDJSON в stdout вместо списка строк."
        ),
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


def build_frequency_map(
    paths: list[str],
    *,
    min_len: int,
    min_entropy: float,
    max_vowel: float,
    min_unique: int,
    label: str,
    reporter: Optional[Reporter] = None,
    track_encodings: bool = False,
) -> tuple[list[set[str]], Counter[str], dict[str, int]]:
    """
    Читает все дампы, возвращает множества строк по файлам,
    частоту появления строки по количеству файлов
    и (если track_encodings) объединённую маску кодировок.
    """
    file_sets: list[set[str]] = []
    frequencies: Counter[str] = Counter()
    encodings: dict[str, int] = {}

    group = label.lower()
    live = reporter is not None and reporter.enabled

    for index, path in enumerate(paths, start=1):
        progress = (
            reporter.file_progress(group, index, len(paths), path)
            if live
            else None
        )

        found = extract_strings(
            path,
            min_len=min_len,
            min_entropy=min_entropy,
            max_vowel=max_vowel,
            min_unique=min_unique,
            progress=progress,
        )

        strings = set(found)

        file_sets.append(strings)
        frequencies.update(strings)

        if track_encodings:
            for value, flag in found.items():
                encodings[value] = encodings.get(value, 0) | flag

        print(
            f"[INFO] {label} {index}/{len(paths)}: "
            f"{path} -> {len(strings)} кандидатов",
            file=sys.stderr,
        )

        if live:
            reporter.emit({
                "type": "file_done",
                "group": group,
                "index": index,
                "total": len(paths),
                "path": path,
                "candidates": len(strings),
            })

    return file_sets, frequencies, encodings


def calculate_result(
    clean_sets: list[set[str]],
    cheat_freq: Counter[str],
    *,
    min_cheat_hits: int,
) -> set[str]:
    """
    result = строки, присутствующие минимум в N cheat-дампах
             минус объединение clean-дампов.
    """
    clean_union: set[str] = set()

    for strings in clean_sets:
        clean_union.update(strings)

    stable_cheat = {
        value
        for value, hits in cheat_freq.items()
        if hits >= min_cheat_hits
    }

    return stable_cheat - clean_union


def build_json_items(
    result: set[str],
    cheat_freq: Counter[str],
    encodings: dict[str, int],
    limit: int,
) -> tuple[list[dict], int]:
    """
    Готовит строки для GUI: s - строка, e - кодировки, h - в скольких
    cheat-дампах найдена, n - энтропия. Сначала самые «надёжные»:
    больше попаданий, затем длиннее.
    """
    rows = [
        {
            "s": value,
            "e": encodings.get(value, ENC_ASCII),
            "h": cheat_freq[value],
            "n": round(shannon_entropy(value), 3),
        }
        for value in result
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
                for value in sorted_values:
                    f.write(value)
                    f.write("\n")
        except OSError as exc:
            print(
                f"[ERROR] Не удалось записать результат в {output_path}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
    else:
        for value in sorted_values:
            sys.stdout.write(value)
            sys.stdout.write("\n")


def main() -> int:
    args = parse_args()

    reporter = Reporter(args.json)

    if args.json:
        # Пути и логи могут содержать кириллицу: не зависим от кодировки консоли.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    started = time.monotonic()
    cheat_hits_required = args.min_cheat_hits

    if len(args.cheat) < 2:
        print(
            "[WARN] Используется только 1 cheat-дамп: "
            "статистика по стабильности отсутствует.",
            file=sys.stderr,
        )

    reporter.emit({
        "type": "start",
        "clean": len(args.clean),
        "cheat": len(args.cheat),
    })

    clean_sets, _, _ = build_frequency_map(
        args.clean,
        min_len=args.min_len,
        min_entropy=args.min_entropy,
        max_vowel=args.max_vowel_ratio,
        min_unique=args.min_unique,
        label="CLEAN",
        reporter=reporter,
    )

    cheat_sets, cheat_freq, cheat_encodings = build_frequency_map(
        args.cheat,
        min_len=args.min_len,
        min_entropy=args.min_entropy,
        max_vowel=args.max_vowel_ratio,
        min_unique=args.min_unique,
        label="CHEAT",
        reporter=reporter,
        track_encodings=True,
    )

    result = calculate_result(
        clean_sets,
        cheat_freq,
        min_cheat_hits=cheat_hits_required,
    )

    if args.output or not args.json:
        write_output(result, args.output)

    destination = args.output if args.output else "stdout"

    print(
        f"[INFO] Итоговых кандидатов: {len(result)} -> {destination}",
        file=sys.stderr,
    )

    if args.json:
        items, total = build_json_items(
            result,
            cheat_freq,
            cheat_encodings,
            args.limit,
        )

        reporter.emit({
            "type": "result",
            "items": items,
            "total": total,
            "truncated": total > len(items),
            "clean_counts": [len(s) for s in clean_sets],
            "cheat_counts": [len(s) for s in cheat_sets],
            "min_cheat_hits": cheat_hits_required,
            "elapsed": round(time.monotonic() - started, 2),
        })

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
