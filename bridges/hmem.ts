// JSON-lines bridge from the Python service to the vendored Meterless H-MEM reference engine.
// One request per line on stdin, one response per line on stdout:
//   {"id":1,"op":"add","content":"...","tags":["..."],"source":"nirvana"}
//   {"id":2,"op":"query","text":"...","topN":5,"threshold":0.05}
//   {"id":3,"op":"feedback","memoryId":"mem-1","kind":"helpful"}
//   {"id":4,"op":"stats"}
// The engine itself is used unchanged from vendor/meterless/hmem (see vendor/meterless/UPSTREAM.md).

import readline from "node:readline";
import { HMEM } from "../vendor/meterless/hmem/src/index.ts";

const persistDir = process.argv[2];
if (!persistDir) {
  process.stderr.write("usage: hmem.ts <persistDir>\n");
  process.exit(2);
}
const hmem = new HMEM({ persistDir, actor: "nirvana" });

type Req = { id: number; op: string; [k: string]: unknown };

function handle(req: Req): unknown {
  switch (req.op) {
    case "add": {
      const rec = hmem.add({
        content: String(req.content),
        tags: Array.isArray(req.tags) ? req.tags.map(String) : [],
        source: String(req.source ?? "nirvana"),
        type: "factual",
        confidence: typeof req.confidence === "number" ? req.confidence : undefined,
      });
      return { id: rec.id, layer: rec.layer, confidence: rec.confidence, domain: rec.domain };
    }
    case "query": {
      const res = hmem.query(String(req.text), {
        topN: typeof req.topN === "number" ? req.topN : 5,
        threshold: typeof req.threshold === "number" ? req.threshold : undefined,
      });
      return {
        memories: res.memories.map((m) => ({
          id: m.memory.id,
          content: m.memory.content,
          tags: m.memory.tags,
          relevance: m.relevance,
          confidence: m.memory.confidence,
        })),
        reason: res.trace.retrievalReason,
      };
    }
    case "feedback": {
      hmem.feedback(String(req.memoryId), req.kind as "helpful" | "not_helpful" | "wrong");
      return { ok: true };
    }
    case "stats": {
      return { memories: hmem.store.all().length };
    }
    default:
      throw new Error(`unknown op ${req.op}`);
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let req: Req;
  try {
    req = JSON.parse(line);
  } catch {
    process.stdout.write(JSON.stringify({ id: null, error: "bad json" }) + "\n");
    return;
  }
  try {
    process.stdout.write(JSON.stringify({ id: req.id, result: handle(req) }) + "\n");
  } catch (e) {
    process.stdout.write(JSON.stringify({ id: req.id, error: String((e as Error).message ?? e) }) + "\n");
  }
});
process.stdout.write(JSON.stringify({ ready: true }) + "\n");
