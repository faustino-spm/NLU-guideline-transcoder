# 02 — Complete 15 Sub-Campaign Audit, Authoring Patterns & Disposition Matrix

Consolidates the initial 5 sub-campaigns from [guideline_rule_correlation.md](file:///C:/Users/SPM/Documents/NLU-guideline-transcoder/docs/guideline_rule_correlation.md) with the full audit of the remaining 10 sub-campaigns and all disposition derivatives conducted in this session.

---

## 1. Attachment Inventory & Deduplication

Across the 15 sub-campaigns listed in `Guidelines Sample.xlsx`, the 16 attached files deduplicate by MD5 hash to **8 unique guideline documents**:

| Unique Attachment | MD5 | Shared By Sub-Campaigns | Format & Special Notes |
| --- | --- | --- | --- |
| `SPM Calibration - Sept 2026.xlsx` | `5767cc0a` | HomeCredit Fintech | 13 weighted questions (`Q1`–`Q13` = 100%) + 2 compliance flags (CTQ / Non-Critical) |
| `HC CALL GUIDELINES.docx` | `0570a4d1` | HomeCredit Fintech | Only 28 chars of text; actual content is **2 embedded NotebookLM PNG slides** (4-step call standard: Opening, Acknowledgement, Probing, Focus Points) |
| `CALLMON_2026_MADRID.xlsx` | `adce5d8e` | MBTC Cards 30DPD | **Blocked:** 3.2 MB OLE2-encrypted / password-protected (`StrongEncryptionDataSpace`) — unreadable without password |
| `ORICO PHONE MON.xlsx` | `88ba9b40` | Orico Auto Loan | Defect-deduction Excel (`Critical=25`, `Important=15`, `Essential=10`); same EWBC form family as live `EWB_*` |
| `RBANK Call Quality Monitoring Form.xls` | `2caa5f5c` *(×3 identical)* | BPI Rbank CC, Instabale, PL | 3 weighted pillars (`Regulatory Compliance 40%`, `Service Quality 30%`, `Call Skills 30%`), PASS/FAIL/N/A |
| `Agency Call Monitoring Form v.2.pdf` | `50b20ac5` | RCBC Cards PWO | 1-page RCBC Bankard PDF, 4 unweighted Yes/No/N/A sections (`Call Opening`, `Call Proper [RPC/3PC]`, `Quality of Call`, `Closing of Call`) |
| Security Bank ACA PDFs (`MAY 2026` ×3, `June 2026` ×2, `06.10.26(ACA)` ×2) | `c9d92941`, `7834d232`, `87f0e2a4` | All 6 SBC sub-campaigns | **Exact same 1-page Security Bank ACA Quality Evaluation Form**, just filled in for 3 different sample calls (Auto Loan, Business Express Loan, Credit Card Field) |
| `EDC QA Framework Evaluation Form - SPM - QAFE Form.pdf` | `ab075cad` | TALA EDC | 4-page QAFE PDF: 7 Core Framework pillars (21 checkpoints = 100 pts) + 8 Zero Tolerance Policies + footnotes `[1]`–`[15]` |

---

## 2. Three Live Rule-Authoring Patterns Discovered

Across all 15 sub-campaigns, live DB rules follow **3 distinct structural encodings** (not 1):

1. **Pattern A — 1:1 Pillar-Level Mirror:** Client form sections → `Component`, client criteria → `Checklist Name` (`TALA_EDC`, `SBC`, `SBC_AUTO_CURING`, `MBTC_30DPD`, `BANKARD_PTP`/`RIGHT_PARTY_CONTACT`, `RCBC_CARDS_LEGAL_RIGHT_PARTY_CONTACT`, legacy `BPI_RBANK_*_RPC`, and newest `EWB_*_PTP`/`RIGHT_PARTY_CONTACT`).
2. **Pattern B — 1:1 Item-Level Mirror (Flattened):** Each client sub-criterion becomes its own `Component` (23–32 components) with verbatim form wording pasted into `Checklist Name` (newer `BPI_RBANK_CARDS_30DPD_SL_*`, `BPI_RBANK_PL_30DPD_SL_*`, `BPI_RBANK_PL_60DPD_SL_*`).
3. **Pattern C — Shared 7-Component SPM Template:** Discards the client form's taxonomy and maps behaviors onto SPM's standardized 100-pt template (`Verification and Introductory`, `Negotiation Skills`, `Communication Skills`, `Call Quality`, `Documentation`, `Compliance`, `Risk Level`) — used by `HOME_CREDIT_*`, `ORICO_*`, `SBC_CARDS_*_SPM_*`, `MBTC_*_SPM_*`, `RCBC_Legal_NEW_*`, and **also used for non-RPC dispositions inside newer 1:1 campaigns**.

---

## 3. Complete 15 Sub-Campaign Correlation Table

> **0 exact rule-name matches** exist between `Guidelines Sample.xlsx` sub-campaign names and the live DB dropdown.

| # | Sub-Campaign | Best Matching Live Rule Family(ies) | Mapping Judgment | Pattern | Key Findings & Drift |
| :-: | --- | --- | --- | --- | --- |
| 1 | **TALA EDC** | `TALA_EDC_*` (6 variants, 196 rows) | **True 1:1** (`PTP`/`RPC`) | Pattern A | All 21 checkpoints + 8 ZTPs mapped; `19/56` `auto_pass`; `4/8` ZTPs `auto_fail`; SOP-expanded `H1`–`H5`. |
| 2 | **BPI Rbank Instabale** | `BPI_RBANK_INSTABALE_*` (5 variants, 152 rows) | **True 1:1** (`RPC`) | Pattern A | Exact fractional weights (`6.67`, `4.44`, `2.5`, `3.75`, `1.88`); `92.52` on `RPC` (missing `7.48` pts); `14/40` `auto_pass`. |
| 3 | **BPI Rbank CC** | `BPI RBANK CARDS 30DPD`, `BPI_RBANK_CARDS_RECOV_*`, `BPI_RBANK_BUCKET_4_*`, `BPI_RBANK_CARDS_30DPD_SL_*` | **True 1:1** (in 2 encodings) | Pattern A + B | Legacy rules use 3 pillars; newer `_SL_` rules flatten 23–32 criteria into components with verbatim XLS text. |
| 4 | **BPI Rbank PL** | `BPI_RBANK_PL_*`, `BPI_RBANK_PL_30DPD_SL_*`, `BPI_RBANK_PL_60DPD_SL_*` | **True 1:1** (in 2 encodings) | Pattern A + B | Same split as CC; `BPI_RBANK_PL_RPC` omits `auto_fail` on PID/recording unlike `CARDS_RECOV_RPC`. |
| 5 | **RCBC Cards PWO** | `BANKARD_*`, `RCBC_CARDS_LEGAL/PRELEGAL_*`, `RCBC_Legal/Prelegal_*`, `RCBC_*` | **Inconsistent — 3 mappings coexist** | A + C + Wrong-Bank | `BANKARD_PTP`/`RCBC_CARDS_LEGAL_RIGHT_PARTY_CONTACT` = **Almost 1:1** (invented `6.67`/`2.86`/`5`/`4` weights); `RCBC_Legal_NEW_*` = Pattern C; **`RCBC_RPC`/`PTP` literally copy-pasted Security Bank (`SBC`) rubric + `sbc_*` intents!** |
| 6 | **SBC Auto Loan Curing** | `SBC_AUTO_CURING_*` (7 variants, 142 rows) | **Almost 1:1** (`RPC`/`PTP_REPO`) | Pattern A | Mirrors all 6 ACA sections; inflates `Policies` to `15` pts each (`45` pts total → unnormalized `110`–`165`). |
| 7 | **SBC Auto Loan** | Generic `SBC` (1 rule, 28 rows) | **Unmapped specifically; generic `SBC` is ~1:1** | Pattern A | Matches exact ACA PDF weights (`5,1,1,6,4,2,3,3,10,8,18,...15`), omitting `Policies`. |
| 8 | **MBTC Cards 30DPD** | `MBTC_30DPD_*` (7 variants) | **Blocked on encrypted file; live rule looks 1:1** | Pattern A | Bespoke pillars (`Opening the Call`, `Positive Identification`, `Problem Solving and Collection Skills`, `Account Handling & Tagging in CM`, etc.) + `priority1`–`priority5`; other MBTC buckets use Pattern C. |
| 9 | **Orico Auto Loan** | `ORICO_*` (6 variants, 136 rows) | **Not 1:1 (Re-templated)** | Pattern C | Maps 22 EWBC defect items into 7-component SPM template; drops fee computation/jargon/closing questions; adds `Loudness`/`Pause Ratio`. *(Contrast: newest `EWB_PTP` mirrors the exact same form 1:1!)* |
| 10 | **SBC Cards B2** | `SBC_CARDS_CURING_B2_SPM_*` (7 + 2 legacy) | **Not 1:1 (Re-templated)** | Pattern C | All 7 SPM variants normalize to `100` (case-insensitive); zeros out 15-pt `Documentation` to `auto_pass`; legacy rules pull in `bdo_*`/`bpi_*` + `pushback_test_roni`. |
| 11 | **HomeCredit Fintech** | `HOME_CREDIT_*` (7 variants) | **Not 1:1 (Re-templated)** | Pattern C | Ignores both `Q1`–`Q13` calibration Excel and 4-step slide deck; maps into 7-component SPM template. |
| 12 | **SBC Auto Loan Recovery** | None dedicated *(generic `SBC`, `SBC_Legal_*`, `SBC_Prelegal_*`)* | **Unmapped** | — | `SBC_Legal_*`/`Prelegal_*` use Pattern C. |
| 13 | **SBC Insurance** | None | **No live rule found** | — | — |
| 14 | **SBC Cards B2 Field** | None dedicated *(likely shares `SBC_CARDS_CURING_B2_SPM_*`)* | **No dedicated rule** | — | — |
| 15 | **SBC Cards B4** | None *(catalog has `B2`, `RECOV_L1`, `LOAN_L6`, `GEOX`)* | **No live rule found** | — | All other `SBC_CARDS_*_SPM_*` rules use Pattern C. |

---

## 4. Disposition Derivative Matrix & Consistency Findings

### Standard 7-Disposition Baseline vs. Actual Live Coverage

| Rule Family | Count | RPC | PTP | Follow-Up / BP | Reminder | Claiming / Fully Paid | 3PC / TPC | Unrelated | Extra / Anomalies |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | --- |
| `HOME_CREDIT_*` | 7 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Complete 7/7 |
| `MBTC_30DPD_*` | 7 | ✅ | ✅ | ✅ (`BP`) | ✅ | ✅ | ✅ | ✅ | Complete 7/7 |
| `ORICO_*` | 6 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | Missing `UNRELATED` |
| `TALA_EDC_*` | 6 | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | Missing `FOLLOW_UP` |
| `SBC_AUTO_CURING_*` | 7 | ✅ | ✅ (`PTP_REPO`) | ✅ | ✅ | ✅ | ✅ | ✅ | Complete 7/7 |
| `SBC_CARDS_CURING_B2_SPM_*` | 7 (+2) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | +2 legacy non-SPM (`RPC`, `PTP`) |
| Other `SBC_*_SPM_*` (`RECOV_L1`, `LOAN_L6`, `PL_RECOV_L1`) | 6 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | Missing `REMINDER` (`GEOX` has 7/7) |
| `BPI_RBANK_INSTABALE_*` / `BPI_RBANK_PL_*` | 5 | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | Missing `PTP`, `UNRELATED` |
| `BPI_RBANK_CARDS_RECOV_*` | 6 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | Missing `UNRELATED` |
| `BPI_RBANK_*_SL_*` (`CARDS_30DPD`, `PL_30DPD`, `PL_60DPD`) | 7–9 | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | Missing `FOLLOW_UP`; **adds `PAYMENT_CURED`** (+2 legacy `RPC_PTP`/`RPC_RTP`) |
| `RCBC_*` / `RCBC_Prelegal_*` / `RCBC_CARDS_*` | 7 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | Typo: `BRCBC_CARDS_LEGAL/PRELEGAL_PTP` |
| `RCBC_Legal_*` | 6 | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | Missing `FOLLOW_UP` |
| `BANKARD_*` | 11 | ✅×2 | ✅×2 | ✅×2 | ✅ | ✅×2 | ✅ | ✅ | Duplicate old + new rules live simultaneously |

### Deviation Consistency Judgment
- **Conceptually consistent:** Across all 14 compared families, every disposition performs the same logical operation relative to `RPC` (`PTP` adds/activates capture+recap; `FOLLOW_UP`/`BP` swaps in broken-promise + new-schedule checks; `REMINDER` swaps in due-date/amount reminders; `CLAIMING_PAID` swaps in 3–4 settlement checks; `3PC` strips borrower account info and adds 3rd-party/callback probing; `UNRELATED` strips to callee ID + disregard call).
- **Structurally inconsistent:**
  1. **Two competing shared-template generations:** Older SPM (`ffup_*`, `reminder_*`, `claiming_paid_*`, `3pc_*`, `un_*`; loosens acoustic checks on 3PC/UNRELATED) vs. Newer SPM (`opening_greeting_intent`, `settlement_date_intent`, `callee_identification_intent`, `disregard_call_intent`; fixed 18 rows for `3PC`, 20 for `UNRELATED`).
  2. **Mid-family pattern switching:** Newer 1:1 campaigns (`EWB_*`, `BPI_RBANK_*_SL_*`, `RCBC_CARDS_LEGAL_*`) use **1:1 Client-Rubric Mirroring on `RPC`/`PTP`** and then **swap wholesale to the 7-Component SPM Template** on non-RPC dispositions (`UNRELATED`, `THIRD_PARTY_CONTACT`, `PTP_FOLLOW_UP`, `PTP_REMINDER`, `CLAIMING_PAID`).
  3. **Inconsistent `PTP` vs. `RPC` mechanics:** Some flip `special_condition` (`TALA_EDC`, `SBC_CARDS_B2_SPM`, `RCBC_CARDS_LEGAL`, `EWB_*`), some add 1–8 rows (`ORICO`, `MBTC_30DPD`, `BPI_*_SL_*`), and `RCBC_Legal_NEWPTP_REPTP` has 0 diffs from `NEW_RPC`.
