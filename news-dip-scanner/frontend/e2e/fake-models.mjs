#!/usr/bin/env node
/**
 * Stand-in language models for the end-to-end run: OpenAI's Chat Completions (POST /v1/chat/completions) and
 * Anthropic's Messages (POST /v1/messages) on one local port, so the Fly app's real SDKs can run a whole debate
 * without keys or cost. Point the Fly app at it with
 *
 *   OPENAI_BASE_URL=http://127.0.0.1:8099/v1  ANTHROPIC_BASE_URL=http://127.0.0.1:8099
 *   OPENAI_API_KEY=sk-e2e  ANTHROPIC_API_KEY=sk-ant-e2e  LLM_ANALYSIS_MODE=debate
 *
 * Every answer is built from the price in the prompt ("... trades at 12.71 USD"). The GPT model reads a temporary
 * fear at 72%, the Claude model fundamental damage at 44%, so the openings disagree and the rebuttal and the judge
 * run too; the texts say that no language model was asked. GET / lists the calls so far.
 *
 *   node e2e/fake-models.mjs [port]      (default 8099)
 */
import { createServer } from "node:http";

const port = Number(process.argv[2] ?? process.env.FAKE_MODELS_PORT ?? 8099);
const calls = [];

function answer(model, system, prompt) {
  const match = /trades at ([\d.]+)/.exec(prompt);
  const price = match ? Number(match[1]) : 100;
  const round = (value) => Math.round(value * 100) / 100;
  const gpt = /gpt/i.test(model);
  const base = {
    verdict: gpt ? "temporary_fear" : "fundamental",
    probability_up_6m: gpt ? 72 : 44,
    potential_low: round(price * (gpt ? 0.88 : 0.8)),
    entry_price: round(price * 0.96),
    target_price: round(price * 1.14),
    confidence: "medium",
    fear: "Stand-in for the end-to-end test: what the market fears.",
    fundamental_impact: "Stand-in for the end-to-end test: the impact on the business.",
    thesis: "Stand-in thesis for the end-to-end test; no language model was asked.",
    risks: ["A stand-in risk"],
    catalysts: ["A stand-in catalyst"],
    checks: ["A stand-in check"],
  };
  const text = `${system}\n${prompt}`;
  let step = "opening";
  let reply = base;
  if (text.includes('"debate_summary"')) {
    step = "judge";
    reply = {
      ...base,
      verdict: "mixed",
      probability_up_6m: 59,
      debate_summary: "Stand-in ruling: both analysts moved to a mixed verdict after the rebuttal.",
      agreement: "medium",
      favoured: "A",
    };
  } else if (text.includes('"changed_mind"')) {
    step = "rebuttal";
    reply = {
      ...base,
      verdict: "mixed",
      probability_up_6m: gpt ? 66 : 52,
      critique: ["The other analyst's target isn't supported by the input."],
      concessions: ["The drop came on heavy volume."],
      changed_mind: true,
    };
  }
  calls.push(`${step} ${model}`);
  console.log(`${new Date().toISOString()} ${step.padEnd(8)} ${model}`);
  return JSON.stringify(reply);
}

const flatten = (content) =>
  typeof content === "string" ? content : (content ?? []).map((block) => block.text ?? "").join("");

function send(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(data) });
  res.end(data);
}

createServer((req, res) => {
  if (req.method === "GET") return send(res, 200, { calls });
  let raw = "";
  req.on("data", (chunk) => (raw += chunk));
  req.on("end", () => {
    const body = raw ? JSON.parse(raw) : {};
    const model = String(body.model ?? "");
    if (req.url?.endsWith("/chat/completions")) {
      const system = flatten(body.messages?.find((m) => m.role === "system")?.content);
      const prompt = flatten(body.messages?.find((m) => m.role === "user")?.content);
      return send(res, 200, {
        id: "chatcmpl-e2e",
        object: "chat.completion",
        created: Math.floor(Date.now() / 1000),
        model,
        choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: answer(model, system, prompt) } }],
        usage: { prompt_tokens: 3500, completion_tokens: 900, total_tokens: 4400 },
      });
    }
    if (req.url?.endsWith("/messages")) {
      const system = flatten(body.system);
      const prompt = flatten(body.messages?.[0]?.content);
      return send(res, 200, {
        id: "msg_e2e",
        type: "message",
        role: "assistant",
        model,
        content: [{ type: "text", text: answer(model, system, prompt) }],
        stop_reason: "end_turn",
        stop_sequence: null,
        usage: { input_tokens: 3600, output_tokens: 950 },
      });
    }
    send(res, 404, { error: { message: `No such endpoint: ${req.url}` } });
  });
}).listen(port, "127.0.0.1", () => console.log(`Stand-in models on http://127.0.0.1:${port}`));
