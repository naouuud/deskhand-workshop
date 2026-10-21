# Choosing an LLM provider

Every checkpoint reads one setting from the repo `.env` to decide which company (or
which program on your laptop) runs the model:

```ini
LLM_PROVIDER=gemini
```

There are seven values. Azure, OpenAI and Anthropic are covered in the README. This page
covers the other four, which you can use without a credit card:

| `LLM_PROVIDER` | Runs where | Key | Cost | Good for |
| --- | --- | --- | --- | --- |
| `gemini` | Google's servers | free, from Google AI Studio | free tier, then pay as you go | the easiest start; the same key as the prep kit |
| `openrouter` | many companies' servers, behind one key | free account | free models, or a few cents with credit | trying many models with one key |
| `ollama` | your laptop | none | free | working offline, nothing leaves your machine |
| `lmstudio` | your laptop | none | free | the same, with a desktop app to pick models |

If you're unsure, pick Gemini. If you did the prep kit, you already have the key.

## How it works

Gemini, OpenRouter, Ollama and LM Studio all speak the same API as OpenAI (they are
"OpenAI-compatible"). So the checkpoints talk to them with the OpenAI client and change
three things: the address (`base_url`), the key, and the model name. All of it sits in
one table, `OPENAI_COMPATIBLE`, at the top of each checkpoint's `llm.py`.

Compatible doesn't mean identical, and `llm.py` has comments where the servers differ.
Two you can read about there:

- Ollama ignores the field LangChain uses for the token cap (`max_completion_tokens`),
  so checkpoints 02 to 04 send the older `max_tokens` instead.
- Gemini repeats a running token total on every streamed chunk, so adding the chunks up
  would count the same tokens several times.

## Gemini

1. Open <https://aistudio.google.com/apikey>, sign in with a Google account and click
   **Create API key**. No card needed.
2. In `.env`:

   ```ini
   LLM_PROVIDER=gemini
   GEMINI_API_KEY=your-gemini-key
   GEMINI_MODEL=gemini-3.5-flash-lite
   ```

Things to know:

- The free tier allows only a few requests per minute. A run over all 60 tickets
  (`main.py` in checkpoint 01) can hit the limit, and those tickets show
  `LLM_RATE_LIMITED`. Wait a minute and run again, or switch on billing in AI Studio.
- On the free tier Google may use what you send to improve its products. The Nimbus
  tickets are made up, so that's fine here. Don't send real customer data.
- Checkpoints 03 and 04 embed with `gemini-embedding-001`, on the same key.

## OpenRouter

1. Create an account at <https://openrouter.ai> and a key at
   <https://openrouter.ai/settings/keys>.
2. In `.env`:

   ```ini
   LLM_PROVIDER=openrouter
   OPENROUTER_API_KEY=your-openrouter-key
   OPENROUTER_MODEL=openrouter/free
   ```

Things to know:

- `openrouter/free` picks a free model for each request, so two runs may use two
  different models. Free models also have a small daily request limit. For steadier
  results, name a model: `openai/gpt-4o-mini`, `google/gemini-2.5-flash-lite`, or any id
  from <https://openrouter.ai/models>. Paid models need credit on your account; a few
  dollars lasts the whole course.
- OpenRouter forwards each request to one of several hosts that serve the model. The
  code asks it to pick only hosts that support structured outputs
  (`require_parameters`), so a model with none fails with an error instead of returning
  broken JSON.
- The embeddings for checkpoints 03 and 04 (`openai/text-embedding-3-small`) are not
  free. Indexing the six policy documents costs well under a cent.
- Cost tracking reads OpenRouter's model names: `openai/gpt-4o-mini` is priced as
  `gpt-4o-mini`, and free models as $0.

## Ollama

1. Install it from <https://ollama.com/download>. On Windows and macOS it runs in the
   background (look for the llama icon).
2. Download a chat model, and for checkpoints 03 and 04 an embedding model:

   ```powershell
   ollama pull llama3.2           # 2 GB
   ollama pull nomic-embed-text   # 270 MB, checkpoints 03 and 04 only
   ```

3. In `.env`:

   ```ini
   LLM_PROVIDER=ollama
   OLLAMA_MODEL=llama3.2
   ```

Things to know:

- No key, no bill: cost shows as $0 everywhere.
- The first call is slow (10 to 30 seconds) while the model loads.
- If Ollama answers with a 500 error that mentions CUDA, your graphics driver is too old.
  Update it, or run Ollama on the CPU only: quit the Ollama app from the tray icon, then
  in a PowerShell window you leave open run
  `$env:CUDA_VISIBLE_DEVICES = "-1"; ollama serve` (prep kit step 05 has the details).
- A 3-billion-parameter model is much weaker than the cloud ones. Expect more wrong
  categories and weaker reply drafts. That's worth seeing once. With 16 GB of RAM or
  more, try `ollama pull gemma3:4b` and set `OLLAMA_MODEL=gemma3:4b`.

## LM Studio

1. Install it from <https://lmstudio.ai>.
2. In the **Discover** tab, download a chat model. Gemma 3 4B is a good start on a
   16 GB laptop.
3. Open the **Developer** tab and switch the server on (**Start Server**, port 1234).
   Or from a terminal: `lms server start`.
4. Copy the model's API identifier from the Developer tab (or `lms ls`) into `.env`:

   ```ini
   LLM_PROVIDER=lmstudio
   LMSTUDIO_MODEL=google/gemma-3-4b
   ```

Things to know:

- No key, no bill, like Ollama.
- The model name must match LM Studio's identifier exactly, or every call fails with
  "model not found".
- For checkpoints 03 and 04 you also need an embedding model. LM Studio usually comes
  with `text-embedding-nomic-embed-text-v1.5`; if `lms ls` doesn't list it, search for
  "nomic embed text" in Discover.

## Check that it works

From the repo root, with the venv active:

```powershell
cd checkpoints\01-triage
py -c "import llm; print(llm.PROVIDER, llm.DEFAULT_MODEL); print(llm.text_completion([{'role': 'user', 'content': 'Say hi in five words.'}])[0])"
```

It prints the provider and model, then a short greeting. Then run the checkpoint as
usual (`py main.py`).

## Checkpoints 03 and 04: embeddings

The RAG index stores one vector per policy section, and its size depends on the
embedding model (1536 numbers for OpenAI's, 3072 for Gemini's, 768 for nomic). So after
you change provider, rebuild the index before you search it:

```powershell
cd checkpoints\03-rag
py ingest.py
```

Embeddings come from the same provider as chat. To mix them, set `EMBEDDING_PROVIDER`.
For example, chat on Ollama and embed on Gemini:

```ini
LLM_PROVIDER=ollama
EMBEDDING_PROVIDER=gemini
GEMINI_API_KEY=your-gemini-key
```

Anthropic has no embeddings API, so with `LLM_PROVIDER=anthropic` you always set
`EMBEDDING_PROVIDER`. Any of the other six works.

## Checkpoint 04: the release gate

`evals/thresholds.json` is calibrated on `claude-haiku-5-5`. On another model the gate
will probably close, which tells you that model makes more mistakes on the golden
tickets. That's the gate doing its job, not a bug in your setup.

A second, stronger model for plan generation goes in `GEMINI_MODEL_STRONG`,
`OPENROUTER_MODEL_STRONG`, `OLLAMA_MODEL_STRONG` or `LMSTUDIO_MODEL_STRONG`.

## Checkpoint 00 and the lab

Checkpoint 00 is the broken v1 bot, and it only knows Azure and OpenAI (one of its
flaws). To run it on another provider, point its OpenAI settings at that server for one
terminal. In PowerShell, from `checkpoints\00-broken-bot`, for Gemini:

```powershell
$env:OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
$env:OPENAI_MODEL = "gemini-3.5-flash-lite"
$env:OPENAI_API_KEY = (Select-String '^GEMINI_API_KEY=(.*)' ..\..\.env).Matches.Groups[1].Value
py incidents.py
```

That last line reads the key out of `.env`, so it never ends up in your shell history.
For Ollama use `http://localhost:11434/v1`, model `llama3.2` and any key
(`$env:OPENAI_API_KEY = "ollama"`); for LM Studio, `http://localhost:1234/v1`. The
settings last until you close the window.

The incidents were staged on GPT models, so another model may fail differently. On
Gemini, for example, Incident A's reply often fits inside v1's 256-token cap and the
JSON comes back whole. The flaw is still there in the code: v1 never checks why the
model stopped.

In the lab (`py lab\server.py`), the first entry of the model picker shows your provider
(Gemini, OpenRouter, Ollama or LM Studio) instead of GPT.

## If something goes wrong

| You see | Try |
| --- | --- |
| `LLM_PROVIDER='...' is not supported` | a typo in `.env`; the error lists the seven names |
| `GEMINI_API_KEY is not set` (or `OPENROUTER_API_KEY`) | the key line is empty or still says `your-...` |
| `LLM_RATE_LIMITED`, or `429` | the free tier's per-minute limit; wait a minute |
| `LLM_TIMEOUT` (01: `APIConnectionError`) on Ollama or LM Studio | the local server isn't running: open the app, or `ollama serve` / `lms server start` |
| `model not found` (404) | Ollama: `ollama pull <name>`. LM Studio: copy the exact identifier. OpenRouter: check the id on its models page |
| `401` | the key was copied with a space or quotes; paste it again |
| search errors after switching provider (03, 04) | rebuild the index: `py ingest.py` |
