# Model research for Edith's brain (free options)

Checked **2026-11-14** by the `desktop` worker (research only, no code changed).
Goal: an OpenAI-compatible chat-completions model that picks the right tool out of ~50 with valid JSON
arguments, at under ~5 s per voice turn, for free.

**How to read this:** facts come from the official page linked in each row (fetched today). Anything
marked **unconfirmed** could not be verified on an official page and needs a live probe before
it is shown in the setup wizard. Third-party blogs disagreed with each other a lot, so I used them only
where no official number exists, and labelled them as such.

## Token budget assumption (used for the "turns" estimates)

- 50 tool definitions + system prompt ≈ **~6,000 prompt tokens per request**. This is an estimate:
  the benchmark agent should log `usage.prompt_tokens` on the first call and replace this number.
- A typical Edith turn = **2 requests** (1: model emits a tool call, 2: model reads the tool result and
  answers), so **≈ 12-13k tokens per turn**. Multi-step turns cost more.
- Rule of thumb: a provider with **8K TPM** fits about **one** Edith request per minute.

---

## Summary table

| Provider | Model id | Free cost / limits | Tools | Thinking model | Fit for Edith | Source | Checked |
|---|---|---|---|---|---|---|---|
| Ollama (local) | `qwen3:4b` | free, 2.5 GB download | yes | yes (can be disabled) | **Best local pick for 6 GB GPU** | https://ollama.com/library/qwen3 | 2026-11-14 |
| Ollama | `qwen3:1.7b` | free, 1.4 GB | yes | yes (can be disabled) | CPU fallback candidate | https://ollama.com/library/qwen3 | 2026-11-14 |
| Ollama | `granite4:3b-h` | free, 1.9 GB, 1M ctx, hybrid Mamba-2 | yes | no | Good: small, no thinking delay, cheap long context | https://ollama.com/library/granite4/tags | 2026-11-14 |
| Ollama | `granite4:1b-h` | free, 1.6 GB | yes | no | CPU fallback candidate | https://ollama.com/library/granite4/tags | 2026-11-14 |
| Ollama | `lfm3:1.7b` | free, 1.1-1.7 GB, 256K ctx | yes | yes | **Best CPU-only candidate** (built for local agents) | https://ollama.com/library/lfm3 | 2026-11-14 |
| Ollama | `nemotron-3-nano:4b` | free, 2.8-2.9 GB | yes | yes | Good alternative to qwen3:4b | https://ollama.com/library/nemotron-3-nano | 2026-11-14 |
| Ollama | `llama3.2:3b` | free, 2.0 GB | yes | no | OK for simple single-tool routing; older | https://ollama.com/library/llama3.2 | 2026-11-14 |
| Ollama | `phi4-mini` (3.8b) | free, 2.5 GB | yes | no | Worth one test | https://ollama.com/library/phi4-mini | 2026-11-14 |
| Ollama | `qwen2.5:3b` / `qwen2.5:7b` | free, 1.9 GB / 4.7 GB | yes | no | Superseded by qwen3; 7b too big next to Whisper | https://ollama.com/library/qwen2.5 | 2026-11-14 |
| Ollama | `qwen3.5:4b` | free, 3.3-4.0 GB | yes | yes (+vision) | Too tight for a 4 GB budget once the context cache is added | https://ollama.com/library/qwen3.5 | 2026-11-14 |
| Ollama | `ministral-3:3b` | free, 3.0 GB | yes | no (+vision) | Possible; needs Ollama ≥0.13.1 | https://ollama.com/library/ministral-3 | 2026-11-14 |
| Ollama | `bonsai-2:4b` | free, 1.6 GB (ternary weights) | yes | yes (+vision) | Interesting but new/unproven; test only if time allows | https://ollama.com/library/bonsai-2 | 2026-11-14 |
| Ollama | `gemma3:*` | free | **no** (vision only) | no | **Not usable** (no tools badge) | https://ollama.com/library/gemma3 | 2026-11-14 |
| Ollama | `gemma4:e2b` / `e4b` | free, 4.6-7.5 GB / 6.6-9.5 GB | yes | yes | Too big for 6 GB shared with Whisper | https://ollama.com/library/gemma4 | 2026-11-14 |
| Ollama | `mistral-nemo` (12b) | free, 7.1 GB | yes | no | Too big | https://ollama.com/library/mistral-nemo | 2026-11-14 |
| Google Gemini | `gemini-3-flash-preview` | listed free (pricing summary, **not verbatim**); RPM/RPD **unpublished** | yes (OpenAI compat) | yes, default **high**, can go to `minimal` | **Best cloud pick** (owner probe: ~1 s with a tool call). It is a preview, so it may be shut down | https://aistudio.google.com/docs/thinking , https://aistudio.google.com/docs/pricing | 2026-11-14 |
| Google Gemini | `gemini-3.5-flash-lite` | free (pricing, verbatim); limits unpublished | yes | yes, default **minimal** | Stable, low-latency fallback | https://aistudio.google.com/docs/pricing , https://aistudio.google.com/docs/models | 2026-11-14 |
| Google Gemini | `gemini-3.6-flash` | free (verbatim); limits unpublished | yes | yes, default medium, supports `minimal` | Smarter stable option; test with `minimal` | same | 2026-11-14 |
| Google Gemini | `gemini-3.8-flash` | free (verbatim); limits unpublished | yes | yes, default **medium**, **no `minimal`** (low is the floor) | Poor fit for voice (owner probe: hangs >40 s) | https://aistudio.google.com/docs/thinking | 2026-11-14 |
| Google Gemini | `gemini-flash-lite-latest` | alias; target **unconfirmed** (probably gemini-3.5-flash-lite) | yes | depends on target | Avoid aliases in the wizard: their target changes (Google gives 2 weeks' email notice) | https://aistudio.google.com/docs/models | 2026-11-14 |
| Google Gemini | `gemini-2.5-flash` | deprecated; "API access … limited to existing users"; reroutes to 3.5-flash-lite on 2026-12-01 | yes | yes | **Not usable** (owner probe: 404) | https://aistudio.google.com/docs/models | 2026-11-14 |
| Google Gemini | `gemini-2.5-flash-lite` | "served until further notice"; free per pricing summary (**not verbatim**) | yes | off by default | Backup only; older | https://aistudio.google.com/docs/models | 2026-11-14 |
| Groq | `qwen/qwen3.8-27b` (preview) | free: 30 RPM, 1K RPD, **8K TPM, 200K TPD** | yes (+parallel) | yes, `reasoning_effort: "none"` available | Very fast but token limits allow only ~16 turns/day | https://console.groq.com/docs/rate-limits , https://console.groq.com/docs/reasoning | 2026-11-14 |
| Groq | `openai/gpt-oss-20b` | free: 30 RPM, 1K RPD, 8K TPM, 200K TPD | yes (no parallel) | yes, cannot be off (`low` minimum) | Fast; same token wall | same | 2026-11-14 |
| Groq | `openai/gpt-oss-120b` | free: 30 RPM, 1K RPD, 8K TPM, 200K TPD | yes (no parallel) | yes, `low` minimum | Smarter, same wall | same | 2026-11-14 |
| Groq | `llama-3.1-8b-instant` / `llama-3.3-70b-versatile` | **free limits unconfirmed** (not in today's rate-limit table; models page says "Enterprise pricing") | yes (+parallel) | no | Only if the owner's console shows free limits | https://console.groq.com/docs/models , https://console.groq.com/docs/tool-use | 2026-11-14 |
| NVIDIA NIM | `meta/llama-3.3-70b-instruct` | free dev/test access; per-model limits **unpublished** (user report: ~40 RPM, 1,000 credits) | yes (documented in schema) | no | Good, if the terms are OK (see notes) | https://docs.api.nvidia.com/nim/reference/meta-llama-3_3-70b-instruct-infer | 2026-11-14 |
| NVIDIA NIM | `openai/gpt-oss-20b` / `openai/gpt-oss-120b` | same | partially documented (`tools`, `tool_choice` in request schema) | yes | Test | https://docs.api.nvidia.com/nim/reference/openai-gpt-oss-120b-infer | 2026-11-14 |
| NVIDIA NIM | `nvidia/nemotron-3.5-lightning-30b-a3b` | same; featured as "Free inference" | **unconfirmed** | yes (Nemotron 3.x reasoning) | Test | https://build.nvidia.com/ , https://docs.api.nvidia.com/nim/reference/llm-apis | 2026-11-14 |
| OpenRouter | `nvidia/nemotron-3.5-lightning:free` | 20 RPM; **50 RPD** (<$10 bought), **1,000 RPD** (≥$10 lifetime) | yes (`tools`, `tool_choice`) | yes | Good free cloud backup | https://openrouter.ai/nvidia/nemotron-3.5-lightning:free , https://openrouter.ai/docs/api-reference/limits | 2026-11-14 |
| OpenRouter | `thinkingmachines/inkling-2-small:free` | same caps | yes (`tools`, `reasoning_effort`) | yes | Test | https://openrouter.ai/thinkingmachines/inkling-2-small:free | 2026-11-14 |
| OpenRouter | `liquid/lfm-3-1.7b:free` | same caps | yes (`tools`, `tool_choice`) | yes | Small, so probably weaker; fast | https://openrouter.ai/liquid/lfm-3-1.7b:free | 2026-11-14 |
| Cerebras | `qwen-3.8-27b` | **trial only**: $5 credit, expires after 30 days; 300 RPM, 150K uncached TPM | yes | reasoning controls not stated | Excellent speed, but not free long-term | https://inference-docs.cerebras.ai/support/rate-limits | 2026-11-14 |
| Cerebras | `gpt-oss-120b` | trial: 5 RPM, 30K uncached TPM, 1M TPD | yes | yes | Trial only | same | 2026-11-14 |
| Mistral | `ministral-8b-latest`, `mistral-small-latest` | free "Experiment" tier; limits **unconfirmed** (community: ~1 req/s); requires a phone number and opting in to data training | yes | no | Possible; check limits in the owner's console | https://docs.mistral.ai/capabilities/function_calling/ | 2026-11-14 |
| GitHub Models | n/a | **retired 2026-07-30** | n/a | n/a | **Do not list** | https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models | 2026-11-14 |
| Cloudflare Workers AI | e.g. `@hf/nousresearch/hermes-2-pro-mistral-7b` | 10,000 Neurons/day free | per-model only; tools via the OpenAI endpoint **not stated** | varies | Low priority | https://developers.cloudflare.com/workers-ai/platform/pricing/ | 2026-11-14 |

---

## Per-provider notes

### 1. Ollama (local, RTX 3050 6 GB shared with faster-whisper)

**VRAM budget.** faster-whisper `small` (float16) takes about 1 GB and `large-v3-turbo` about 1.6 GB+
(my estimate, not measured), which leaves roughly **4 GB for the LLM weights plus its context
cache**. With ~6k prompt tokens of tool definitions, the context cache (KV cache) of a 4B model adds
several hundred MB to ~1 GB. So in practice the download should be **≤ ~3 GB**, and `qwen3.5:4b`
(3.3-4.0 GB) is too tight. Hybrid/Mamba models (`granite4:*-h`, `lfm3`) have much smaller context
caches, which is a real advantage with a 6k-token tool list.

**Tool support** comes from the "tools" badge on each library page. Note that **`gemma3` has no tools
badge**. `gemma4` does have tools, but even its smallest size (`e2b`, 4.6 GB+) is too big here.

**Thinking control through the OpenAI-compatible endpoint**
(https://docs.ollama.com/api/openai-compatibility , https://docs.ollama.com/capabilities/thinking):
- `/v1/chat/completions` accepts `tools`, `reasoning_effort` and `reasoning: {effort}`.
  **`tool_choice` is NOT supported.** Edith must not rely on `tool_choice: "required"` with Ollama.
- `reasoning_effort: "none"` sends `think=false` for boolean-thinking models (qwen3, nemotron-3-nano,
  lfm3). Unsupported names fall back to the model's default. Use `/api/show` to see each model's
  `thinking.values`.
- GPT-OSS cannot turn thinking off: `"none"` maps to `low`.
- A `think` field is documented only for `/v1/responses`, not for chat completions.

**Context-window pitfall (important, unconfirmed for the current Ollama version).** If Ollama's default
context length is smaller than ~8k, the 50 tool definitions get silently cut off and tool calling gets
much worse. The OpenAI-compatible endpoint has no per-request `num_ctx`, so Edith's setup should set
`OLLAMA_CONTEXT_LENGTH=8192` (or larger), or create a Modelfile with `PARAMETER num_ctx 8192`. The
benchmark agent should check this with `/api/ps` or `/api/show`.

**Speed note (estimate).** On a laptop RTX 3050, reading ~6k prompt tokens with a 3-4B Q4 model
probably takes a few seconds on the first turn. Ollama reuses the cached prompt prefix when the system
prompt and tools stay byte-identical between requests, so **keep the tool list order stable** to get
that reuse.

### 2. Google Gemini (free tier)

- **Free model ids** ("Free of charge" on the pricing page, verbatim): `gemini-3.8-flash`,
  `gemini-3.6-flash`, `gemini-3.5-flash-lite`, plus non-chat models. From the cut-off part of the page
  (summary only, **not verbatim**): `gemini-3-flash-preview`, `gemini-3.1-flash-lite`,
  `gemini-2.5-flash-lite`. `gemini-4-carbon` / `gemini-4-argon` are **not** on the free tier.
  (The pricing page lists `gemini-4-hydrogen` as free, but it was not checked for tools or speed.)
- **Rate limits: Google does not publish per-model free RPM/RPD/TPM anymore.** The rate-limits page
  says they are shown in AI Studio (https://aistudio.google.com/rate-limit), are per *project* (not per
  key), RPD resets at midnight Pacific, and limits "are not guaranteed". Blog figures (5-15 RPM,
  20-1,500 RPD) conflict, so treat them as **unconfirmed**. The wizard should tell the user to check AI
  Studio, or the probe should read 429 responses.
- **Thinking control via the OpenAI endpoint** (https://aistudio.google.com/docs/openai): `reasoning_effort`
  = `minimal | low | medium | high` (+`xhigh` on gemini-4-hydrogen only). For Gemini 3 Flash,
  `minimal` maps to thinking_level `minimal`. **Thinking cannot be fully turned off on Gemini 3/4**
  (`"none"` works only on 2.5 models). `reasoning_effort` and `thinking_config` cannot be sent together.
- **Default thinking levels** (https://aistudio.google.com/docs/thinking): `gemini-3-flash-preview` =
  high, `gemini-3.6-flash` = medium, **`gemini-3.8-flash` = medium with no `minimal` option** (floor is
  `low`), `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` = minimal.
- **Why 3.8-flash hangs (likely, partly unconfirmed).** Its default is medium thinking and it cannot go
  below `low`. Reasoning over a 50-tool prompt at medium can take many seconds. A >40 s hang also
  suggests free-tier queueing or capacity limits on the newest model: Google says free limits "are not
  guaranteed". That part is **unconfirmed**. Try sending `reasoning_effort: "low"` with a client
  timeout of ~15 s. If it still hangs, keep it out of the voice shortlist.
- **gemini-3-flash-preview** answered in ~1 s in the owner's probe even though its documented default is
  `high`. Send `reasoning_effort: "minimal"` explicitly anyway. It is a *preview*: other previews
  (`gemini-3-pro-preview`, `gemini-3.1-flash-lite-preview`) have already been shut down, so the wizard
  needs a stable fallback (`gemini-3.5-flash-lite`).
- **Thought signatures (Gemini 3).** On the OpenAI-compatible endpoint, every `tool_calls[i]` in the
  response carries `extra_content.google.thought_signature`. When Edith sends the assistant turn back
  (with the tool result), it **must include that `extra_content` unchanged**. Otherwise Gemini 3 returns
  **HTTP 400** ("missing thought_signature"). Gemini 2.5 only gets slightly worse without it.
  Google's own OpenAI page only says signatures are supported. The field name and the 400 come from
  Google's developer forum and client bug trackers
  (https://discuss.ai.google.dev/t/openai-api-compatibility-broken-due-to-thought-signature-on-gemini-3-pro-preview/109823 ,
  https://github.com/microsoft/vscode/issues/296713 , https://github.com/openai/codex/issues/7519).
  **This affects `bot/jarvis/memory.py` / `brain.py`: keep assistant messages as raw dicts and don't
  rebuild them, including in streaming mode.** Also, "Invalid thought signature" appears if the model or
  key changes mid-conversation, so clear history when the wizard switches model.
- There is a forum report that tool calling fails for `gemini-3.6-flash` / `gemini-3.5-flash-lite` via
  OpenAI compatibility *on Vertex AI* (not AI Studio; not verified):
  https://discuss.ai.google.dev/t/tool-calling-doesnt-work-for-new-gemini-3-6-flash-and-gemini-3-5-flash-lite-models-on-vertex-ai-enterprise-agents-platform-via-openai-compatibility-api/175801
  The benchmark should test both models on the AI Studio endpoint.

### 3. Groq (free plan)

- Free-plan table (https://console.groq.com/docs/rate-limits). The page's tab label didn't come through
  in the fetch, but the numbers are far below the Developer-plan figures on the models page, so this is
  the free plan:

  | Model | RPM | RPD | TPM | TPD | ≈ Edith turns / min | ≈ Edith turns / day |
  |---|---|---|---|---|---|---|
  | `qwen/qwen3.8-27b` | 30 | 1K | 8K | 200K | ~0.5 (one request per minute) | **~16** |
  | `openai/gpt-oss-20b` | 30 | 1K | 8K | 200K | ~0.5 | ~16 |
  | `openai/gpt-oss-120b` | 30 | 1K | 8K | 200K | ~0.5 | ~16 |
  | `llama-3.1-8b-instant` | unconfirmed | unconfirmed | unconfirmed (blogs: 6K) | unconfirmed (blogs: 500K) | – | – |
  | `llama-3.3-70b-versatile` | unconfirmed | unconfirmed | unconfirmed (blogs: 12K) | unconfirmed (blogs: 100K) | – | – |

  (Estimate: ~6k tokens/request, 2 requests/turn. **TPM, not RPM, is the binding limit.** If the brain
  sent only the ~15 most relevant tools (~2k tokens), these numbers roughly double or triple.)
- Tool use: all eight listed models support it, but **gpt-oss models do not do parallel tool calls**
  (https://console.groq.com/docs/tool-use).
- Reasoning (https://console.groq.com/docs/reasoning): `qwen/qwen3.8-27b` accepts
  `reasoning_effort: "none"`. gpt-oss accepts `low|medium|high` (cannot be off). With tools,
  `reasoning_format` must be `parsed` or `hidden`, because **`raw` returns a 400 with tool calling**.
  For gpt-oss, use `include_reasoning: false` instead (`reasoning_format` is unsupported there).
- `qwen/qwen3-32b` (older id) no longer appears in Groq's docs.

### 4. NVIDIA NIM (build.nvidia.com, `https://integrate.api.nvidia.com/v1`)

- **What the free access is:** free "trial experience" API keys with the NVIDIA Developer Program. NVIDIA
  publishes **no credit amount or RPM**: "The rate of requests will vary per model queried"
  (https://forums.developer.nvidia.com/t/nvidia-nim-faq/300317). A user forum post from 2026-11-07 reports
  **1,000 credits and 40 RPM** observed. That is a user report, not NVIDIA's (no staff reply on that
  thread):
  https://forums.developer.nvidia.com/t/credit-rate-limit-increase-request-build-nvidia-com-free-tier-1-000-5-000-credits-40-200-rpm/388265 .
  Several recent third-party guides say credits were replaced by per-model rate limits; **unconfirmed**.
- **Terms:** "for prototyping, research, development and testing purposes only", and production is
  defined as including "activity serving real end-users". A personal assistant for the owner is
  arguably testing, but **the lead/owner should decide whether to offer NIM in the wizard**, and if
  so, label it "for testing".
- **Tool calling** is documented per model: `meta/llama-3.3-70b-instruct` has full `tools` / `tool_calls`
  / role `tool` in its schema. `openai/gpt-oss-120b` lists `tools` + `tool_choice` in the request schema,
  but its response schema shows no `tool_calls`, so it is partial. `nvidia/nemotron-3-super-120b-a12b`'s
  schema has **no** `tools` field. Every other model is **not stated**, so test before listing.
- Some catalogue models return 404 or "not available yet" for some accounts (forum report), so the
  wizard should probe the chosen model.

### 5. OpenRouter (`:free` models)

- Limits (https://openrouter.ai/docs/api-reference/limits): **20 RPM** for every account; **50 requests/day**
  if the account has bought less than $10 of credits in total, **1,000/day** with ≥ $10 lifetime
  purchases. A negative balance blocks free models too. `GET /api/v1/key` returns
  `free_model_daily_requests`, which the wizard could show.
  → ≈ **25 Edith turns/day** without credits, ≈ **500/day** after a one-time $10 purchase; ~10 turns/min.
- Free models with tools confirmed on their model pages (accepted parameters): `nvidia/nemotron-3.5-lightning:free`,
  `thinkingmachines/inkling-2-small:free`, `liquid/lfm-3-1.7b:free`. The tool-calling collection also lists
  `nvidia/nemotron-3.5-super-vl:free` and `nvidia/nemotron-3-ultra-550b-a55b:free` (1M ctx; large, so
  probably slower). **The full free+tools list could not be pulled**: the models API returned an
  incomplete catalogue to my fetcher. The benchmark agent should query
  `https://openrouter.ai/api/v1/models` with the owner's key and filter for `:free` +
  `supported_parameters` containing `tools`.
- The free list changes often (e.g. one free model shows "Going away November 28, 2026"), so the wizard
  should not hard-code more than a couple of `:free` ids.
- Privacy of free endpoints (prompt logging/training) is **unconfirmed** and worth checking per model,
  because Edith's prompts include PC details.

### 6. Others

- **Cerebras**: very fast and OpenAI-compatible, with strong tool support (`tool_choice` none/auto/required,
  strict mode, parallel calls). But there is **no permanent free tier**: a $5 trial credit that expires
  after 30 days (https://inference-docs.cerebras.ai/support/rate-limits). Trial limits: `qwen-3.8-27b` 300 RPM / 150K
  uncached TPM (≈12 turns/min, no daily cap); `gpt-oss-120b` 5 RPM / 30K TPM / 1M TPD. Reasoning controls
  are not stated. Label it "trial", not "free".
- **Mistral La Plateforme**: a free "Experiment" tier exists, but the official tier page now returns 404 and
  the help article gives no numbers. Community figure (**unconfirmed**): ~1 request/s, 500K TPM, ~1B
  tokens/month per model. Requires a verified phone number and **opting in to data training**.
  Function calling: `ministral-3b/8b/14b-latest`, `mistral-small-latest` and others
  (https://docs.mistral.ai/capabilities/function_calling/). **Caveat:** Mistral's `tool_choice` uses `"any"`
  instead of OpenAI's `"required"`. The function-calling page doesn't say the API is OpenAI-compatible.
- **GitHub Models**: **fully retired on 2026-07-30**. Remove it from any list.
- **Cloudflare Workers AI**: 10,000 Neurons/day free (https://developers.cloudflare.com/workers-ai/platform/pricing/).
  It has an OpenAI-compatible endpoint (`https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1`),
  but the docs don't say whether `tools` pass through it, and per-model tool support has to be looked up in
  the catalogue. Needs an account id plus a token, which is harder for a non-technical user. Low priority.

---

## Ranked shortlist for the benchmark agent (max 3 per provider)

Benchmark settings to use: Edith's real ~50 tool definitions; record `usage.prompt_tokens`, time to first
tool call, total turn time, and tool-choice / JSON-validity accuracy. Always send the "low thinking"
setting listed for each model.

**Ollama (local)**: set `OLLAMA_CONTEXT_LENGTH` ≥ 8192; run with Whisper loaded at the same time.
1. `qwen3:4b` with `reasoning_effort: "none"`: **"Best free local choice for 6 GB GPUs"**
2. `granite4:3b-h` (no thinking): **"Lightest local option: leaves the most room for voice recognition"**
3. `lfm3:1.7b` with `reasoning_effort: "none"`: **"For PCs without a usable graphics card (runs on CPU)"**
   (Reserves if time allows: `nemotron-3-nano:4b`, `llama3.2:3b`, `phi4-mini`.)

**Google Gemini** (`https://generativelanguage.googleapis.com/v1beta/openai/`; echo `extra_content` on tool calls)
1. `gemini-3-flash-preview` with `reasoning_effort: "minimal"`: **"Fastest free cloud choice (preview: may change)"**
2. `gemini-3.5-flash-lite` with `reasoning_effort: "minimal"`: **"Most dependable free Gemini (stable)"**
3. `gemini-3.6-flash` with `reasoning_effort: "minimal"`: **"Smarter free Gemini, a bit slower"**
   (Also re-probe `gemini-3.8-flash` with `reasoning_effort: "low"` and a 15 s timeout, only to explain why it is excluded.)

**Groq** (`reasoning_format: "hidden"` for Qwen, `include_reasoning: false` for gpt-oss)
1. `qwen/qwen3.8-27b` with `reasoning_effort: "none"`: **"Very fast, but only ~15 conversations a day for free"**
2. `openai/gpt-oss-20b` with `reasoning_effort: "low"`: **"Fast backup with the same daily limit"**
3. `llama-3.1-8b-instant`, only if the owner's console shows free limits for it: **"Most generous free limits, simpler answers"**

**NVIDIA NIM** (only if the lead accepts the "testing only" terms)
1. `meta/llama-3.3-70b-instruct`: **"Reliable tool use, free for testing"**
2. `nvidia/nemotron-3.5-lightning-30b-a3b`: **"Fast NVIDIA model, free for testing"** (tools unconfirmed)
3. `openai/gpt-oss-20b` (`reasoning_effort: "low"`): **"Backup, free for testing"**

**OpenRouter**
1. `nvidia/nemotron-3.5-lightning:free`: **"Good free backup (50 requests/day, 1,000 with a $10 top-up)"**
2. `thinkingmachines/inkling-2-small:free`: **"Free alternative backup"**
3. `liquid/lfm-3-1.7b:free`: **"Tiny and quick, for simple commands"**

**Cerebras (trial)**
1. `qwen-3.8-27b`: **"Fastest cloud option: 30-day trial, not free forever"**
2. `gpt-oss-120b`: **"Trial: smart but limited to 5 requests/minute"**

**Mistral (Experiment tier)**
1. `ministral-8b-latest`: **"Free with data-sharing opt-in"**
2. `mistral-small-latest`: **"Smarter Mistral, same free tier"**

## Implications for the code (for the lead / jarvis worker)

1. **Preserve `extra_content` on assistant tool-call messages** (Gemini 3 returns a 400 otherwise), and reset
   history when the provider or model changes.
2. **Don't send `tool_choice` to Ollama** (unsupported); use Mistral's `"any"` instead of `"required"`.
3. **Consider sending a relevant subset of tools** (e.g. by category) to cut ~6k → ~2k prompt tokens. On
   Groq's 8K-TPM free plan this is the difference between ~16 and ~40+ turns a day, and it speeds up
   local models too.
4. Set an explicit per-provider "low thinking" parameter (table above) and a **client timeout of ~15 s**
   with fallback to the next model, so a hanging free-tier model (like 3.8-flash today) doesn't freeze
   a voice turn.
5. Avoid `*-latest` aliases in the wizard: their target changes over time.
