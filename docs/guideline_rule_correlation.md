# QA Guideline-to-Rule Correlation Overview

High-level synthesis comparing client QA guideline attachments (`Guidelines Sample.xlsx`) against live NLU rule catalogs (`http://172.20.0.39:8088/rule`) across **5 representative sub-campaigns** and their disposition variants.

---

## 1. Two Rule-Authoring Patterns

Across the matched sub-campaigns, live NLU rules follow one of two structural strategies:

| Pattern | Sub-Campaigns | How Client Guidelines Map to Live Rules |
| --- | --- | --- |
| **A. 1:1 Client-Rubric Mirroring** | **TALA EDC**, **SBC Auto Loan Curing**, **BPI Rbank Instabale** *(also MBTC 30DPD, RCBC)* | Live `component` and `checklist_name` values are lifted directly from the client's QA form, preserving the client's pillar taxonomy and sub-criteria. |
| **B. Shared 7-Component SPM Template** | **Orico Auto Loan**, **SBC Cards B2** *(also HomeCredit, SBC Cards L6/Recov L1/GEOX/Legal)* | Client forms (whether defect-based Excel or weighted PDF) are **not** mirrored structurally. Core behaviors are mapped onto a standardized 100-point internal template (`Verification and Introductory`, `Negotiation Skills`, `Communication Skills`, `Call Quality`, `Documentation`, `Compliance`, `Risk Level`), with campaign customization concentrated in `Negotiation Effectiveness` (`priority1`–`priority4`) and `Compliance`. |

---

## 2. Sub-Campaign Summary

| Sub-Campaign | Attachment Format | Live Variants (Total Rows) | Pattern | Core Score Behavior | Key Correlation & Gaps |
| --- | --- | --- | --- | --- | --- |
| **TALA EDC** | 4-page QAFE PDF (7 pillars, 21 checkpoints = 100 pts, 8 ZTPs) | 6 (`196` rows; `PTP`/`RPC` = `56`) | **1:1 Mirror** | `99.99` on `PTP`/`RPC`; `30`–`50` on pruned variants | All 21 checkpoints + 8 ZTPs map 1:1. Broad checkpoints expand into SOP-driven multi-intents (e.g., `Follow Policies` → 12 rows including `H1`–`H5` negotiation hierarchy). `19/56` rows are `auto_pass`; only `4/8` ZTPs trigger `auto_fail`. |
| **Orico Auto Loan** | Defect-based Excel (`Critical=25`, `Important=15`, `Essential=10`) | 6 (`136` rows; `PTP` = `31`, `RPC` = `28`) | **SPM Template** | `100` on `PTP`/`RPC`; `66`–`82` on pruned variants | Translates 22 defect items into the 100-pt SPM template. Drops Excel's fee computation, jargon check, conversation control, and closing-question items; adds `Loudness`, `Pause Ratio`, `Stick Twice on OB`, and `Risk Level`. |
| **SBC Cards B2** | Security Bank ACA PDF (`100.00` pts, RPC + 3PC branches) | 7 SPM (`169` rows) + 2 legacy (`73` rows) | **SPM Template** | `100` (case-insensitive) across **all 7 SPM variants** | Maps PDF's negotiation hierarchy (`priority1`–`priority4`) and BSP 454/857/R.A. 4200 policies (`auto_fail`), but zeros out the PDF's 15-pt `Documentation` section to `auto_pass` and omits risk mitigation / alternate payment channels. |
| **SBC Auto Loan Curing** | Security Bank ACA PDF (`100.00` pts, same form family as Cards B2) | 7 (`142` rows; `PTP_REPO` = `29`, `RPC` = `28`) | **1:1 Mirror** | Unnormalized: `110`–`165` on RPC/Follow-up/Paid; `100` on `TPC`/`UNRELATED` | Mirrors the ACA PDF's 6 sections directly (`Opening the Call`, `Handling Objections & Soft Skills`, `Collection Call Proper RPC`, `Closing The Call`, `Documentation`, `Policies`). Assigns `15` pts each to `Policies` (`45` total) instead of `0`-pt auto-fail gates. |
| **BPI Rbank Instabale** | BPI/RBANK `.xls` (`Regulatory 40%`, `Service Quality 30%`, `Call Skills 30%`) | 5 (`152` rows; `RPC` = `40`, `FOLLOW_UP_BP` = `38`) | **1:1 Mirror** | `92.52` on `RPC`/`FOLLOW_UP_BP`; `79.32` on `REMINDER`/`PAID`; `55.35` on `3PC` | Mirrors the 3 XLS pillars and fractional sub-weights (`40.00` + `30.00` + `22.52`), expanding negotiation hierarchy into `priority1`–`priority4`. `14/40` `RPC` rows are `auto_pass` (mostly `Service Quality` CRM/SLA checks). |

---

## 3. Disposition Branching Across Variants

Every campaign uses distinct rule rows per call disposition, following two mechanisms:
1. **Condition Gating (same skeleton, flipped `special_condition`):** E.g., `TALA_EDC_PTP` vs. `TALA_EDC_RPC` (10 PTP rows flip to `not_applicable` or `auto_pass`), and `SBC_CARDS_CURING_B2_SPM_PTP` vs. `RIGHT_PARTY_CONTACT` (`recap_intent` → `not_applicable`).
2. **Template Pruning & Injection (non-RPC outcomes):** `FOLLOWUP`, `REMINDER`, `CLAIMING_PAID`/`FULLYPAID`, `TPC`/`3PC`, and `UNRELATED` drop probing/negotiation rows and inject standardized cross-campaign checklists (`ffup_*`, `reminder_*`, `claiming_paid_*`, `3pc_*`, `rob_*`). Except for `SBC_CARDS_CURING_B2_SPM_*`, pruned variants usually **do not** re-normalize `component_score` to `100`.

---

## 4. Key Gaps & Authoring Drift (Implications for Transcoding)

- **Not derivable from client attachments alone:**
  - Multi-intent expansion inside broad rubric items (e.g., negotiation hierarchies `H1`–`H5` / `priority1`–`priority4`) comes from **campaign SOPs/playbooks**.
  - `passing_score` (`80`), `placement_name` (`default_placement`), acoustic checks (`Loudness`, `Pause Ratio`), `Risk Level` (`r1`–`r3`), and non-RPC disposition templates (`3PC`, `UNRELATED`, `CLAIMING_PAID`, `REMINDER`, `FOLLOWUP`) are **platform conventions**.
- **Non-audio / CRM checks default to `auto_pass`:** System tagging, documentation accuracy, SLA/calling-period checks, and subjective soft-skill items are routinely retained for rubric completeness as `auto_pass`.
- **Manual authoring drift to guard against:**
  - **Score & casing bugs:** Unnormalized sums (`55.35`–`165`) and case mismatches (e.g., `Opening and closing` vs. `Opening and Closing` causing `105` in `SBC_CARDS_CURING_B2_SPM_PTP`, or `"Regulatory Compliance"` typo in `BPI_RBANK_INSTABALE_3PC`).
  - **Stale remarks & mismatched intents:** Copy-pasted `failing_intent_remarks` (e.g., `consent_to_record` saying *"Failed to verify debtor's name"* in `ORICO_PTP`), borrowed cross-bank intents (e.g., `bdo_*` / `bpi_*` and `pushback_test_roni` in non-SPM `SBC_CARDS_CURING_B2`), and inconsistent `auto_fail` / `speaker` flags across variants.
