"""
Сверка нового движка с эталоном (test/reference_engine.py, первая версия).

    python test/equivalence.py

Проверяет, что оптимизации не меняют результат:
  1. случайные небольшие дампы при разных параметрах и числе процессов;
  2. большие дампы, где параллельная работа режет файл на куски, а строки
     лежат вплотную к границам кусков (и сквозь «естественную» границу).
"""

import importlib.util
import os
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW = os.path.join(ROOT, "engine", "cheat_string_diff.py")
REF = os.path.join(ROOT, "test", "reference_engine.py")

ALPHABET = "BCDFGHJKLMNPQRSTVWXYZbcdfghjklmnpqrstvwxyz0123456789!@#$%^&*"


def make_string(rng, low=9, high=30):
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(low, high)))


def run(script, clean, cheat, extra):
    result = subprocess.run(
        [sys.executable, script, "--clean", *clean, "--cheat", *cheat, *extra],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-800:]
    return sorted(line for line in result.stdout.split("\n") if line)


def small_dumps(directory, rng):
    common = [make_string(rng) for _ in range(40)]
    stable = [make_string(rng) for _ in range(25)]
    partial = [make_string(rng) for _ in range(15)]
    wide = [make_string(rng) for _ in range(10)]

    def dump(ascii_strings, wide_strings=()):
        data = b"\0" * 32
        for value in ascii_strings:
            data += value.encode() + b"\0" * 17
        for value in wide_strings:
            data += value.encode("utf-16le") + b"\0" * 17
        data += b"The quick brown fox jumps over the lazy dog many times here\0"
        data += b"C:\\Users\\x\\AppData\\Roaming\\.minecraft\\mods\\thing.jar\0"
        data += b"https://example.com/some/path/file\0"
        return data

    contents = {
        "clean1.dmp": dump(common, wide[:4]),
        "clean2.dmp": dump(common + partial[:3], wide[:4]),
        "cheat1.dmp": dump(common + stable + partial[:8], wide),
        "cheat2.dmp": dump(common + stable + partial[4:], wide),
        "cheat3.dmp": dump(common + stable, wide),
    }

    for name, data in contents.items():
        with open(os.path.join(directory, name), "wb") as f:
            f.write(data)

    join = lambda n: os.path.join(directory, n)
    return [join("clean1.dmp"), join("clean2.dmp")], [join(f"cheat{i}.dmp") for i in (1, 2, 3)]


def boundary_dumps(directory, rng):
    """
    Два больших cheat-дампа (24 МБ) и два маленьких clean. В cheat-дампах
    возле каждой границы, которую выберет планировщик, лежат строки:
    прямо перед разделителем, прямо после и через «естественную» границу.
    """
    mb = 1024 * 1024
    size = 24 * mb
    planted = []

    def build(with_unique):
        data = bytearray(size)

        for k in range(1, 6):
            natural = k * size // 6
            separator = natural + 100
            data[separator] = 0x01  # не печатный и не ноль: безопасная граница

            before = make_string(rng, 30, 30)
            after = make_string(rng, 30, 30)
            spanning = make_string(rng, 30, 30)
            wide_before = make_string(rng, 20, 20)

            data[separator - 30:separator] = before.encode()
            data[separator + 1:separator + 31] = after.encode()
            data[natural - 15:natural + 15] = spanning.encode()
            data[separator - 80:separator - 40] = wide_before.encode("utf-16le")

            if with_unique:
                planted.extend([before, after, spanning, wide_before])

        return data

    # Одни и те же строки во всех cheat-дампах: строим первый, копируем второй.
    first = build(True)
    second = bytearray(first)

    paths = {}
    for name, payload in (("cheat1.dmp", first), ("cheat2.dmp", second)):
        paths[name] = os.path.join(directory, name)
        with open(paths[name], "wb") as f:
            f.write(payload)

    for name in ("clean1.dmp", "clean2.dmp"):
        paths[name] = os.path.join(directory, name)
        with open(paths[name], "wb") as f:
            f.write(b"\0" * 64 + make_string(rng).encode() + b"\0" * 64)

    return (
        [paths["clean1.dmp"], paths["clean2.dmp"]],
        [paths["cheat1.dmp"], paths["cheat2.dmp"]],
        planted,
    )


def main():
    rng = random.Random(5)
    failures = 0

    directory = tempfile.mkdtemp(prefix="equiv ")

    try:
        clean, cheat = small_dumps(directory, rng)

        cases = [
            [], ["--min-cheat-hits", "1"], ["--min-cheat-hits", "2"],
            ["--min-len", "12"], ["--min-entropy", "3.2"],
            ["--max-vowel-ratio", "0.5"], ["--min-unique", "6"],
        ]

        for extra in cases:
            expected = run(REF, clean, cheat, extra)

            for jobs in ("1", "2", "5"):
                actual = run(NEW, clean, cheat, [*extra, "--jobs", jobs])

                if actual != expected:
                    failures += 1
                    print(f"РАЗЛИЧИЕ: {extra or 'по умолчанию'}, jobs={jobs}: "
                          f"эталон {len(expected)}, новый {len(actual)}")

        print(f"1. Случайные дампы: {len(cases) * 3} сочетаний, расхождений {failures}")

        big_dir = os.path.join(directory, "big")
        os.makedirs(big_dir)
        clean, cheat, planted = boundary_dumps(big_dir, rng)

        expected = run(REF, clean, cheat, [])

        # Планировщик действительно режет файл на куски?
        spec = importlib.util.spec_from_file_location("engine_under_test", NEW)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        chunks = module.chunks_for(cheat[0], 4)
        assert len(chunks) >= 3, f"файл не порезан на куски: {chunks}"
        print(f"2. Границы кусков: файл 24 МБ разбит на {len(chunks)} части")

        missing = [value for value in planted if value not in expected]
        assert not missing, f"эталон потерял строки: {missing[:2]}"

        for jobs in ("1", "4"):
            actual = run(NEW, clean, cheat, ["--jobs", jobs])

            if actual != expected:
                failures += 1
                print(f"РАЗЛИЧИЕ на границах, jobs={jobs}: "
                      f"нет {sorted(set(expected) - set(actual))[:2]}, "
                      f"лишние {sorted(set(actual) - set(expected))[:2]}")

        print(f"   строк у границ: {len(planted)}, найдено эталоном {len(expected)}")
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    if failures:
        print(f"ПРОВАЛ: расхождений {failures}")
        sys.exit(1)

    print("Новый движок даёт тот же результат, что эталон.")


if __name__ == "__main__":
    main()
