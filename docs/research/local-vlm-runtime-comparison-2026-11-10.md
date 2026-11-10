# Local VLM runtime/model comparison for the guided strategy (Issue 13)

Date: 2026-11-10
Status: direction decided 2026-11-10 (see below); nothing installed or run yet.

## Decision (2026-11-10, from user answers)

- Windows Server 2019 on 402 is a hard constraint; no other host, no OS upgrade.
  This eliminates Ollama (officially Windows 10 22H2+ Home/Pro only), vLLM, TGI,
  and LM Studio. **Runtime: llama.cpp `llama-server`.**
- Any licence is acceptable → no need for licence-driven substitutes; Qwen2.5-VL
  is usable directly.
- Tens of seconds per planner call is acceptable → CPU-only 3B-class inference is
  fine; a 7B is not ruled out on speed, only on memory.
- SQL Server memory question deferred: start with a ~3 GB model, monitor RSS and
  total host memory during the feasibility test, and only ask the DBA about
  lowering SQL max server memory if real pressure is observed.

**Selected for feasibility test: `llama-server` + Qwen2.5-VL-3B-Instruct Q4_K_M
(`ggml-org/Qwen2.5-VL-3B-Instruct-GGUF`), single-model mode (not router), loopback
only, `-c 8192`, `--cache-type-k q8_0`, capped `--image-max-tokens`.**
Escalation path if 3B plaque-reading accuracy is insufficient on the labeled pilot:
Qwen2.5-VL-7B-Instruct Q4_K_M, with the SQL memory question reopened using measured
RSS numbers.

## Scope and constraints

The guided strategy calls a local vision-language model (VLM) planner over async HTTP
(localhost) from inside the view/zoom loop, not only for a first look. Hard constraints:

- Target host `WSTPPORT402`: Windows Server 2019 Standard (64-bit, VMware), 32 GB RAM,
  Intel Xeon Gold 6248R, 8 logical CPUs, no GPU. CPU-only inference.
- SQL Server 2019 on the same host has configured max server memory 28,544 MB; a
  point-in-time snapshot showed only 585 MB in use and ~24 GB available, but SQL may
  grow toward its cap. Budget the VLM at **4–8 GB RAM maximum**, preferably ~4 GB.
- Local-only operation (no cloud calls at inference time), multimodal image input,
  structured JSON output, and predictable behaviour when the HTTP client disconnects
  (the planner already closes the async HTTP response on cancellation; whether the
  model server stops compute is runtime-specific).
- Detection success and Street View image spend matter more than tokens/second.

## Runtime comparison

| Runtime | Windows Server 2019 | CPU-only | Vision | OpenAI-compatible API | Structured JSON | Disconnect cancels compute | License | Setup |
|---|---|---|---|---|---|---|---|---|
| **llama.cpp `llama-server`** | Yes (prebuilt `llama-server.exe`, pure C/C++, no OS-version gate) | Yes (primary target; threads/affinity flags) | Yes (experimental `libmtmd`; mmproj GGUF) | Yes (`/v1/chat/completions`, `image_url` with base64/data-URI) | Yes (`response_format` json_schema, `--json-schema`, grammars) | Yes for direct connections (server cancels task on TCP close; see caveat below) | MIT | Single exe + model files; run as service via NSSM/Task Scheduler |
| **Ollama** | **No — officially "Windows 10 22H2 or newer, Home or Pro"**; Windows Server is unsupported (may run, not guaranteed; must validate) | Yes | Yes (`images` on `/api/chat`, vision model library) | Partial (`/v1/chat/completions` shim + native `/api/chat`) | Yes (`format` = `json` or JSON schema) | Yes — generation is tied to the request context; closing the connection cancels it (relies on the same llama.cpp-style machinery) | MIT (server); model licenses vary | Installer/zip, model pulls, `OLLAMA_NO_CLOUD=1` for local-only |
| **LM Studio** | No (desktop app, Windows 10/11; GUI-oriented, not a server OS fit) | Yes | Yes | Yes | Yes | Yes (JIT/offload on stream close) | **Proprietary freeware; commercial use terms restrict some org uses** | GUI-first; poor fit for unattended Server 2019 |
| **vLLM** | No (Linux-only; no native Windows support) | CPU backend experimental only | Yes | Yes | Yes (guided decoding) | n/a | Apache-2.0 | Not viable on the target |
| **HF TGI** | No (Linux containers; deprecated in favour of vLLM) | No practical CPU path | Yes | Yes (messages API) | Yes | n/a | Apache-2.0 (TGI 1.x/2.x; later versions moved to a different licence — check before use) | Not viable on the target |
| **LocalAI** | Windows builds exist but vision-on-CPU story is a thin wrapper over llama.cpp; extra moving parts | Yes | Yes | Yes | Yes | Inherits llama.cpp behaviour | MIT | More complexity than llama.cpp alone |
| **ONNX Runtime GenAI** | Yes (Windows 10+/Server via NuGet/zip) | Yes | Yes (Phi-3.5/4-vision, Gemma 3, Qwen2.5-VL support varies by release) | **No built-in HTTP server** — you write/host your own API layer | Via constraints in custom code | In your own code | MIT | Most engineering effort; only worth it if llama.cpp fails |

Sources: llama.cpp server README (tools/server/README.md, accessed 2026-11-10);
llama.cpp multimodal docs (docs/multimodal.md, accessed 2026-11-10);
Ollama API docs + FAQ + Windows docs (docs.ollama.com, accessed 2026-11-10);
Ollama vision library (ollama.com/search?c=vision, accessed 2026-11-10).

### llama.cpp multimodal notes

- Vision support lives in `libmtmd` and is marked **experimental**; it is available on
  the OpenAI-compatible chat endpoint. Pre-quantized GGUFs with `mmproj` files are
  published under `ggml-org` on Hugging Face (Gemma 3, SmolVLM/SmolVLM2, Qwen2-VL,
  Qwen2.5-VL 3B/7B/32B/72B, InternVL 2.5/3, Moondream2, Pixtral, Mistral Small 3.1).
- CPU-only is a first-class path: `--threads`, `--no-mmproj-offload`, KV-cache
  quantization (`--cache-type-k q8_0`), and `--ctx-size` bound memory.
- Structured output: `response_format` with `json_schema` on `/v1/chat/completions`
  constrains decoding to the schema (grammar-based), which matches the guided
  planner's closed action schema well.
- Image token budget can be capped with `--image-min-tokens` / `--image-max-tokens`
  for dynamic-resolution models (Qwen2.5-VL) — important to keep CPU prompt-eval
  time bounded.

### Ollama notes

- Official Windows support is "Windows 10 22H2 or newer, Home or Pro" — **Windows
  Server 2019 is outside the supported matrix** (docs.ollama.com/windows, accessed
  2026-11-10). The standalone `ollama-windows-amd64.zip` + `ollama serve` may work,
  but this must be validated on 402 before any commitment.
- Local-only mode: set `OLLAMA_NO_CLOUD=1` (or `disable_ollama_cloud` in
  `server.json`) to remove cloud features. Inference itself is always local.
- Memory control: `keep_alive`, `OLLAMA_NUM_PARALLEL` (default 1; RAM scales with
  `num_parallel * context`), `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_CONTEXT_LENGTH`.
- Default context is 4096; vision requests with large images need `num_ctx` raised,
  which multiplies KV memory on CPU.

## Cancellation / disconnect semantics

This matters because the guided planner closes the HTTP response when the user
cancels a job, and CPU inference on 8 cores is precious next to SQL Server.

- **llama.cpp `llama-server`**: cancellation on client disconnect is implemented
  (server logs `srv stop: cancel task` when the connection closes). Caveats found in
  the tracker:
  - Issue #24496 (Jun 2025) reported generation *not* cancelled with
    `is_connection_closed` never checked; closed as not-planned by maintainer
    (`ngxson`) stating direct-mode cancellation already works — the reporter's case
    involved an **API proxy** in between. Lesson: keep the planner's HTTP connection
    direct to `llama-server` on loopback; a proxy can break disconnect detection.
  - Router mode had a separate propagation gap ("Router wasn't propagating client
    disconnects fully", server-queue.cpp, ~3 weeks ago) — prefer **single-model
    mode**, not router mode.
  - There is no explicit "abort by request id" REST endpoint for regular
    completions; disconnect is the cancellation mechanism (plus
    `t_max_predict_ms` as a server-side backstop).
- **Ollama**: request context cancellation aborts generation (Ollama is built on
  llama.cpp and ties generation to the HTTP request context). No public abort API;
  `ollama stop` / `keep_alive: 0` unload the model, which also frees CPU.
- Either way, "closing the client request" and "backend stops compute" are only
  equivalent for **direct loopback connections** — validate this empirically on 402
  (cancel mid-request, watch CPU drop) as part of the feasibility test.

## Model comparison (CPU-viable sizes)

| Model | Params | Quant (GGUF) RAM estimate* | OCR/street-text strength | JSON reliability | Licence |
|---|---|---|---|---|---|
| **Qwen2.5-VL-3B-Instruct** | 3.8B | ~2.5–3.5 GB (Q4_K_M + mmproj) | Strong for size: TextVQA 76.8, DocVQA 91.6; dynamic resolution, bounding boxes, "stable JSON outputs" per model card | Schema-constrained via runtime; good instruction following | Apache-2.0 (3B is Apache; note Qwen's larger VL sizes use the Qwen licence — check per size) |
| **Qwen2.5-VL-7B-Instruct** | 8.3B | ~5.5–7 GB | Better (TextVQA 84.3) but tight against the 4–8 GB budget once KV cache and SQL growth are counted | Same | Qwen Research licence for 7B — **commercial terms need review before adoption** |
| **MiniCPM-V 2.6** | 8B (SigLip-400M + Qwen2-7B) | ~5.5–6.5 GB | Best-in-class OCRBench for its size; 1.8 MP images in 640 tokens (cheap prompt eval) | Good | Weights free for academic use; **commercial use requires registration questionnaire** (Feishu form) — procurement step |
| **Gemma 3 4B IT** | 4.3B | ~3.5–4.5 GB (also official QAT build for CPU) | Moderate: TextVQA 58.9 (val) — weakest of the shortlist for fine text; 896×896 fixed 256-token images keep cost predictable | Good | **Gemma Terms of Use** (custom, use-policy based; generally permissive but not OSI) — legal review |
| **SmolVLM2-2.2B-Instruct** | 2.2B | ~2–2.5 GB | Adequate for gross scene understanding; weak on small plaque text (OCRBench ~mid-60s family-level) | Acceptable | **Apache-2.0** — cleanest licence |
| **Moondream2 (20250414)** | ~2B | ~2 GB | Fast scene queries; limited fine OCR | Limited structured output | Business/usage terms differ by deployment size — check before use |
| **InternVL2.5-4B / InternVL3-2B** | 4B / 2B | ~3–4 GB / ~2 GB | Good OCR family; less battle-tested in llama.cpp than Qwen/Gemma | Good | MIT (InternVL weights) — verify per release |

\* RAM estimate = Q4_K_M GGUF weight size + mmproj + KV cache at ~4–8K context with
`--cache-type-k q8_0`. Add headroom for image encode spikes.

### Licence flag

For an internal municipal/company pilot, Apache-2.0 (Qwen2.5-VL-3B, SmolVLM2) and MIT
(runtimes) are the path of least resistance. MiniCPM-V 2.6 needs the OpenBMB
registration form; Qwen2.5-VL-7B and Gemma 3 need a terms review. Resolve this with
the owner before the feasibility test, not after.

## Memory-fit analysis for WSTPPORT402

- Host: 32 GB RAM. SQL Server configured cap 28,544 MB but observed at 585 MB in a
  single snapshot; available memory was ~24 GB at that time.
- Safe envelope for the VLM process: **≤ 6 GB RSS**, leaving SQL free to grow by
  ~5 GB before pressure and Windows itself ~2 GB.
- Fits comfortably: SmolVLM2-2.2B, Qwen2.5-VL-3B, Gemma 3 4B (QAT), InternVL3-2B —
  all ~2–4.5 GB.
- Fits only if SQL stays near its snapshot: Qwen2.5-VL-7B, MiniCPM-V 2.6 (~6–7 GB
  with context). These would need a DBA conversation about lowering SQL max memory
  or scheduling batch runs off-peak.
- Context length is the hidden multiplier on CPU: cap `num_ctx`/`-c` at 4–8K and cap
  image tokens (`--image-max-tokens` in llama.cpp) so prompt eval stays bounded.
- Rough CPU latency expectation on 8 Xeon cores: tens of seconds per planner call
  for a 3–4B model with one 640×640-class image. Acceptable because the planner is
  called a bounded number of times per address, but it makes disconnect-cancellation
  and per-call `max_tokens` limits essential.

## Shortlist (for discussion — no selection made)

1. **llama.cpp `llama-server` + Qwen2.5-VL-3B-Instruct (Q4_K_M)** — best overall fit:
   supported OS path (plain exe), experimental-but-working vision, real JSON-schema
   constraint, Apache-2.0 weights, ~3 GB RSS, strong text-in-image benchmarks.
   Risks: mtmd is experimental; Qwen2.5-VL GGUF vision quality at Q4 on small
   plaques is unproven — must be measured on the labeled pilot images.
2. **llama.cpp `llama-server` + Gemma 3 4B IT QAT** — the QAT build is designed for
   CPU, predictable 256-token image cost, good JSON following. Risks: weaker TextVQA
   than Qwen2.5-VL; Gemma terms need a legal once-over; fixed 896² encoder may miss
   small plaques unless the planner zooms.
3. **Ollama + qwen2.5vl:3b** — operationally simplest (model pulls, keep-alive,
   local-only flag) **if** it runs acceptably on Server 2019, which is officially
   unsupported. Treat as a convenience option pending an on-host smoke test.

Fallback if llama.cpp vision proves unstable on Windows: ONNX Runtime GenAI with a
custom thin HTTP wrapper (most work) or re-scope to text-only planning over OCR
observations (no VLM in the loop).

## Open questions for the user

1. Is Windows Server 2019 support hard (favours llama.cpp) or is upgrading/off-box
   inference on another host an option (reopens Ollama/vLLM)?
2. Licence posture: is the Qwen2.5-VL-3B Apache-2.0 licence acceptable as-is, and do
   we want to bother with the MiniCPM registration or Gemma terms at all for a pilot?
3. What is the acceptable per-planner-call latency budget (seconds? tens of seconds?)
   — this decides whether a 7B model is even on the table.
4. Can the DBA sanction a fixed SQL max-memory reduction, or must we assume growth to
   28.5 GB and cap the VLM at ~3 GB (which favours Qwen2.5-VL-3B or SmolVLM2)?

## Next step after a decision

Feasibility test on 402 with synthetic/approved local images only: install the chosen
runtime, load the chosen quant, verify (a) RSS stays in budget while SQL runs,
(b) a schema-constrained planner call returns valid JSON, (c) disconnecting the HTTP
client actually frees the CPU, (d) latency per call. No Street View calls; keep
`DETECTION_STRATEGY=sweep` as default throughout.
