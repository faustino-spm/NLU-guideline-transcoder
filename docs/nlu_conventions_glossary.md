# Phoenix AI 3 NLU Database Conventions & Domain Glossary

Scraped directly from the live Phoenix AI 3 platform (`http://172.20.0.39:8088`: `/rule`, `/intent`, `/entity`) covering **33,200 rule rows across 1,057 unique rules** and **23,429 regex patterns across 1,611 intents**.

---

## 1. Site Architecture & Data Sources

- **`/rule`** (`/api/proxy?endpoint=rules%2F`): 33,200 rows, 1,057 rules, 16 JSON fields (`id`, `rule_name`, `placement_name`, `passing_score`, `speaker`, `component`, `component_score`, `component_condition_id`, `checklist_name`, `intent`, `entity`, `special_condition`, `passing_component_remarks`, `failing_component_remarks`, `failing_intent_remarks`, `fail_recommendation`).
  - `placement_name` is `default_placement` on **33,200 / 33,200** rows (100%).
  - `entity` is `null` on **33,198 / 33,200** rows (effectively unused; 2 rows are stray typos).
- **`/intent`** (`/api/proxy?endpoint=patterns%2F`): 23,429 Taglish/English regex patterns (`intent`, `speaker`, `regex`, `id`) across 1,611 unique intents.
  - **Key authoring workflow insight:** In the newest batch of rules (`id >= 80,000`, 201 rules / 6,435 rows), **3,893** intent references already exist in `patterns/` and **2,542** do not yet have patterns—meaning rule CSVs are authored first with clean semantic `*_intent` names, and regex patterns are maintained separately.

---

## 2. Verified Terminology: `RPC` = **Right Party Contact** (Not Person)

Checked across all **1,057 rule names** and **33,200 rows**:
- `RIGHT_PARTY_CONTACT` appears in **64 rules**; `THIRD_PARTY` appears in **71 rules**; `"Right Party"` appears in **209 rows** across components/remarks.
- `RIGHT_PERSON_CONTACT` appears in **0 rules**; `THIRD_PERSON` appears in **0 rules**; `"Right Person"` appears in **0 rows** (`PERSON` only appears in `PERSONAL_LOAN`).
- Both client QA PDFs (`Agency Call Monitoring Form v.2.pdf` and Security Bank ACA PDF) explicitly print **"Right Party Contact"**.

---

## 3. Chronological Evolution & Latest Non-Legacy Conventions (`id >= 80,000`)

Sorting all 1,057 rules by `max(id)` shows the literal newest production rules (`id = 85,000–86,472`: `EWB_*`, `BANKARD_*`, `BPI_RBANK_*_SL_*`, `BPI_PL_*_SL_*`):

1. **1:1 Client-Rubric Mirroring on `RIGHT_PARTY_CONTACT` & `PTP`**
   - The newest rules (`EWB_PTP`/`RIGHT_PARTY_CONTACT`, `BANKARD_RIGHT_PARTY_CONTACT`, `BPI_RBANK_*_SL_PTP`/`RIGHT_PARTY_CONTACT`) mirror the client form 1:1 for `RPC`/`PTP`, while delegating non-RPC dispositions (`PTP_FOLLOW_UP`, `PTP_REMINDER`, `CLAIMING_PAID`, `UNRELATED`, and often `THIRD_PARTY_CONTACT`) to the shared 7-component SPM template.
   - Note on defect-deduction forms (`EWB_PTP`, identical template to `ORICO PHONE MON.xlsx`): the newest `EWB_PTP` rows put the literal defect tier values (`25` = Critical, `15` = Important, `10` = Essential) into `component_score` and tag `Critical` rows as `auto_fail` / `negate,auto_fail`.
2. **`Speaker` Convention (`agent`: 29,985 | `client`: 3,215)**
   - `client` is used almost exclusively for:
     - **`Risk Level`** (**2,806 / 2,887** Risk Level rows = 97.2%): classifies borrower-spoken risk cues (`high_risk_intent` = medical/financial distress like `"nahospital"`/`"walang ipon"`, `medium_risk_intent` = willing to pay on payday, `low_risk_intent` = ready for full/lump-sum settlement).
     - **`active_listening_intent`** (**264** rows + campaign variants): detects borrower frustration (`"paulit-ulit ka naman"`, `"hindi ka ba nakikinig"`), paired with `special_condition = negate`.
3. **`Risk Level` Standard Block (Newest Format)**
   - `passing_score = 80`, `speaker = client`, `component = Risk Level`, `component_score = null`, `component_condition_id = 1`.
   - `High Risk` (`high_risk_intent`, `r1`, `passing_component_remarks = "High Risk"`), `Medium Risk` (`medium_risk_intent`, `r2`, `"Medium Risk"`), `Low Risk` (`low_risk_intent`, `r3`, `"Low Risk"`), with failing remarks & recommendation left empty.
4. **`Passing Score`**
   - `80` is the dominant default (**24,905 / 33,200** rows, and uniform across the newest `EWB_*`, `BANKARD_*`, and `BPI_*_SL_*` rules).
5. **`Component Condition ID` (`1`: 31,889 | `>1`: 1,311)**
   - `1` = default AND group.
   - `2..N` = OR-alternative combinations, overwhelmingly used for **Positive ID / Account Verification** (e.g., `cid=1`: Name+Birthday, `cid=2`: Name+Address, `cid=3`: Birthday+Address).
6. **Active `Special Condition` Vocabulary (Top Values)**
   - `""` (normal positive audio check, 12,215)
   - `auto_fail` (5,130) — mandatory PID, Call Recording Disclosure, ZTP/Critical gates
   - `auto_pass` (3,981) — non-audio CRM/documentation/SLA/escalation checks
   - `not_applicable` (3,525) — disposition-gated items (e.g., PTP items on `RPC`, 3PC items on `RPC`/`PTP`)
   - `auto_fail,negate` / `negate,auto_fail` (1,303) — prohibited behaviors (profanity, harassment, unfair debt collection, misrepresentation)
   - `r1` / `r2` / `r3` (984–985 each) — `Risk Level` classification tiers
   - `voice intent must be less than 0.55` (662) / `0.20` (99) — acoustic pause-ratio / call-pace / hold-time checks
   - `voice intent must be between -10 and -5` (672 incl. whitespace variants) / `-20 and -5` (88) — acoustic loudness checks
   - `negate` (530) — non-ZTP undesirable checks (`active_listening_intent`, jargon)
   - `priority1`–`priority5` (444 / 467 / 257 / 194 / 55) — tiered negotiation / payment hierarchy
   - `2_sentences_required` (311) & `auto_fail,2_sentences_required` (161) — requires two distinct utterances (e.g., "Stick Twice on OB", two closing attempts, two-factor PID)
7. **Remarks & Recommendations Formula (Newest Rules)**
   - Dominant subject is **"The collection specialist"** (3,214 rows in `id >= 80,000` vs. 794 `"The agent"`):
     - `Passing Component Remarks`: `"<Behavior> was <established/completed>."` or `"The collection specialist <did X>."`
     - `Failing Component Remarks`: Negative mirror (`"was not..."` / `"did not..."`).
     - `Failing Intent Remarks`: `"The collection specialist failed to <do X>."`
     - `Fail Recommendation`: `"Ensure the collection specialist <does X>."`

---

## 4. Domain Abbreviation & Phrase Glossary (Derived from Live DB & Regexes)

### Call Dispositions & Contact Types
| Abbreviation | Meaning | Live DB Evidence |
| --- | --- | --- |
| **RPC** | **Right Party Contact** | 64 `*_RIGHT_PARTY_CONTACT` rules, 140 `*_RPC` rules, 209 text rows (0 `Right Person`). |
| **PTP** / **REPTP** | **Promise to Pay** / **Re-Promise to Pay** | `NEWPTP_REPTP`, `ptp_payment_amount_confirmation_intent`. |
| **BP** | **Broken Promise** | `FOLLOW_UP_BP`, `MBTC_30DPD_BP`, `bdo_broken_promise_intent` (*"Broken promises were acknowledged..."*). |
| **RBP** | **Reason for Broken Promise** | `reason_for_delinquency_or_broken_promise_intent` (*"reason for delay in payment (RFD)/ broken promise (RBP)"*). |
| **RTP** | **Refusal to Pay** | `BPI_*_SL_RPC_RTP`, `rtp_closing_spiel_intent`. |
| **3PC** / **TPC** | **Third Party Contact** | `*_3PC`, `*_TPC`, `*_THIRD_PARTY_CONTACT`, `tpc_third_party_identification_intent`. |
| **UN** | **Unrelated (Party)** | `un_name_of_unrelated_intent`, `un_disregard_the_call_intent`. |
| **FFUP** | **Follow-Up** | `ffup_reason_for_broken_promise_intent`, `ffup_ask_new_ptp_sched_intent`. |

### Verification, Account & Negotiation Terms
| Abbreviation | Meaning | Live DB / Regex Evidence |
| --- | --- | --- |
| **PID** | **Positive Identification** | `pid_full_name_intent`, `birthdate_verification_intent`, `declared_address_verification_intent` (*"verified the debtor's personal identification details"*). |
| **CH** | **Cardholder** | `mentioned_ch_full_name_intent`, `asked_if_ch_received_demand_or_notification_letter_intent`. |
| **CM** | **Cardmember** *(or Collection Module/System in MBTC)* | `ask_cm_capacity_to_pay_intent` (*"assess the borrower's capacity to pay"*); `mbtc_30dpd_CM_recorded_properly_intent` (*"Account Handling & Tagging in CM — recorded call notes in the system"*). |
| **OB** / **TOB** | **Outstanding Balance** / **Total Outstanding Balance** | `ob_intent`, `tob_one_time_payment_intent` (*"offered or discussed TOB as a one-time payment option"*). |
| **MAD** | **Minimum Amount Due** | `psba_mad_intent`, `mad_intent` (regex: `minimum.*due`, `minimum.*amount`). |
| **RFD** | **Reason for Delinquency / Delay** | `rfd_intent` (regex: `bakit/ano.*reason/dahilan.*delay/delinquent/past due`). |
| **SOI** / **SOF** | **Source of Income** / **Source of Funds** | `sof_intent` (regex: `source of funds/income`). |
| **OTP** | **One-Time Payment** | `tob_one_time_payment_intent`, `ubp_discount_otp_intent`, `principal_otp_intent`. |
| **PIF** | **Pay in Full** | `H1_pif_today_intent`, `edc_pif_tomorrow_intent`. |
| **DP** | **Down Payment** | `dp_intent`, `bnb_sl_prelegal_down_payment_intent`. |
| **EPA** | **Extended Payment Arrangement** *(regex also matches "Estimated Partial Amount")* | `bpi_pl_epa_intent` (*"offered an extended payment arrangement"*), `bpi_epa_intent` (regex: `estimated.*partial.*amount`, `monthly.*installment`). |
| **SPA** | **Special Payment Arrangement** *(amnesty/waiver in negotiation; **Special Power of Attorney** in ID verification)* | `bdo_h3_spa_intent` (*"clearly explained the amnesty offer"*); SBC PDF (*"authorized handler/SPA"*). |
| **VS** | **Voluntary Surrender** *(auto/vehicle loans)* | `mc3_vs_intent` (regex: `(masurrender\|surrender).*(sasakyan\|car\|kotse\|auto\|motor\|unit)`). |
| **MRI** | **Mortgage Redemption Insurance** | `pif_mri_intent` (regex: `death.*certificate`, `MRI`). |
| **MOP** | **Mode of Payment** | `bpi_pl_recap_mop_intent` (*"summarized the mode of payment"*). |
| **BTC** / **ETA** | **Best Time to Call** / **Estimated Time of Availability (Arrival)** | `probed_btc_intent`, `probed_eta_intent` (*"confirmed the borrower's availability"*). |
| **IC4** | **IC4 Collections System / Memo** *(RCBC Bankard)* | `ic4_memo_accuracy_intent` (*"Updated IC4 Memos based on correct information and payment details"*). |

### Campaign, Product & Operations Abbreviations
| Abbreviation | Meaning | Live DB Context |
| --- | --- | --- |
| **SPM** | **S.P. Madrid & Associates** *(also marks the shared 7-component internal template)* | `*_SPM_*` (192 rules). |
| **SL** | **Soft-Call / Late-Stage or Standardized-List BPI/BDO/BNB campaign tag** *(122 rules)* | `BPI_RBANK_PL_60DPD_SL_*`, `BPI_CARDS_30DPD_SL_*`, `BDO_AUTO_LOAN_SL_*`, `BNB_SL_Prelegal_*`. |
| **DPD** / **XDAYS** | **Days Past Due** / **X-Days early delinquency bucket** | `30DPD`, `60DPD`, `BPI_CARDS_XDAYS_SL_*`. |
| **PWO** / **WOFF** | **Pre-Write-Off** / **Write-Off** | `RCBC Cards PWO`, `BPI_CARDS_PWO_LUZ_CC_*`, `MBTC_CARDS_WOFF_PL_*`. |
| **RECOV** / **RECO** | **Recovery** | `BPI_RBANK_CARDS_RECOV_*`, `SBC_CARDS_RECOV_L1_*`, `BPI_CARDS_RECO_LUZ_*`. |
| **PL** / **BL** / **CC** | **Personal Loan** / **Business Loan** / **Credit Card** | `BPI_RBANK_PL_*`, `BPI_BL_*`, `BPI_CARDS_*_CC_*`. |
| **LUZ** | **Luzon** *(regional portfolio)* | `BPI_CARDS_PWO_LUZ_CC_*`, `BPI_CARDS_RECO_LUZ_1/2_*`. |
| **EDC** / **QAFE** / **ZTP** | **Early Delinquency Collections** (TALA) / **QA Framework Evaluation** / **Zero Tolerance Policy** | `TALA_EDC_*`, QAFE PDF. |
| **ACA** / **SLA** | **Accredited Collection Agency** (Security Bank) / **Service Level Agreement** | SBC ACA PDF (*"Submission of call recording beyond ACA SLA"*). |
| **TL** / **MCC** / **SOP** | **Team Leader** / **Metrobank Card Corp.** / **Standard Operating Procedure** | `mbtc_30dpd_tl_escalation_intent`, `mbtc_30dpd_mcc_sop_escalation_intent`. |
| **BNPL** / **SALAD** | **Buy Now Pay Later** (Lazada) / **Salary Advance** (SB Finance) | `LAZADA_BNPL_SPM_*`, `SBF_SALAD_SPM_*`. |
| **ROB** | **Robinsons Bank** *(legacy Robinsons auto/bikes intents sometimes reused on Unrelated)* | `ROB_AUTO_REMINDER`, `ROB_BIKES_RPC`, `rob_name_of_unrelated_intent`. |
| **EWB** / **EWBC** | **EastWest Bank / EastWest Bank Corporation** | `EWB_PTP`, `EWB_60DPD_PL_*`, `ewbc_collection_agency_code_of_conduct_intent`. |
