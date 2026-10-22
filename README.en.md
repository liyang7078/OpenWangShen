# OpenWangShen · Autofill Job Application Forms

> Stop retyping the same 30 fields on every campus-recruitment form.
> Your personal data **never leaves your machine**, and the tool **never submits for you**.

简体中文 | [English](README.en.md)

---

## What it is

A **local-first** form-filling tool for online job applications.

| Command | What it does |
|---|---|
| `check` | Validate your profile — which fields are missing |
| `fill` | Open a job page, detect form fields, fill them in (**no submit**) |
| `sheet` | If the page can't be opened, generate a copy-paste checklist |
| `mask` | Redact PII before asking an AI to help with essay questions |

## What it is NOT

- ❌ Not a mass-apply bot. It doesn't pick jobs, doesn't submit anything
- ❌ Doesn't bypass logins, never asks for credentials, never solves CAPTCHAs
- ❌ Never uploads your information anywhere

## Three hard rules

| # | Rule | Why |
|---|---|---|
| 1 | **Never submits** | Submission is irreversible and consumes your quota. You click it |
| 2 | **Never touches passwords or CAPTCHAs** | Detected and marked as a "human gate" |
| 3 | **PII never leaves your machine** | Values go from profile straight into the browser — no network, no LLM context |

## Quick start

```bash
# Requirements: Python 3.8+ (core features need stdlib only)
pip install playwright && playwright install chromium   # optional, for autofill

cp fixtures/档案_示例.json profile.json     # edit with your own info
python scripts/wangshen.py check --profile profile.json
python scripts/wangshen.py fill  --profile profile.json --url "https://..." --dry-run
python scripts/wangshen.py guide            # print the recommended path
```

## How it works

```
profile.json (local only)
      │
      ├─→ check ─────→ which fields are missing
      ├─→ mapping ───→ page label → profile key (187 fields / 735 labels)
      ├─→ fill ←──────┘  values go straight into the browser
      │      └─ can't open? → generate checklist → you paste manually
      └─→ mask ──────→ redact PII before sending text to an AI
```

**Key insight:** filling a form is mechanical — it needs no "thinking", so real
values never have to reach an AI. Tasks that *do* need thinking (writing essay
answers) don't need real values. **The two can be cleanly separated.**

## Privacy

- Whitelist-based redaction: only fields that can **uniquely identify a person**
  are masked (name / phone / ID / email / address).
  `gender=male`, `party member` are **not** masked — they identify nobody, and
  masking them just makes the text useless
- Post-mask verification re-scans for leftovers; refuses to output if any remain
- Mapping tables live in memory only — never written to disk or logs
- **Artifacts are never overwritten by default**: a name collision is saved under
  a new timestamped name instead (`--overwrite` to opt in). Writes are atomic
- **A zero-truth gate runs before any write**: feedback packages and the issue
  registry are **aborted** if they contain a phone number, ID, or local path
  (not warned, not redacted-and-allowed)

See [SECURITY.md](SECURITY.md) — including the **private** channel for reporting
security defects (please don't open a public issue for those).

## Testing

```bash
python scripts/wangshen.py selftest              # every script's built-in selftest
python -m unittest discover -s tests             # 151 unit tests, stdlib only
python scripts/dist_check.py                     # pre-release leak scan
python scripts/gen_issue_template.py --check     # issue template / option sets in sync
python scripts/doc_check.py                      # docs' numbers vs the actual assets
```

These are the same commands CI runs (Ubuntu, four Python versions including the
3.8 lower bound we promise, plus Windows).

## Disclaimer

This tool only fills forms. It guarantees no application outcome.
Please comply with the terms of service of any site you use it on.
**Always verify the filled content before submitting.**

## License

[MIT](LICENSE)

---

## Author

**Author: liyang7078**

Issues and pull requests are welcome.
