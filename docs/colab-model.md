# Running your own model on Google Colab (Ollama + ngrok)

This project can use a **self-hosted, OpenAI-compatible model** as its relevance
judge instead of (or ahead of) Gemini and Groq. It plugs in as the `colab`
provider. With `providers: [colab, gemini, groq]` (the default in
`settings.yaml`), the run prefers your Colab model when the notebook is up and
falls back to Gemini→Groq automatically when it is not — so a dead tunnel never
costs you your alerts.

The setup: **Ollama** serves **Qwen2.5-7B** on a free Colab notebook, exposed
with an **ngrok** tunnel. Ollama manages the model and rarely crashes; ngrok
dials `127.0.0.1`, avoiding the IPv6 `::1` refusal that breaks cloudflared quick
tunnels.

> **Ephemeral by nature.** Free Colab sessions idle out (~90 min) and hard-stop
> (~12 h), and the ngrok URL changes every session. Each time you restart, you
> re-run the cells below and paste the new URL into `settings.yaml`.

---

## One-time: get a free ngrok token

Sign up at <https://dashboard.ngrok.com> → **Your Authtoken** → copy it. In
Colab, add it as a secret: **🔑 (left sidebar) → Secrets → + Add**, name it
`NGROK_AUTHTOKEN`, paste the value, and toggle **Notebook access** on.

Pick a GPU runtime too: **Runtime → Change runtime type → T4 GPU → Save** (not
required, but a 7B model answers far faster on the GPU).

---

## The notebook, cell by cell

Run these cells top to bottom. Cells 1–3 are one-time per session; cell 4 (the
tunnel) must **stay running** the whole time you use the model.

### Cell 1 — install Ollama + ngrok

```python
!apt-get update -qq
!apt-get install -y zstd                     # Ollama's installer needs zstd
!curl -fsSL https://ollama.com/install.sh | sh
!pip install -q ollama pyngrok
!ollama --version
```

### Cell 2 — start the Ollama server

```python
import subprocess, time
subprocess.Popen(["ollama", "serve"])        # listens on 127.0.0.1:11434
time.sleep(5)
print("Ollama server started.")
```

### Cell 3 — pull the model and give it an 8k context

Ollama defaults to a 2048-token context. Our prompt plus a 10-job batch is
bigger than that, so we bake a larger context into a derived model. `qwen2.5-8k`
is just a name — it is still the 7B model, only with `num_ctx` raised to 8192
(8k **tokens of context**, not a bigger model).

```python
!ollama pull qwen2.5:7b
open("Modelfile", "w").write("FROM qwen2.5:7b\nPARAMETER num_ctx 8192\n")
!ollama create qwen2.5-8k -f Modelfile
!ollama list
```

### Cell 4 — open the ngrok tunnel (keep this running)

The `host_header` is essential: Ollama refuses requests whose `Host` is not
localhost, so ngrok must rewrite it or every call 403s.

```python
from pyngrok import ngrok
from google.colab import userdata

ngrok.set_auth_token(userdata.get("NGROK_AUTHTOKEN"))
tunnel = ngrok.connect(11434, host_header="localhost:11434")
print(tunnel.public_url)     # https://xxxx.ngrok-free.dev  <-- copy this
```

---

## Point this project at it

Edit **`config/settings.yaml`**, `llm:` block:

```yaml
llm:
  providers:
    - colab
    - gemini
    - groq
  colab_base_url: "https://xxxx.ngrok-free.dev"   # <- the ngrok URL from cell 4, no trailing path
  colab_model: "qwen2.5-8k"                        # <- the 8k model from cell 3
```

Leave `COLAB_API_KEY` empty in `.env` — Ollama needs no key. The provider appends
`/v1/chat/completions` (Ollama's OpenAI-compatible endpoint) itself. To disable
Colab and use Gemini/Groq only, set `colab_base_url: ""`.

---

## Verify

```bash
python -m job_alerts --log-level INFO search --dry-run
```

Look for `via colab` in the assessment log, and `LLM (colab) scored …` in:

```bash
python -m job_alerts list --new --explain | head
```

If the tunnel is down or the URL is stale, the run falls back to Gemini/Groq
with no error — that is the safety net working as designed.

---

## Notes and troubleshooting

- **Every session is new.** Restarting the notebook gives a fresh ngrok URL;
  re-run the cells and update `colab_base_url`. Inherent to free Colab.
- **Broken-JSON / everything falls back to Gemini.** Almost always the context
  window: make sure `colab_model` is the **`qwen2.5-8k`** model from cell 3, not
  plain `qwen2.5:7b`. As a fallback you can instead lower `llm.batch_size` (e.g.
  to 3) and `llm.max_description_chars` (e.g. 500) in `settings.yaml`.
- **403 on every request.** The `host_header="localhost:11434"` in cell 4 is
  missing or wrong — Ollama is rejecting the tunneled Host header.
- **First request is slow.** Model load + first-token latency can be tens of
  seconds; the provider timeout is `llm.timeout` (default 60s). Raise it in
  `settings.yaml` if the first batch times out.
- **Idle timeout.** Colab reclaims idle T4s (~90 min) and caps sessions (~12 h).
  For scheduled 08:00/18:00 runs, start the notebook shortly before — or just
  rely on the Gemini/Groq fallback for unattended runs.
- **Another model.** Any Ollama model works; set `colab_model` to its name (e.g.
  `llama3.1:8b`). Rebuild the `-8k` variant the same way if you want its context
  raised.
