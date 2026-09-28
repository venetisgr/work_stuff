// The API contract: every mock fixture validates against contract/api-v1.schema.json, every endpoint has a fixture,
// the schema refuses what it should, and the computed fields of the fixtures follow the rules the Fly app computes
// them by (so the fixtures are a worked example for both sides).
//
// CONTRACT_SAMPLES=<folder> runs the same checks over answers saved from a running Fly app too (named like the mocks:
// me.json, ideas-page2.json, idea-7.json, error-404.json...), e.g. `CONTRACT_SAMPLES=../samples npm test`.
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import { describe, expect, it } from "vitest";
import type { IdeaDetail, IdeaSummary, IdeasList } from "@/lib/types";

const CONTRACT = join(__dirname, "..", "contract");
const MOCKS = join(CONTRACT, "mocks");
const schema = JSON.parse(readFileSync(join(CONTRACT, "api-v1.schema.json"), "utf8"));

function makeAjv() {
  const ajv = new Ajv2020({ strict: true, allErrors: true });
  addFormats(ajv);
  ajv.addKeyword("x-endpoints");
  ajv.addSchema(schema);
  return ajv;
}

const ajv = makeAjv();

function validator(definition: string) {
  const validate = ajv.getSchema(`${schema.$id}#/$defs/${definition}`);
  if (!validate) throw new Error(`No definition ${definition}`);
  return validate;
}

function errorsOf(definition: string, data: unknown): string[] {
  const validate = validator(definition);
  return validate(data) ? [] : (validate.errors ?? []).map((e) => `${e.instancePath} ${e.message}`);
}

/** Which definition a fixture is an example of, by its file name. */
function definitionFor(file: string): string {
  const rules: [RegExp, string][] = [
    [/^me(-.+)?\.json$/, "Me"],
    [/^status(-.+)?\.json$/, "Status"],
    [/^ideas(-.+)?\.json$/, "IdeasList"],
    [/^idea-\d+\.json$/, "IdeaDetail"],
    [/^reanalyse\.json$/, "ReanalyseAccepted"],
    [/^job-.+\.json$/, "Job"],
    [/^thesis-changes\.json$/, "ThesisChanges"],
    [/^error-.+\.json$/, "Error"],
  ];
  const rule = rules.find(([pattern]) => pattern.test(file));
  if (!rule) throw new Error(`${file}: no definition for this file name`);
  return rule[1];
}

const jsonFiles = (folder: string) =>
  readdirSync(folder)
    .filter((name) => name.endsWith(".json"))
    .sort();
const files = jsonFiles(MOCKS);
const load = <T>(file: string, folder = MOCKS): T => JSON.parse(readFileSync(join(folder, file), "utf8")) as T;
const SAMPLES = process.env.CONTRACT_SAMPLES ? resolve(process.env.CONTRACT_SAMPLES) : null;

describe("the schema", () => {
  it("compiles in strict mode", () => {
    expect(() => makeAjv()).not.toThrow();
  });

  it("maps every endpoint to a definition that exists", () => {
    const endpoints: Record<string, string> = schema["x-endpoints"];
    expect(Object.keys(endpoints).length).toBeGreaterThanOrEqual(8);
    for (const ref of Object.values(endpoints)) {
      const name = ref.replace("#/$defs/", "");
      expect(schema.$defs[name], ref).toBeDefined();
    }
  });

  it("has a fixture for every endpoint's definition", () => {
    const covered = new Set(files.map(definitionFor));
    for (const ref of Object.values<string>(schema["x-endpoints"])) {
      expect(covered, ref).toContain(ref.replace("#/$defs/", ""));
    }
  });
});

describe("mock fixtures", () => {
  it.each(files)("%s validates", (file) => {
    expect(errorsOf(definitionFor(file), load(file))).toEqual([]);
  });

  const idea = load<IdeaDetail>("idea-7.json");

  it("refuses formatted strings where numbers belong", () => {
    const broken = structuredClone(idea);
    (broken.idea.price as unknown as { amount: string }).amount = "$751.66";
    expect(errorsOf("IdeaDetail", broken).join()).toMatch(/must be number/);
  });

  it("refuses a missing key, even a nullable one", () => {
    const broken = structuredClone(idea) as unknown as Record<string, unknown>;
    delete broken.prices_problem;
    expect(errorsOf("IdeaDetail", broken).join()).toMatch(/must have required property 'prices_problem'/);
  });

  it("refuses unknown keys in computed objects", () => {
    const broken = structuredClone(idea);
    (broken.idea as unknown as Record<string, unknown>).price_text = "$751.66";
    expect(errorsOf("IdeaDetail", broken).join()).toMatch(/must NOT have additional properties/);
  });

  it("refuses dates without an offset and bad verdicts", () => {
    const broken = structuredClone(idea);
    broken.idea.created = "28/09/2026";
    (broken.idea as unknown as { verdict: string }).verdict = "buy";
    const errors = errorsOf("IdeaDetail", broken).join();
    expect(errors).toMatch(/must match format "date-time"/);
    expect(errors).toMatch(/must be equal to one of the allowed values/);
  });

  it("accepts an old record without a debate", () => {
    const old = structuredClone(idea);
    delete old.opportunity.debate;
    old.debate = null;
    old.idea.debate = null;
    expect(errorsOf("IdeaDetail", old)).toEqual([]);
  });
});

// --- the computed fields follow the Fly app's rules -----------------------------------------------------------------

function band(score: number): string {
  if (score >= 80) return "strong";
  if (score >= 65) return "good";
  if (score >= 50) return "fair";
  return "weak";
}

const pct = (value: number, base: number) => (value / base - 1) * 100;

function checkSummary(summary: IdeaSummary) {
  expect(summary.score_band).toBe(band(summary.score));
  expect(summary.upside_pct).toBeCloseTo(pct(summary.target.amount, summary.price.amount), 2);
  expect(summary.downside_pct).toBeCloseTo(pct(summary.potential_low.amount, summary.price.amount), 2);
  expect(summary.entry_upside_pct).toBeCloseTo(pct(summary.target.amount, summary.entry.amount), 2);
  expect(summary.entry_downside_pct).toBeCloseTo(pct(summary.potential_low.amount, summary.entry.amount), 2);
  for (const money of [summary.price, summary.entry, summary.target, summary.potential_low, summary.stat_low]) {
    if (summary.fx) expect(money.approx).toBeCloseTo(money.amount * summary.fx.rate, 3);
    else expect(money.approx).toBeNull();
  }
  expect(summary.superseded).toBe(summary.superseded_by !== null);
  if (summary.debate) expect(summary.debate.final_probability).toBe(summary.probability_up_6m);
}

function checkList(list: IdeasList) {
  list.ideas.forEach(checkSummary);
  expect(list.count).toBeLessThanOrEqual(list.total);
  expect(list.ideas.length).toBeLessThanOrEqual(list.page.size);
  if (list.filters.sort === "score") {
    const scores = list.ideas.map((item) => item.score);
    expect(scores).toEqual([...scores].sort((a, b) => b - a));
  }
}

function checkDetail(detail: IdeaDetail) {
  checkSummary(detail.idea);
  expect(detail.opportunity.id).toBe(detail.idea.id);
  expect(detail.opportunity.analysis.probability_up_6m).toBe(detail.idea.probability_up_6m);
  const values = detail.levels.map((level) => level.value.amount);
  expect(values).toEqual([...values].sort((a, b) => b - a));
  expect(new Set(detail.levels.map((level) => level.key)).size).toBe(5);
  expect(detail.history.filter((item) => item.current)).toHaveLength(1);
  expect(detail.history.find((item) => item.current)?.id).toBe(detail.idea.id);
  expect(detail.chart === null).toBe(detail.prices_problem !== null);
  if (detail.chart) {
    const days = detail.chart.closes.map(([day]) => day);
    expect(days).toEqual([...days].sort());
  }
  expect(detail.idea.matches_my_rules).toBe(detail.rule_misses.length === 0);
  if (detail.debate) {
    expect(detail.opportunity.debate?.mode).toBe(detail.debate.mode);
    const count = detail.debate.mode === "single" ? 1 : 2;
    expect(detail.debate.participants).toHaveLength(count);
    expect(detail.idea.debate?.line).toBe(detail.debate.line);
    expect(detail.debate.title).toBe(detail.debate.mode === "single" ? "The models" : "The debate");
    expect(detail.debate.ruling_title === null).toBe(detail.debate.mode === "single");
    const labels = detail.debate.participants.map((side) => side.model_label);
    for (const side of detail.debate.participants) {
      expect(side.favoured).toBe(detail.debate.favoured === side.model);
      expect(side.compare).toBe(detail.debate.mode === "debate" && detail.debate.rounds > 0);
      expect(side.other_label).toBe(labels.find((label) => label !== side.model_label) ?? null);
    }
  } else {
    expect(detail.idea.debate).toBeNull();
  }
}

describe("computed fields of the fixtures", () => {
  it.each(files.filter((file) => /^ideas(-.+)?\.json$/.test(file)))("%s: summaries", (file) => {
    checkList(load<IdeasList>(file));
  });

  it.each(files.filter((file) => /^idea-\d+\.json$/.test(file)))("%s: detail", (file) => {
    checkDetail(load<IdeaDetail>(file));
  });
});

describe.runIf(SAMPLES)("answers saved from a running Fly app (CONTRACT_SAMPLES)", () => {
  const samples = SAMPLES ? jsonFiles(SAMPLES) : [];

  it("has some", () => {
    expect(samples.length).toBeGreaterThan(0);
  });

  it.each(samples)("%s validates and its computed fields add up", (file) => {
    const definition = definitionFor(file);
    const data = load<unknown>(file, SAMPLES ?? MOCKS);
    expect(errorsOf(definition, data)).toEqual([]);
    if (definition === "IdeasList") checkList(data as IdeasList);
    if (definition === "IdeaDetail") checkDetail(data as IdeaDetail);
  });
});
