# sipstress

**sipstress** is a **Python SIP call generator and diagnostic tool**. It places real **INVITE** dialogs through a **director** (SBC / OpenSIPS / similar), keeps **RTP** open for a configurable time, then hangs up. It does **not** embed your whole IVR it drives the **SIP/RTP leg** and records what the **network and platform** did: response codes, timings, jitter, loss, per-call traces, optional WAV captures, and PASS/FAIL health checks.

Use it when you need something **SIPp-like**, but readable in Python, with **reports** (console, JSON, HTML) you can archive or paste into emails.

---

## What it does (mental model)

1. **Your machine** sends **UDP SIP** (and RTP) toward **`--director`**.
2. The **director + downstream equipment** decides routing (provider headers, callee format, outbound PSTN legs, queues, …).
3. sipstress waits for **provisionals / final INVITE responses**, optionally starts **early media RTP** when **180/183 + SDP** appear, stays in media for **`--duration`**, then tears down (**BYE**).
4. It aggregates metrics and emits **text / JSON / HTML** summaries.

Timeouts, duplicate SIP responses, and high RTP loss in the report usually point to **path capacity**, **UDP issues**, or **remote behaviour**.

---

## Features

- **invite_media** scenario (default from CLI): INVITE → optional early media RTP → answered media period → teardown.
- **Load shaping**: **`--calls`**, **`--cps`** (target attempts per second), **`--concurrency` / `-j`** (parallel calls cap).
- **Identity & routing hints**: **`--from`**, **`--pai`**, **`--provider`** → **`X-provider`**, **`--extra-header`**, **`--numbers`** / callee lists.
- **Recording**: WAV under **`--record`**; stereo **`--record-duplex`**; optional **`--microphone`** into RTP (**`[audio]`** extra).
- **Reports**: **`--json-out`**, **`--summary-out`**, interactive **`--html-out`** (**Plotly**; install **viz** extra), PDF via **`--pdf-out`** + **pdf** extra / **`sipstress-html2pdf`**.

Detailed flag explanations: **[`CLI_GUIDE.md`](CLI_GUIDE.md)**.

---

## Requirements

- **Python ≥ 3.9** (**3.10+** recommended).
- **`PyYAML`** and **`rich`** are **default** dependencies in `pyproject.toml` (`uv sync` / `pip install -e .` gets both). Optional groups add Plotly, Playwright, and audio tooling.

---

## Installation

### uv (recommended)

Install [**uv**](https://docs.astral.sh/uv/) (see upstream docs for your OS e.g. `curl -LsSf https://astral.sh/uv/install.sh | sh`). **uv** creates and manages `.venv`, resolves versions from **`pyproject.toml`**, and can maintain a **`uv.lock`** for reproducible installs.

```bash
cd sipstress
uv sync
uv run sipstress --help
```

- **`uv sync`** installs the package **in editable mode** into `.venv/` with core deps (**PyYAML**, **rich**).
- **`uv run …`** runs a command inside that environment without manually activating the venv.

**Lockfile (optional, for reproducible CI or teams):**

```bash
uv lock   # writes/updates uv.lock commit it if you rely on pinned versions
uv sync   # installs exactly what the lockfile specifies
```

**Optional extras** (combine as needed):

| Extra | uv | Purpose |
|--------|-----|--------|
| **HTML dashboards** | `uv sync --extra viz` | `plotly` for `--html-out` |
| **PDF export** | `uv sync --extra pdf` then `uv run playwright install chromium` | `--pdf-out` / **`sipstress-html2pdf`** |
| **Microphone RTP** | `uv sync --extra audio` | `--microphone` (+ **`audioop-lts`** on **Python 3.13+** if needed) |
| **Everything optional** | `uv sync --all-extras` | All of the above |

**Tests / development** ( **`dependency-groups`** `dev` → **pytest** ):

```bash
uv sync --group dev --extra viz   # viz needed for HTML dashboard tests
uv run pytest
```

### pip + venv (legacy)

```bash
cd sipstress
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -U pip
pip install -e .          # core: PyYAML + rich
pip install -e ".[viz]"  # add Plotly for HTML; use .[pdf] / .[audio] as needed
pip install pytest       # tests
```

`requirements.txt` is kept for simple pip-only workflows; prefer **`pyproject.toml`** + **uv** for new setups.

Entry points:

- **`sipstress`** main CLI (**`sipstress.cli:main`**)
- **`sipstress-html2pdf`** HTML→PDF helper

---

## Quick start

Substitute **`YOUR_SBC_HOST`** (hostname or IP), **`YOUR_CALLEE`** (extension or full URI user part), and **`YOUR_*`** identity fields with values from **your** interconnect none of the placeholders below are real services.

With **uv** (no shell activation): prefix with **`uv run`** e.g. **`uv run sipstress --director YOUR_SBC_HOST …`**. If you activated `.venv`, call **`sipstress`** directly.

```bash
sipstress --director YOUR_SBC_HOST YOUR_CALLEE --duration 120s
```

**Explicit Request-URI / port:**

```bash
sipstress --director sip:YOUR_SBC_HOST:5060 --to sip:YOUR_CALLEE@YOUR_SBC_HOST -d 60s
```

**Trunk-style caller ID** (when anonymous `From` breaks PSTN bridges):

```bash
sipstress --director YOUR_SBC_HOST YOUR_CALLEE --duration 120s \
  --from sip:YOUR_E164@your-trunk.example \
  --pai YOUR_E164@your-trunk.example
```

**Provider routing hint** (token your platform expects on `X-provider`):

```bash
sipstress --director YOUR_SBC_HOST YOUR_CALLEE --duration 120s \
  --provider YOUR_PLATFORM_TOKEN \
  --from sip:YOUR_E164@your-domain.example \
  --pai YOUR_E164@your-domain.example
```

(`--provider` sets **`X-provider: YOUR_PLATFORM_TOKEN`** unless you override with **`--extra-header`**.)

**Batch run** multiple attempts, launch rate, parallelism cap, WAV + reports (shape only; tune flags to your lab):

```bash
sipstress --director YOUR_SBC_HOST YOUR_CALLEE --duration 15s \
  --calls 20 --cps 4 -j 2 \
  --provider YOUR_PLATFORM_TOKEN \
  --from sip:YOUR_E164@your-domain.example --pai YOUR_E164@your-domain.example \
  --record ./rec --record-duplex \
  --json-out ./report.json --html-out ./report.html
```

---

## Key CLI concepts

| Flag | Meaning |
|------|--------|
| **`--director`** | SIP host (and implied realm for bare digit callees). |
| **Positional `NUMBER` / `--to`** | Callee; digits become `sip:NUMBER@director-host` unless you pass a full URI. |
| **`--duration` / `-d`** | **Media hold** after RTP is live (not the same as INVITE timeout). |
| **`--calls`** | Total INVITE attempts to schedule. |
| **`--cps`** | Target **starts per second** (scheduler; actual rate can be lower). |
| **`-j` / `--concurrency`** | **Maximum simultaneous** calls (caps parallelism). |
| **`--record` / `--record-duplex`** | WAV artefacts; **`--audit`** expands JSON SIP detail. |

Behaviour notes ( **`invite_media`** ):

- **Early media**: if **180/183 with SDP**, RTP can start **before** **200 OK**.
- Long ringing with only provisionals is covered by **`--max-call-duration`** / invite wait logic (see **`CLI_GUIDE.md`**).
- Some **BYE** quirks (**481**, **408**, **513**) can still count as a **successful** test media completion; see JSON **`bye_note`**.

---

## Reports

- **Console**: live table + final **PASS/FAIL** and findings.
- **`--summary-out`**: plain text; also printed when no output files are set (unless disabled).
- **`--json-out`**: machine-readable **`sipstress_json_v2`** envelope (directors, **call_records**, health, thresholds).
- **`--html-out`**: browser dashboard (install **viz** extra, e.g. **`uv sync --extra viz`**): KPIs, charts, **all calls** table, first-call deep dives.

---

## Tests

```bash
uv sync --group dev --extra viz
uv run pytest
```

(With pip: `pip install -e ".[viz]"` and install **pytest**, then `pytest`.)

---

## Project layout (short)

- **`sipstress/`** package (CLI, engine, scenarios, media, reports, analysis).
- **`tests/`** unit / smoke tests.
- **`CLI_GUIDE.md`** long-form documentation.

---

## Contributing

Issues and PRs welcome. Keep changes focused; match existing style; run **`uv run pytest`** (or **`pytest`** in your venv) before submitting.
