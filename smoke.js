"use strict";

/*
 * Проверка без Electron:  node test/smoke.js
 *
 * Создаёт «дампы» из нулей с известными строками внутри и проверяет,
 * что движок находит ровно то, что должен.
 */

const assert = require("assert");
const fs = require("fs");
const os = require("os");
const path = require("path");

const { findPython, validateRequest, startEngine } = require("../lib/engine");
const { buildYaraRule } = require("../lib/yara");

const enginePath = path.join(__dirname, "..", "engine", "cheat_string_diff.py");

// Строки «как у packed-читов»: мало гласных, есть спецсимволы, высокая энтропия.
const COMMON = "Xk9$Qz!vLm2";      // есть и в clean, и в cheat -> должна исчезнуть
const STABLE = "aZ9#qP!x7Lw";      // во всех cheat-дампах
const ONCE = "Rn8@vT$k2Ye";        // только в первом cheat-дампе
const WIDE = "wQ7!zL#p3Mx";        // UTF-16, во всех cheat-дампах
const TEXT = "The quick brown fox jumps over"; // читаемый текст -> фильтр отбросит

function dump(...parts) {
  const chunks = [Buffer.alloc(64)];

  for (const part of parts) {
    chunks.push(
      part.wide ? Buffer.from(part.value, "utf16le") : Buffer.from(part.value, "ascii"),
      Buffer.alloc(64)
    );
  }

  return Buffer.concat(chunks);
}

const ascii = (value) => ({ value, wide: false });
const wide = (value) => ({ value, wide: true });

async function run(config) {
  const events = [];
  const logs = [];

  const { done } = startEngine(config, {
    enginePath,
    onEvent: (event) => events.push(event),
    onLog: (line) => logs.push(line),
  });

  const info = await done;

  return { info, events, logs, result: events.find((event) => event.type === "result") };
}

function itemsOf(result) {
  return new Map(result.items.map((item) => [item.s, item]));
}

async function main() {
  const python = findPython();
  assert(python, "Python 3.8+ не найден: без него движок не запустить");
  console.log(`Python ${python.version}: ${python.command}`);

  // Папка с кириллицей и пробелом: так бывает в путях Windows.
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "тест дампы "));
  const file = (name, buffer) => {
    const target = path.join(dir, name);
    fs.writeFileSync(target, buffer);
    return target;
  };

  const clean1 = file("clean1.dmp", dump(ascii(COMMON), ascii(TEXT)));
  const clean2 = file("clean2.dmp", dump(ascii(COMMON)));
  const cheat1 = file("cheat1.dmp", dump(ascii(COMMON), ascii(STABLE), ascii(ONCE), wide(WIDE), ascii(TEXT)));
  const cheat2 = file("cheat2.dmp", dump(ascii(COMMON), ascii(STABLE), wide(WIDE)));
  const cheat3 = file("cheat3.dmp", dump(ascii(COMMON), ascii(STABLE), wide(WIDE)));

  const base = { minLen: 8, minEntropy: 2.6, maxVowel: 0.3, minUnique: 4, minHits: null };

  /* --- 1. Параметры по умолчанию: строка обязана быть во всех cheat-дампах --- */
  {
    const config = validateRequest({ ...base, clean: [clean1, clean2], cheat: [cheat1, cheat2, cheat3] });
    const { info, events, result } = await run(config);

    assert.strictEqual(info.code, 0);
    assert(result, "нет события result");

    const found = itemsOf(result);

    assert(found.has(STABLE), "стабильная строка должна найтись");
    assert(found.has(WIDE), "UTF-16 строка должна найтись");
    assert(!found.has(COMMON), "строка из clean-дампов должна быть исключена");
    assert(!found.has(ONCE), "строка только из одного cheat-дампа не проходит при minHits=все");
    assert(!found.has(TEXT), "читаемый текст должен быть отфильтрован");

    assert.strictEqual(found.get(STABLE).e, 1, "STABLE найдена как ASCII");
    assert.strictEqual(found.get(WIDE).e, 2, "WIDE найдена как UTF-16");
    assert.strictEqual(found.get(STABLE).h, 3, "STABLE найдена в 3 cheat-дампах");
    assert.strictEqual(result.total, 2);
    assert.strictEqual(result.truncated, false);

    const done = events.filter((event) => event.type === "file_done");
    assert.strictEqual(done.length, 5, "5 файлов -> 5 событий file_done");
    assert(events.some((event) => event.type === "start"));

    // Общий прогресс приходит вместе с завершением каждого файла и доходит до 100%.
    assert(done.every((event) => typeof event.overall === "number" && event.overall >= 0 && event.overall <= 1));
    assert.strictEqual(Math.max(...done.map((event) => event.overall)), 1, "после последнего файла общий прогресс 100%");

    console.log("ok: параметры по умолчанию");
  }

  /* --- 2. minHits=1: подхватывается и строка из одного дампа --- */
  {
    const config = validateRequest({ ...base, minHits: 1, clean: [clean1, clean2], cheat: [cheat1, cheat2, cheat3] });
    const { result } = await run(config);
    const found = itemsOf(result);

    assert(found.has(ONCE), "при minHits=1 строка из одного дампа должна найтись");
    assert.strictEqual(found.get(ONCE).h, 1);

    // Порядок движка: больше попаданий -> выше.
    assert.strictEqual(result.items[result.items.length - 1].s, ONCE);

    console.log("ok: minHits=1");
  }

  /* --- 3. Один cheat-дамп --- */
  {
    const config = validateRequest({ ...base, clean: [clean1, clean2], cheat: [cheat1] });
    const { result } = await run(config);
    const found = itemsOf(result);

    assert(found.has(STABLE) && found.has(ONCE) && found.has(WIDE));
    assert.strictEqual(result.total, 3);

    console.log("ok: один cheat-дамп");
  }

  /* --- 4. Ограничение --limit проверяется через CLI-совместимый режим --- */
  {
    const { spawnSync } = require("child_process");
    const cli = spawnSync(
      python.command,
      [enginePath, "--clean", clean1, clean2, "--cheat", cheat1, cheat2, cheat3],
      { encoding: "utf8", env: { ...process.env, PYTHONIOENCODING: "utf-8" } }
    );

    assert.strictEqual(cli.status, 0);
    assert.deepStrictEqual(
      cli.stdout.trim().split("\n").sort(),
      [STABLE, WIDE].sort(),
      "CLI без --json печатает строки как раньше"
    );

    console.log("ok: CLI без --json совместим со старым поведением");
  }

  /* --- 5. Валидация --- */
  {
    assert.throws(() => validateRequest({ ...base, clean: [clean1], cheat: [cheat1] }), /минимум 2/);
    assert.throws(() => validateRequest({ ...base, clean: [clean1, "relative.dmp"], cheat: [cheat1] }), /абсолютным/);
    assert.throws(() => validateRequest({ ...base, clean: [clean1, clean2], cheat: [clean2] }), /и как clean, и как cheat/);
    assert.throws(() => validateRequest({ ...base, clean: [clean1, path.join(dir, "нет.dmp")], cheat: [cheat1] }), /не найден/);
    assert.throws(() => validateRequest({ ...base, minLen: 0, clean: [clean1, clean2], cheat: [cheat1] }), /длина/);
    assert.throws(() => validateRequest({ ...base, minHits: 5, clean: [clean1, clean2], cheat: [cheat1] }), /Минимум cheat-дампов/);
    assert.throws(() => validateRequest({ ...base, clean: [clean1, dir], cheat: [cheat1] }), /не файл/);

    console.log("ok: валидация запроса");
  }

  /* --- 6. Отмена --- */
  {
    // ~40 МБ печатаемых символов: движку есть чем заняться.
    const big = Buffer.alloc(40 * 1024 * 1024);
    for (let i = 0; i < big.length; i += 1) big[i] = 33 + ((i * 7919 + (i >> 3)) % 90);

    const bigFile = file("big.dmp", big);
    const config = validateRequest({ ...base, clean: [bigFile, clean1], cheat: [cheat1] });

    const running = startEngine(config, { enginePath, onEvent: () => {}, onLog: () => {} });
    setTimeout(() => running.cancel(), 300);

    const info = await running.done;
    assert.strictEqual(info.cancelled, true);

    console.log("ok: отмена анализа");

    // Параллельный режим: после отмены не должно остаться рабочих процессов.
    if (process.platform === "linux") {
      const { spawnSync } = require("child_process");

      const descendants = (pid) => {
        const found = [];
        const out = spawnSync("pgrep", ["-P", String(pid)], { encoding: "utf8" }).stdout;

        for (const child of out.split(/\s+/).filter(Boolean)) {
          found.push(child, ...descendants(child));
        }

        return found;
      };

      // Живой процесс: есть в /proc и не «зомби» (зомби уже не работает).
      const alive = (pid) => {
        try {
          const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
          return stat.slice(stat.lastIndexOf(")") + 2, stat.lastIndexOf(")") + 3) !== "Z";
        } catch {
          return false;
        }
      };

      // Много коротких строк: проверка вхождения занимает секунды и рабочие живут достаточно долго.
      const unit = Buffer.concat([Buffer.from("aZ9#qP!x7LwRt5$mK2&vBn"), Buffer.alloc(1)]);
      const busy = Buffer.alloc(30 * 1024 * 1024);
      for (let i = 0; i + unit.length <= busy.length; i += unit.length) unit.copy(busy, i);

      const busyCheat = file("busy_cheat.dmp", busy);
      const busyClean = file("busy_clean.dmp", busy);

      const many = validateRequest({ ...base, jobs: 3, clean: [busyClean, clean1], cheat: [cheat1, busyCheat] });
      const parallel = startEngine(many, { enginePath, onEvent: () => {}, onLog: () => {} });

      // Ждём, пока поднимутся рабочие процессы (до 20 секунд).
      let workers = [];
      for (let i = 0; i < 100 && workers.length < 3; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 200));
        workers = descendants(parallel.pid);
      }
      assert(workers.length >= 3, `тест бессмыслен: рабочих процессов нет (${workers.length})`);

      parallel.cancel();
      await parallel.done;
      await new Promise((resolve) => setTimeout(resolve, 1500));

      const survivors = workers.filter(alive);
      assert.deepStrictEqual(survivors, [], `после отмены живы процессы: ${survivors}`);

      console.log(`ok: после отмены не осталось процессов-сирот (было потомков: ${workers.length})`);
    }
  }

  /* --- 7. YARA --- */
  {
    const rule = buildYaraRule(
      [
        { value: STABLE, enc: 1 },
        { value: WIDE, enc: 2 },
        { value: 'quote"and\\slash', enc: 3 },
        { value: STABLE, enc: 1 },          // дубликат
        { value: "кириллица", enc: 1 },      // не ASCII -> пропуск
      ],
      { ruleName: "1 bad-name!", minMatches: 99, date: "2026-09-21" }
    );

    assert.strictEqual(rule.count, 3);
    assert.strictEqual(rule.skipped, 2);
    assert.strictEqual(rule.ruleName, "r_1_bad_name_");
    assert.strictEqual(rule.minMatches, 3, "minMatches ограничивается числом строк");
    assert(rule.text.includes(`$s1 = "${STABLE}" ascii`));
    assert(rule.text.includes(`$s2 = "${WIDE}" wide`));
    assert(rule.text.includes('"quote\\"and\\\\slash" ascii wide'));
    assert(rule.text.includes("3 of them"));

    assert.throws(() => buildYaraRule([], {}), /Нет строк/);

    fs.writeFileSync(path.join(dir, "rule.yar"), rule.text);
    console.log(`ok: YARA (правило сохранено в ${path.join(dir, "rule.yar")})`);
    console.log(rule.text);
  }

  console.log("Все проверки пройдены.");
}

main().catch((error) => {
  console.error("ПРОВАЛ:", error);
  process.exit(1);
});
