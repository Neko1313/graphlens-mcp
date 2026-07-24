# graphlens (движок): недостроенные рёбра графа — сводный отчёт

> **СТАТУС: всё закрыто в `graphlens 0.8.2`** (ядро + плагины `graphlens-rust`,
> `graphlens-typescript`, `graphlens-go` → 0.8.2, `graphlens-python` → 0.8.0).
> Проверено перезамером на бенчмарке — см. таблицу «Верификация 0.8.2» ниже.
> Документ сохранён как запись найденного и как чек-лист фикса.

Отчёт собран из двух независимых источников, которые сходились на одних и тех же
пробелах резолвинга:

1. **Реальное использование агентом** на приватных репозиториях `repo-judge`
   (**hub** — TypeScript + React, FSD; **fbr**) через `graphlens-mcp`.
2. **A/B-бенчмарк** `graphlens-mcp/benchmarks` на 10 OSS-репозиториях — даёт
   количественные замеры покрытия.

> Слой ответственности: `graphlens-mcp` только персистит и отдаёт то, что вернул
> движок. Все пункты ниже — постройка рёбер, то есть **движок**. Единственный
> пункт, чинившийся на стороне MCP (выдача имён неразрешённых целей), помечен.

---

## Верификация 0.8.2 (перезамер)

| Issue | было (0.7.0) | стало (0.8.2) | проверка |
|---|---|---|---|
| TS barrel / arrow-const экспорты | `getPath` callers = **0** | **3** | hono |
| TS резолвинг вызовов | call-targets **32 / 583** | **371 / 445** | hono |
| Rust `inherits_from` (implementors) | **0** рёбер | **228** | ripgrep |
| Go `references` | **0** рёбер | **2819** | gin (с gopls) |
| TS type-refs (`has_type`) | 693 | **1145** | hono |

Эффект на HARD-задачах бенчмарка (deepseek): impact-спирали, что упирались в эти
gaps, резко подешевели — напр. `hono_impact_getpath` **486k → 111k** токенов,
`rg_impl_sink_printer` **265k → 92k**, `zod_impact_geterrormap` **365k → 61k**.

---

## Issue 1 — TS: рёбра «использование типа» ✅ FIXED 0.8.2

Аннотации типом не давали ребра `references` (поля интерфейсов, сигнатуры,
аннотации переменных). У `Group`, `Conversation`, `GroupRole` было
`references_total: 0`. → 0.8.2 строит type-рёбра (`has_type` 693→1145).

## Issue 2 — TS: barrel-реэкспорты → ложный «мёртвый код» ✅ FIXED 0.8.2

`weeklyTrend` через `@/shared/lib/echarts` (barrel) не резолвился —
`callers_total: 0` для живой функции. → 0.8.2 резолвит цепочку
`@/alias → index.ts → export from './file'`; на бенчмарке `getPath` callers 0→3,
call-targets 32→371.

## Issue 3 — неразрешённые цели без имён ✅ (MCP-сторона)

`callees_unresolved` отдавал только счётчик. `external_symbol`-узлы несут имя —
выдача чинится на стороне `graphlens-mcp` (перестать отбрасывать имя при
агрегации). От движка нужно лишь продолжать помечать неразрешённые цели
`external_symbol` с исходным именем.

## Rust `inherits_from` / Go `references` ✅ FIXED 0.8.2

Benchmark-находки (не из hub/fbr): Rust не строил `inherits_from` (implementors
трейтов невидимы — ripgrep `StandardSink implements Sink`), Go не строил
`references`. → 0.8.2: ripgrep inherits_from 0→228, gin references 0→2819.

---

## Прочее

- **PHP-резолвер `unavailable`** — php-файлы парсятся (узлы есть), 0 связей.
  Упомянуто для полноты, не как баг.
