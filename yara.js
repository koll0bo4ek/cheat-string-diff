"use strict";

/*
 * Генерация YARA-правила из списка найденных строк.
 *
 * Строки приходят из движка только в диапазоне printable ASCII (0x20-0x7e),
 * но мы всё равно проверяем это здесь: файл правила должен быть валидным
 * при любых входных данных.
 */

const ENC_ASCII = 1;
const ENC_UTF16 = 2;

const PRINTABLE_ASCII = /^[\x20-\x7e]+$/;

// Слова, которые YARA не разрешает использовать как имя правила.
const RESERVED = new Set([
  "all", "and", "any", "ascii", "at", "base64", "base64wide", "condition",
  "contains", "endswith", "entrypoint", "false", "filesize", "for", "fullword",
  "global", "import", "in", "include", "int8", "int16", "int32", "matches",
  "meta", "nocase", "not", "of", "or", "private", "rule", "startswith",
  "strings", "them", "true", "uint8", "uint16", "uint32", "wide", "xor",
]);

function sanitizeRuleName(name) {
  let result = String(name ?? "")
    .trim()
    .replace(/[^A-Za-z0-9_]/g, "_")
    .slice(0, 64);

  if (!result) result = "cheat_strings";
  if (!/^[A-Za-z_]/.test(result)) result = `r_${result}`;
  if (RESERVED.has(result.toLowerCase())) result = `${result}_rule`;

  return result;
}

function escapeYaraString(value) {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function modifiersFor(enc) {
  const ascii = (enc & ENC_ASCII) !== 0;
  const wide = (enc & ENC_UTF16) !== 0;

  if (ascii && wide) return "ascii wide";
  if (wide) return "wide";
  return "ascii";
}

/**
 * @param {{value: string, enc: number}[]} items
 * @param {{ruleName?: string, minMatches?: number, description?: string, date?: string}} options
 * @returns {{text: string, count: number, skipped: number, ruleName: string, minMatches: number}}
 */
function buildYaraRule(items, options = {}) {
  const seen = new Set();
  const usable = [];
  let skipped = 0;

  for (const item of items) {
    const value = item && typeof item.value === "string" ? item.value : "";

    if (!value || !PRINTABLE_ASCII.test(value) || seen.has(value)) {
      skipped += 1;
      continue;
    }

    seen.add(value);
    usable.push({ value, enc: Number.isInteger(item.enc) ? item.enc : ENC_ASCII });
  }

  if (usable.length === 0) {
    throw new Error("Нет строк, подходящих для YARA-правила.");
  }

  const ruleName = sanitizeRuleName(options.ruleName);

  const requested = Math.trunc(Number(options.minMatches));
  const minMatches = Math.min(
    Math.max(Number.isFinite(requested) ? requested : 1, 1),
    usable.length
  );

  const description = String(
    options.description || "Candidate strings from differential dump analysis"
  )
    .replace(/[\r\n]+/g, " ")
    .slice(0, 200);

  // Локальная дата: по UTC ночью в Европе получился бы вчерашний день.
  const now = new Date();
  const date =
    options.date ||
    `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const pad = String(usable.length).length;

  const lines = [];
  lines.push(`rule ${ruleName}`);
  lines.push("{");
  lines.push("    meta:");
  lines.push(`        description = "${escapeYaraString(description)}"`);
  lines.push(`        generated = "${date}"`);
  lines.push("    strings:");

  usable.forEach((item, index) => {
    const id = String(index + 1).padStart(pad, "0");
    lines.push(
      `        $s${id} = "${escapeYaraString(item.value)}" ${modifiersFor(item.enc)}`
    );
  });

  lines.push("    condition:");
  lines.push(`        ${minMatches} of them`);
  lines.push("}");

  return {
    text: `${lines.join("\n")}\n`,
    count: usable.length,
    skipped,
    ruleName,
    minMatches,
  };
}

module.exports = { buildYaraRule, sanitizeRuleName, escapeYaraString };
