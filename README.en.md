# Capability Dissociation in a Ternary-Quantized 27B Model on 8GB VRAM

> **In one line**: running a ternary-quantized (PTQ1) 27B multimodal model on
> 8GB of VRAM, we measured its real behaviour across four vision dimensions with
> controlled experiments. **The result is not "it got dumber overall" — some
> capabilities are selectively preserved while others drop to zero.**

**English** · [中文](README.md)

---

## 📄 Documents

| Document | Content |
|---|---|
| **[docs/FINDINGS.en.md](docs/FINDINGS.en.md)** | **Full report: mapping the capability dissociation** (results · method · limitations) |
| [docs/FINDINGS.md](docs/FINDINGS.md) | Same report, in Chinese |
| [docs/EXPERIMENT_DESIGN.md](docs/EXPERIMENT_DESIGN.md) | Experiment design (ruling out alternative explanations, reading rules, honest boundaries) *(Chinese)* |
| [docs/TOPICS.md](docs/TOPICS.md) | Eight candidate research topics and their shared design *(Chinese)* |
| [experiments/analysis/report.html](experiments/analysis/report.html) | Interactive report — 6 charts + 57 raw outputs, **with a 中文 / EN toggle** |

### Report preview (left: Chinese · right: English)

One `report.html`, one click in the top-right corner — chart titles, metric labels
and the raw-output table all switch with it, **with no page reload and no network access**.

![report preview](docs/preview.png)

### Key result at a glance

> **On 8GB VRAM, this ternary-quantized 27B model has intact semantic vision
> (4/4 dimensions) but a coordinate-localization hit rate of 6% (2/36, no hint given).**
>
> A control experiment shows that when the prompt states a *wrong* location,
> the model outputs exactly that wrong coordinate —
> **it never looks at the image; it only does arithmetic on the prompt.**
>
> → Engineering implication: don't use this class of model for localization;
> use the OS accessibility API instead. And never "give it an approximate
> position and let it refine" — it will adopt your wrong value.

### Figures

| Dissociation | Control experiment |
|---|---|
| ![dissociation](docs/figs/fig5_dissociation_en.svg) | ![control](docs/figs/fig0_control_en.svg) |

---

## What this project investigates

### Starting point

The usual statement is "local small models are dumb". That is too vague to
guide engineering decisions.

This project tests a more precise proposition:

> **The capability loss caused by extreme low-bit quantization is not uniform.
> Some capabilities are almost untouched; others collapse to zero.**

If true, this directly contradicts the default assumption that an average score
(MMLU / perplexity) can summarize the cost of quantization — **because an
average turns "perfectly fine" and "completely broken" into "a slight decline".**

### Why it is measurable now

Almost all quantization research measures Q8 → Q4 → Q2.
**Q2 is already considered garbage by consensus**, so nobody goes below it.

This project uses **ternary weights (−1 / 0 / +1)** — an even more extreme tier
than Q2. Combined with an engine modification that offloads the KV cache to
system RAM, it sustains a nominal 128K context on 8GB of VRAM.

**This combination has, to our knowledge, never been reproduced.** It is not
"I built a system" — it is "I have an experimental platform others don't".

---

## Measured results (2026-10-05)

Full report: **[docs/FINDINGS.en.md](docs/FINDINGS.en.md)**.

| Capability dimension | Result | n |
|---|---|---|
| Image content understanding | **Intact** — lists objects and their quadrants | 1 |
| Colour attribute recognition | **Intact** — answers "2 colours" exactly | 1 |
| Spatial partition counting | **Intact** — answers "2 on the left" exactly | 1 |
| Object counting | **Intact** — answers "4 objects" exactly | 1 |
| **Pixel-level coordinate localization** | **6%** — 2/36 hits | 36 |
| Format loss | **0/36** — the parsing criterion is not at fault | 36 |

**Key control experiment**: the prompt states a **wrong** location (50%, 50%).
The model outputs exactly the coordinate corresponding to that wrong location
(1 px from the hint, 273 px from the ground truth).

> **It never looks at the image; it only converts numbers. The visual pathway
> does not participate in inference.**

**Cross-wording consistency**: across 5 semantically equivalent phrasings
(including Chinese), 3 of 4 targets produce **byte-identical** outputs —
independent of what was asked.

### A number we got wrong, and corrected

An earlier version of this repository recorded "coordinate localization: 0/57 hits".

**That number was wrong.** It mixed three kinds of samples together:
12 came from an experiment whose **prompt leaked the answer** (invalid),
and 9 belonged to a **control group that was deliberately given the location
hint** (designed to hit). The real value after removing them is **2/36**.

`analyze.py` now contains `classify_run()`, which automatically separates
"no-hint grounding / control / invalid", **so this kind of contamination
cannot silently happen again.**

---

## Experiment design highlights

The full matrix, the plan for ruling out competing explanations, and the
reading rules are in [`docs/EXPERIMENT_DESIGN.md`](docs/EXPERIMENT_DESIGN.md).

### Four capability probes

| Probe | What it measures | How the ground truth is built |
|---|---|---|
| P1 Intent routing | Chinese spoken-language intent understanding | An existing 134-case benchmark (safety misses counted separately) |
| P2 Image content understanding | Can it read what is in the image | Synthetic images + manual scoring |
| P3 Spatial localization | Can it produce coordinates | **Solid-colour dots** — coordinates are unambiguous |
| P4 Long-session stability | The degradation threshold as context grows | Synthetic turn scripts |

**Why solid dots for P3**: any real UI element can be misidentified.
Solid dots remove all ambiguity — **a wrong answer can only be a capability
problem, not a recognition problem.**

### Three competing explanations that must be ruled out

1. Not caused by quantization — **the model is simply like this**
2. Not caused by quantization — a **resolution / input-processing** issue
3. Not a capability gap but **non-compliant output format** (the explanation most likely to overturn an existing conclusion)

### A known methodological boundary

**This project has no same-source FP/Q8 baseline, so it cannot prove that
"the dissociation is caused by quantization".**

It can only claim "capability dissociation is **observed** at the PTQ1
quantization level". It cannot claim "quantization **caused** the dissociation".
Supporting the latter requires cross-model comparison.

This limitation is written into
[`EXPERIMENT_DESIGN.md §5`](docs/EXPERIMENT_DESIGN.md) and
**is not hidden in the wording.**

---

## Project structure

```
engine/          llama.cpp KVMem fork (binaries, not included in this repo)
controller/      Anti-degeneration guardrails, long-term memory, image index
feishu-bot/      Remote entry point, intent routing, desktop operator layer
qwen/            0.8B intent-routing service
uia/             Windows UIA / OCR control-tree probes
experiments/     Experiment code and raw data
docs/            Design documents and findings
tests/           Automated tests
```

---

## What this project is *not*

In the interest of honesty, here is what this project **does not** do:

- ❌ **It is not a general Computer Use implementation.** Coordinate
  localization hits only 6% (2/36); desktop control uses the Windows UIA
  control tree (a deterministic API) rather than model vision.
- ❌ **It is not fully autonomous.** The architecture is
  "model understands intent → system API executes"; the model takes no part
  in coordinate decisions.
- ❌ **It does not prove that quantization caused the dissociation** (see above).
- ❌ **It is not a complete quantization study.** One quantization level,
  one model, four capability dimensions.

---

## Reproducing

### Environment

```
GPU:   RTX 4060 Laptop, 8GB VRAM
OS:    Windows
Model: Ternary-Bonsai-2-27B-PTQ1_0.gguf   (obtain separately)
Engine: llama.cpp KVMem fork              (build separately)
```

### Dependencies

```bash
pip install uiautomation winocr mss pillow
```

### Launch

```bat
start_kvmem_all.bat          REM start engine + proxy + routing service
start_kvmem_all.bat status   REM check status
start_kvmem_all.bat stop     REM stop
```

### Safe defaults

Every irreversible action is denied by default:

- The mouse/keyboard operator layer is **off by default**
- The send-target allowlist is **empty by default**
- Key combinations, Enter and Tab are **permanently denied** (explicit authorization required)
- The allowlist lives **in memory only and is cleared on restart**

Design rationale: [`docs/EXPERIMENT_DESIGN.md`](docs/EXPERIMENT_DESIGN.md)
and inline code comments.

---

## License

Code is covered by `LICENSE`.
**Model weights are not part of this repository** — obtain them separately and
comply with their original license.

---

## Citation

If the findings here are useful to you, you are welcome to cite them.
**If a conclusion does not hold in your environment, please open an issue** —
especially the dissociation phenomenon: I would very much like to know whether
it reproduces at other quantization levels.
