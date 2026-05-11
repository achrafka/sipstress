# SipStress — command reference (tables)

**In one line:** SipStress **calls a number** through your **phone server**, **stays on the line** for a time you set, **hangs up**, and can **save sound** and **write reports**.

Below: **reference tables** for each flag, then **example command tables** (what to run, why, what you get), and **percentiles**. For the exact spelling of every flag: `sipstress --help`

Examples use placeholders like **`YOUR_SBC`** (director / SBC) and **`YOUR_CALLEE`** (dial string). Swap them for the values your platform gives you.

---

## Basics — who to call, how long, config file

| Command | What it does | When to use it | What you should see / get | Example |
|--------|----------------|----------------|----------------------------|---------|
| **`NUMBER`** (digits at the end of the line) | The **number to dial**. | Normal **one-number** test | One destination per run. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s` |
| **`--director`** | **Address of the phone server** that will place the call (IP, hostname, or `sip:…`). **Required.** | It’s wherever your SIP service lives. | SipStress talks to that server to start the call. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s` |
| **`--to`** | Full **call address** when a simple dial string isn’t enough. | Special formats (`user@host`, Tel URIs, `user=phone`, …). **Do not** use together with the positional `NUMBER`. | Same as a normal dial, but routing follows the exact address you set. | `sipstress --director YOUR_SBC --to sip:user@callee.example.com -d 30s` |
| **`-d` / `--duration`** | **How long to keep the call connected** after voice is flowing (timer is tied to media start, not to ringing). Default **60s**. | Any test where you need a fixed “talk time” (e.g. 30s, 2m). | The call stays up about that long, then the tool hangs up. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 2m` |
| **`--from`** | **Caller ID** the network should show to the callee. | Trunks that reject anonymous calls, or when you must show an **identity your policy allows**. | Far end sees the identity your platform accepts (if policy allows). | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --from sip:cliuser@trunk.example.com` |
| **`--config FILE.yaml`** | Read options from a **file** instead of typing a long command. | Repeatable tests; sharing one file with the team. Needs **PyYAML** installed. | Same behaviour as if you typed those flags on the command line. | `sipstress --config mytest.yaml` |

---

## Multiple numbers and load

**Rule:** use either **one** number (`NUMBER` or `--to`) **or** a list (`--numbers` / `--numbers-file`). Don’t mix both.

**`-d`** is still “how long each answered call stays up.” **`--calls`**, **`--cps`**, **`-j`** control how many dials happen and how fast / how many at once.

### Examples (copy-paste)

```bash
# One number, one call (usual test)
sipstress --director YOUR_SBC YOUR_CALLEE -d 30s

# Three destinations: dial each once in order (replace tokens with your dial strings)
sipstress --director YOUR_SBC --numbers FIRST_DEST,SECOND_DEST,THIRD_DEST -d 30s

# Same three destinations, but 9 calls total: each gets 3 calls (round-robin)
sipstress --director YOUR_SBC --numbers FIRST_DEST,SECOND_DEST,THIRD_DEST --calls 9 -d 30s

# Same callee many times, about 2 new calls per second
sipstress --director YOUR_SBC YOUR_CALLEE --calls 20 --cps 2 -d 15s

# Four calls live at the same time, 30 attempts, destinations rotate in list order
sipstress --director YOUR_SBC --numbers FIRST_DEST,SECOND_DEST,THIRD_DEST --calls 30 -j 4 -d 30s

# Wait 200ms after starting each call (extra spacing on top of --cps)
sipstress --director YOUR_SBC YOUR_CALLEE --calls 10 --call-delay 200ms -d 10s

# Stop *starting* new calls after 5 minutes (set --calls large enough that time is the real limit)
sipstress --director YOUR_SBC YOUR_CALLEE --calls 1000 --cps 1 -j 2 --run-duration 5m -d 20s
```

### Flags (short)

| Flag | Meaning |
|------|--------|
| **`--numbers`** | Comma list. Call 1 dials first, call 2 second, then repeat. |
| **`--numbers-file`** | One number per line (`#` = comment). If you use both, CSV comes first, then file lines. |
| **`--calls`** | How many calls to place in total. With several `--numbers` and no `--calls`, default = one call per listed number. |
| **`--cps`** | How many new calls to *start* per second (default `1`). |
| **`-j` / `--concurrency`** | Max calls at the same time (default `1`). |
| **`--call-delay`** | Extra pause after each start (`0` = none). |
| **`--run-duration`** | Only schedule new calls for this long; `0` = no time cap (use `--calls` or Ctrl+C). |
| **`--ramp-up`** / **`--ramp-down`** | Ease CPS up at start / down before `--run-duration` ends (`0` = off). |

**YAML:** same options as keys, e.g. `calls`, `numbers`, `numbers_file`, `cps`, `concurrency`, `call_delay`, `run_duration`, `ramp_up`, `ramp_down`.

---

## Identity, login, and “which line / partner”

| Command | What it does | When to use it | What you should see / get | Example |
|--------|----------------|----------------|----------------------------|---------|
| **`--pai`** | Sets the **trusted caller identity** header many carriers expect. | Same identity as `--from` | Better chance the network treats the CLI as valid. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --pai cliuser@sbc.example.com` |
| **`--provider NAME`** | Sends “use **this partner / gateway**” (`X-provider: NAME`). | OpenSIPS-style routing when your director picks an outbound leg from a symbolic name. | Server may route toward the matching trunk or PSTN partner. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --provider YOUR_CARRIER_TAG` |
| **`--extra-header 'Name: value'`** | Adds **custom** phone headers. Repeat the flag for more headers. | Rules only your SBC documents; overrides if you set the same name twice. | The server receives that extra instruction on INVITE (and related). | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --extra-header 'X-Custom: myvalue'` |
| **`--contact-user NAME`** | Username inside **Contact** (who we say we are for return traffic). Default **`sipstress`**. | IT asks for a specific trunk user. | Contact line matches what your policy expects. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --contact-user mytrunk` |
| **`--auth USER:PASSWORD`** | **Login** if the server challenges the call. | Registrars or trunks that need digest auth. | Call can proceed once credentials are accepted. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --auth myuser:mysecret` |
| **`--register-on-start`** | Sends **REGISTER** once **before** the test call. | Servers that only allow calls from registered clients. | A short registration step, then the normal test call. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --register-on-start` |

---

## Timers and “when to start”

| Command | What it does | When to use it | What you should see / get | Example |
|--------|----------------|----------------|----------------------------|---------|
| **`--max-call-duration`** | **Hard limit** for the *entire* attempt (ringing + queue + media). Default ≈ **duration + 3 minutes**. | **Long ringing**, queue music, or **183-only** wait (DialWaiting). | Test does not abort early while the platform is still ringing. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --max-call-duration 15m` |
| **`--invite-timeout`** | Max wait for a **final answer** to the initial call setup (answered, busy, etc.). | Very long rings; aligns with your `--max-call-duration` story. | You either get a final outcome or hit this limit (then timeout in report). | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --invite-timeout 600s` |
| **`--bye-timeout`** | How long to wait for a reply after **hang-up**. Default **32s**. | Rarely changed. | BYE completes or times out within this window. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --bye-timeout 10s` |
| **`--start-at`** | **Delay** the test (clock time, `+30s`, etc.). | Run after lunch, or sync with a maintenance window. | Nothing happens until the chosen time, then the normal call runs. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --start-at +2m` |
| **`--t1`** | Gap between **retry** sends on UDP (SIP T1). Default **500ms**. | **IT debugging** only. | Finer control of retransmit timing; most users never touch this. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --t1 1s` |

---

## Recording sound (WAV)

| Command | What it does | When to use it | What you should see / get | Example |
|--------|----------------|----------------|----------------------------|---------|
| **`--record DIR`** | **Folder** where **WAV** files are written. | Any time you want to **listen back** to the call. | Mono file: mostly what you **heard** from the network (inbound). | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --record ./rec` |
| **`--record-duplex`** | **Stereo** file: **left = heard**, **right = what we sent** on RTP (after encoding). Needs **`--record`**. | Checking **both directions**; needs **two channels together**. | A `*_duplex.wav` (or similar) under `DIR`. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --record ./rec --record-duplex` |
| **`--microphone`** | Feeds your **PC microphone** into the live call. | When you must **speak** into the test (IVR, echo test). Install **audio** extra: `uv sync --extra audio` (or `pip install -e ".[audio]"`). | Your voice goes out on the call; recording rules still apply. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --record ./rec --microphone` |
| **`--mic-gain`** | Louder/quieter **mic** (with `--microphone`). Default **1.0**. | Too loud → try **0.5–0.75**. | More comfortable level in the sent audio. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --record ./rec --microphone --mic-gain 0.6` |
| **`--record-inbound-gain`** | Louder/quieter **only the saved inbound** WAV. Default **0.72** (softer than raw). | Harsh volume jump after answer vs ringback; use **`1.0`** for “no touch”. | File sounds softer/louder; **live** call and **JSON metrics** stay the same. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --record ./rec --record-inbound-gain 1.0` |
| **`--codec pcmu` / `pcma`** | **Voice coding** on the wire (region-dependent). Default **`pcmu`**. | Carrier asks for **A-law** (`pcma`). | Same call flow with the chosen codec in RTP. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --codec pcma` |

---

## Reports and logs

| Command | What it does | When to use it | What you should see / get | Example |
|--------|----------------|----------------|----------------------------|---------|
| **`--json-out FILE`** | Full **machine-readable** report (metrics, health, call record). | Automation, tickets, deep analysis. | A `.json` you can open or parse. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --json-out report.json` |
| **`--html-out FILE`** | **Web page** with charts and explanations. | Sharing with managers; quick visual readout. Needs **viz** extra: `uv sync --extra viz` or `pip install -e ".[viz]"`. | A `.html` you open in a browser. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --html-out report.html` |
| **`--summary-out FILE`** | **Short text** summary on disk. | Email-sized recap; archive. | A small `.txt` with PASS/FAIL and key numbers. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --summary-out summary.txt` |
| **`--audit`** | **Extra SIP/RTP detail** inside JSON. | Investigations; **much larger** JSON. | Richer `call_records` / audit arrays. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --json-out report.json --audit` |
| **`--print-summary`** | Print the text summary to the **screen** even when files are set. | You want **console + files**. | Summary appears in the terminal **and** in files. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --summary-out summary.txt --print-summary` |
| **`--no-dashboard`** | Turns off the **live coloured dashboard** in the terminal. | Scripts, CI, or logging to a pipe. | Quieter console; only logs + file output you asked for. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --no-dashboard` |
| **`--log-level`** | **DEBUG / INFO / WARNING / ERROR**. Default **INFO**. | **DEBUG** when chasing bugs; **ERROR** when you only want problems. | More or less text on stderr / log file. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --log-level DEBUG` |
| **`--log-file FILE`** | Copy logs to a **file**. | Long runs; hand-off to support. | Same messages as console, also in `FILE`. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --log-file sipstress.log` |

**Note:** If you don’t pass `--json-out` or `--summary-out`, SipStress often still **prints a short summary** at the end.

---

## Networking

| Command | What it does | When to use it | What you should see / get | Example |
|--------|----------------|----------------|----------------------------|---------|
| **`--bind-ip`** | Which **local network interface** answers for SIP. Default **`0.0.0.0`**. | Multi-homed server or strict binding. | Traffic uses the chosen interface. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --bind-ip 192.168.1.10` |
| **`--bind-port N`** | **Local SIP port**; **`0`** = auto. | Port conflicts or firewall rules. | SipStress listens on that UDP port. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --bind-port 5060` |
| **`--advertised-ip`** | **Public IP** you publish in SIP/SDP when it differs from the PC’s real IP (**NAT**). | Home lab, VPN, or private LAN behind one public IP. | Remote side sends media/signalling to an address that reaches you. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --advertised-ip 203.0.113.50` |
| **`--rtp-port-range LOW-HIGH`** | **UDP ports** for voice. Default **`40000-41000`**. | Firewall only opens certain UDP range. | RTP stays inside that window. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --rtp-port-range 20000-20100` |
| **`--trace-sip`** | Logs **every** SIP datagram (**very verbose**). | Deep protocol debugging only. | Huge logs; use with `--log-file`. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --trace-sip --log-file sip.log` |

---

## Config file

| Command | What it does | When to use it | What you should see / get | Example |
|--------|----------------|----------------|----------------------------|---------|
| **`--config FILE.yaml`** | Loads options from YAML (`director`, `number`, `duration`, …). | Same test every day; non-technical users edit one file. | Same as CLI, fewer typing errors. | `sipstress --config mytest.yaml` |

Example `mytest.yaml`:

```yaml
director: "YOUR_SBC"
number: "YOUR_CALLEE"
duration: 90s
from: "sip:cliuser@trunk.example.com"
pai: "cliuser@sbc.example.com"
provider: your_carrier_tag
record: ./rec
record_duplex: true
```

Run: `sipstress --config mytest.yaml`  
Use **`to:`** instead of **`number:`** when you need a full callee address.

Load test from a file (same flags as CLI):

```yaml
director: "YOUR_SBC"
numbers:
  - "FIRST_DEST"
  - "SECOND_DEST"
calls: 30
concurrency: 4
duration: 30s
```

Then: `sipstress --config multitest.yaml`

---

## Exit code & limits

| Situation | Meaning | What you should expect | Example |
|-----------|---------|-------------------------|---------|
| Exit **0** | Automatic **health** in the report said **PASS** (rules in the JSON). | “Green” from the tool’s checks still read HTML/JSON if something sounded wrong. | `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --summary-out s.txt`; then `echo $?` → `0` if PASS |
| Exit **2** | Automatic health said **not PASS** (e.g. answer too slow, thresholds). | Call might still have **felt** OK; open the report for the exact rule. | Same command when rules fail (e.g. slow answer) |
| **No `--scenario` in this CLI** | This program only runs the **invite_media** style test (dial, media, hang up). | You cannot pick other scenarios from this command line. | — |

---

## p50, p90, p95, p99 in **Answer**, **Setup**, and **Media** (summary / JSON)

These labels appear in **`summary.txt`** and **`report.json`** next to **Setup**, **Answer**, **jitter**, or **loss**. They are **not** separate tests they are **statistics** on many measurements taken together.

| Symbol | One-line meaning | For **Answer** (time until pickup) | For **Media** (jitter / packet loss) | Example (see them in output) |
|--------|------------------|--------------------------------------|----------------------------------------|--------------------------------|
| **p50** | Typical “middle” value | Half of calls **answered** faster, half slower than this time | Half of calls had **better** jitter/loss than this, half worse | `sipstress ... --summary-out summary.txt` → open `summary.txt` |
| **p90** | 9 out of 10 calls are at least this good | 90% of answer times are **≤** this (only 10% slower) | 90% of runs had jitter/loss **no worse than** this | Same; or **`directors[].latency_ms`** / **`media`** in `report.json` |
| **p95** | Only the slowest 5% are worse | “Almost everyone” answered by this time, except 5% tail | Worst 5% of calls sit above this for jitter/loss | Same |
| **p99** | Only the slowest 1% are worse | Used a lot for **SLAs** rare slow answers | Rare **bad** jitter or loss spikes | Same |

**Single call:** If you only ran **one** call, **p50 = p90 = p95 = p99** (all the same number) there is only one measurement.

**Many calls:** Percentiles **separate** “normal” (p50) from **bad tail** (p99).

---

## Example commands (table)

Use these as **copy-paste starters**. Replace placeholders such as **`YOUR_SBC`** and **`YOUR_CALLEE`**, tokens like **`FIRST_DEST`**, and **`example.com`**-style hostnames with what your deployment uses (`cliuser`, **`YOUR_CARRIER_TAG`**, and file paths stay illustrative).

### Everyday tests

| Example command | What this run is for | What you usually see if it works |
|-----------------|------------------------|-----------------------------------|
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s` | **Smoke test** director + callee + short media hold. | Call connects; short summary at the end; exit **0** or **2** from health rules. |
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 2m --from sip:cliuser@trunk.example.com` | Same test but with a **caller identity** your trunk expects. | Call proceeds; `From` matches what you set (if policy allows). |
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 60s --provider YOUR_CARRIER_TAG --from sip:cliuser@trunk.example.com --pai cliuser@sbc.example.com` | **`X-provider`** plus **matching `From` / PAI** (common outbound pattern). | INVITE carries the provider tag and identity headers; routing follows your platform rules. |

### Recording

| Example command | What this run is for | What you usually see if it works |
|-----------------|------------------------|-----------------------------------|
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 90s --record ./rec` | Save **what you heard** (mono WAV under `./rec`). | A `.wav` file with inbound audio usable for listening. |
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 90s --record ./rec --record-duplex` | **Stereo**: left = heard, right = what the tool sent on RTP. | File name often includes `_duplex`; two channels for comparison. |
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 60s --record ./rec --record-duplex --record-inbound-gain 1.0` | Duplex recording **without** default inbound softening. | WAV inbound channel at “full” scaled level vs default **0.72**. |

### Reports

| Example command | What this run is for | What you usually see if it works |
|-----------------|------------------------|-----------------------------------|
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 120s --json-out report.json --summary-out summary.txt` | **Files**: machine JSON + short text. | `report.json` + `summary.txt` on disk; summary may also print if no html. |
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 120s --html-out report.html` | **Web dashboard** with charts (`uv sync --extra viz` or `pip install -e ".[viz]"`). | `report.html` opens in a browser; graphs + KPI-style blocks. |
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 60s --json-out report.json --audit` | **Deep JSON** (larger file, more SIP/RTP detail). | Same schema, richer `call_records` / audit sections. |

### Long ringing or queue

| Example command | What this run is for | What you usually see if it works |
|-----------------|------------------------|-----------------------------------|
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 30s --max-call-duration 15m` | **Long wait** before answer (music / queue) but only **30s** of media after connect. | INVITE stays alive up to **15 minutes**; your **30s** still controls media hold after 200 OK. |

### Networking

| Example command | What this run is for | What you usually see if it works |
|-----------------|------------------------|-----------------------------------|
| `sipstress --director public.sbc.example.com YOUR_CALLEE -d 60s --advertised-ip 203.0.113.50` | NAT / public IP differs from the PC’s LAN address. | Media path may establish when it failed before. |
| `sipstress --director YOUR_SBC YOUR_CALLEE -d 60s --rtp-port-range 20000-20100` | Firewall allows only a **specific UDP range** for voice. | RTP stays inside **20000–20100** on this host. |

### Config file

| Example command | What this run is for | What you usually see if it works |
|-----------------|------------------------|-----------------------------------|
| `sipstress --config mytest.yaml` | All options live in **`mytest.yaml`** (needs PyYAML). | Same result as typing the same flags; easier to edit and reuse. |

### Multiple numbers

| Example | Idea |
|--------|------|
| `sipstress --director YOUR_SBC --numbers FIRST_DEST,SECOND_DEST,THIRD_DEST --calls 9 -d 30s` | Nine dials; destinations repeat in **list order** (round‑robin). |
| `sipstress --director YOUR_SBC --numbers-file list.txt --calls 100 -j 8 -d 20s` | Numbers from **list.txt**; up to **8** calls at once. |

---

### Same examples as one-liners (copy-paste)

```bash
# Minimal
sipstress --director YOUR_SBC YOUR_CALLEE -d 30s

# Caller ID + provider tag + PAI
sipstress --director YOUR_SBC YOUR_CALLEE -d 60s \
  --provider YOUR_CARRIER_TAG \
  --from sip:cliuser@trunk.example.com \
  --pai cliuser@sbc.example.com

# Record stereo + all report types
sipstress --director YOUR_SBC YOUR_CALLEE -d 120s \
  --record ./rec --record-duplex \
  --json-out report.json --html-out report.html --summary-out summary.txt --audit

# NAT (replace IP)
sipstress --director public.phone.company.com YOUR_CALLEE -d 60s \
  --advertised-ip YOUR.PUBLIC.IP.HERE

# See section "Multiple numbers and load" for more load examples
sipstress --director YOUR_SBC --numbers FIRST_DEST,SECOND_DEST,THIRD_DEST --calls 9 -d 30s
```
