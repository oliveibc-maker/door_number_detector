# Feasibility runbook: llama-server + Qwen2.5-VL-3B on WSTPPORT402

Date: 2026-11-10
Scope: manual, one-time feasibility test on the approved inference target. No Street
View calls. No changes to the app default (`DETECTION_STRATEGY=sweep` stays).
Source of decisions: `docs/research/local-vlm-runtime-comparison-2026-11-10.md`.

## What this validates

1. The runtime installs and the model loads on Windows Server 2019, CPU-only.
2. RSS stays within budget (~3–4 GB) while SQL Server runs.
3. A schema/JSON-constrained planner call returns valid, parseable JSON.
4. Disconnecting the HTTP client mid-request actually frees the CPU.
5. Per-call latency is measurable (expected: tens of seconds on 8 cores).

## Prerequisites

- RDP access to WSTPPORT402 with a local admin-capable account.
- ~6 GB free disk for the runtime and model files.
- Internet access from 402 to `github.com` and `huggingface.co` **once**, to
  download the binary and the model. Inference itself is fully local afterwards.
- Do not run this on WSTPPORT401 (source DB host) — 402 only.

## Step 1 — Download llama.cpp (Windows CPU build)

1. Open <https://github.com/ggml-org/llama.cpp/releases>.
2. From the latest release, download the Windows x64 CPU asset, e.g.
   `llama-<version>-bin-win-cpu-x64.zip` (CPU-only; do **not** take CUDA/Vulkan
   builds — 402 has no GPU).
3. Extract to `C:\llama\` (contains `llama-server.exe`).
4. Sanity check in `cmd`:
   ```cmd
   C:\llama\llama-server.exe --version
   ```

## Step 2 — Download the model (Qwen2.5-VL-3B-Instruct GGUF)

Two files are needed: the model and its multimodal projector (`mmproj`).

1. Open <https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF/tree/main>.
2. Download into `C:\llama\models\`:
   - the Q4_K_M model GGUF (about 2.1 GB), and
   - the matching `mmproj-*.gguf` file (about 0.9 GB).
3. Note the exact filenames — they are needed in the start command.

## Step 3 — Start the server (single-model mode, loopback only)

From `cmd` (adjust filenames to what you downloaded):

```cmd
C:\llama\llama-server.exe ^
  -m C:\llama\models\Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf ^
  --mmproj C:\llama\models\mmproj-Qwen2.5-VL-3B-Instruct-f16.gguf ^
  --host 127.0.0.1 --port 8080 ^
  -c 8192 -np 1 --threads 8 ^
  --cache-type-k q8_0 --cache-type-v q8_0 ^
  --no-mmproj-offload --no-webui ^
  --image-max-tokens 1280
```

Notes:

- `--host 127.0.0.1` is required — the app refuses non-loopback endpoints, and the
  server must never be reachable off-host.
- `-np 1` keeps a single inference slot; the guided strategy is one job at a time.
- `q8_0` KV cache halves context memory with negligible quality loss.
- `--no-mmproj-offload` keeps the projector on CPU (there is no GPU anyway).
- `--image-max-tokens 1280` caps prompt size for 640×640 Street View crops; raise
  only if reads are starved of resolution.
- First load takes minutes; wait for the "server is listening" log line.

## Step 4 — The four checks

Use `curl.exe` (ships with Windows) or PowerShell `Invoke-RestMethod`. A
ready-made contract script also exists in the repo: `scripts/check_local_vlm.py`
(run it from the dev box against the 402 tunnel, or copy it to 402).

### 4a. Health and memory baseline

```cmd
curl http://127.0.0.1:8080/health
```

Then in Task Manager (Details tab) note the `llama-server.exe` working set with
the model loaded but idle. Record: idle RSS = ____ MB.

### 4b. JSON-constrained planner-shaped call

Send a request shaped like the app's planner (schema for one closed action) with a
small synthetic image (any JPEG with large printed digits, e.g. generated in Paint):

```cmd
curl -s http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" -d @planner_test.json
```

where `planner_test.json` contains:

```json
{
  "model": "qwen2.5-vl-3b",
  "temperature": 0,
  "max_tokens": 256,
  "response_format": {"type": "json_object"},
  "messages": [
    {"role": "system", "content": "Inspect the current Street View image for a door-number search. Return exactly one JSON action. Allowed action shapes: inspect_region with box and fov; adjust_view with heading, pitch, and fov; candidate with observation_ids; move_panorama with panorama_id; stop with reason search_complete. Do not output coordinates, tools, or prose."},
    {"role": "user", "content": [
      {"type": "text", "text": "{\"view\":{\"heading\":0,\"pitch\":0,\"fov\":90},\"ocr_observations\":[],\"history\":[]}"},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<BASE64_JPEG>"}}
    ]}
  ]
}
```

Pass criteria:

- HTTP 200; `choices[0].message.content` parses as JSON (the app parses exactly
  this path).
- The JSON `"type"` is one of the allowed actions; fields match that action.
- Note latency from the server log (`prompt eval time`, `eval time`): ____ s.
- Note peak RSS during the call (Task Manager): ____ MB.

Repeat 5 times; all five must return parseable, in-schema JSON.

### 4c. Disconnect cancels compute

1. Start the same request with streaming (`"stream": true`) in one `cmd` window.
2. While tokens are streaming, close the client with `Ctrl+C` (or kill curl).
3. Watch the llama-server log for `srv stop: cancel task` (or similar) and watch
   CPU for `llama-server.exe` in Task Manager.

Pass criteria: the server logs the cancellation and CPU returns to idle within a
few seconds. If generation continues after disconnect, stop — this is a blocker
(see llama.cpp issue #24496; do not put any proxy in front of the server, and make
sure you are testing the direct loopback connection).

### 4d. Memory under SQL co-residency

1. With llama-server idle-loaded, run a representative SQL workload (or ask the DBA
   to), then re-check llama-server RSS and total host free memory.
2. Repeat 4b and confirm latency/RSS are stable.

Pass criteria: total host free memory stays > 2 GB at all times; llama-server RSS
stays ≲ 4 GB. If SQL grows and squeezes the model, record numbers and reopen the
DBA max-memory conversation (deferred decision).

## Step 5 — Wire-up values for the app (only after all checks pass)

In `.env` on 402 (never commit):

```
DETECTION_STRATEGY=guided
LOCAL_VLM_CHAT_URL=http://127.0.0.1:8080/v1/chat/completions
LOCAL_VLM_MODEL=qwen2.5-vl-3b
LOCAL_VLM_TIMEOUT_SECONDS=300
```

Keep `DETECTION_STRATEGY=sweep` everywhere else. Guided stays opt-in until the
issue-13 acceptance criteria are met on the labeled pilot.

## Step 6 — Service hardening (after feasibility is proven)

- Register as a Windows service with NSSM (`nssm install llamavlm
  C:\llama\llama-server.exe <args>`) or a scheduled task at startup, so the model
  survives logoff. A desktop session is not an acceptable home for it.
- Bind stays `127.0.0.1`; no firewall exception is needed or wanted.
- Model files live under `C:\llama\models\`; they are not secrets but keep them out
  of any synced/shared folder.

## Rollback

Stop the service/process and delete `C:\llama\`. Nothing else on 402 is touched.
