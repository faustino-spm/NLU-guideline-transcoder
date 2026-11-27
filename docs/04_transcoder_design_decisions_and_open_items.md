# 04 — Transcoder Design Takeaways, Edge Cases & Open Decisions

High-level engineering guidance and remaining open questions derived from the full audit and prompt-validation campaign.

---

## 1. What Can vs. Cannot Be Derived From Client Attachments Alone

| Category | Derivable From Client Attachment? | How [prompt.txt](file:///C:/Users/SPM/Documents/NLU-guideline-transcoder/prompt.txt) Handles It |
| --- | :---: | --- |
| Section/pillar taxonomy (`Component`) & criteria (`Checklist Name`) | **Yes (~85–95%)** | Mirrored 1:1 from the blank underlying rubric (ignoring filled-in sample call scores/notes). |
| Explicit weights / percentages (`Component Score`) | **Yes** *(when printed)* | Preserved verbatim when present; distributed proportionally (defect tiers) or evenly (Yes/No forms) to sum to `100` when absent. |
| ZTP / Compliance `auto_fail` & `auto_fail,negate` gates | **Yes** | Mapped with empty `Component Score` when 0-pt gates, and `auto_fail` / `auto_fail,negate`. |
| Non-audio CRM / SLA / documentation checks | **Yes** | Retained for rubric completeness with `Special Condition = auto_pass`. |
| Campaign SOP negotiation tiers (`priority1`–`priority4`) | **No (SOP-derived)** | Expanded into standard 4-tier payment hierarchy (`one_time_payment` → `discounted_offer`/`split_amount` → `installment` → `partial_payment`) whenever a checklist covers negotiation hierarchy or payment options. |
| Platform conventions (`default_placement`, `Passing Score = 80`, `Risk Level` `r1`–`r3`, `active_listening_intent`, `voice intent < 0.55`) | **No (Platform-derived)** | Hardcoded into [prompt.txt](file:///C:/Users/SPM/Documents/NLU-guideline-transcoder/prompt.txt) matching live Phoenix AI 3 rules (`id >= 80,000`). |
| Non-RPC dispositions (`FOLLOW_UP`, `REMINDER`, `CLAIMING_PAID`, `3PC`, `UNRELATED`) | **No (Template-derived)** | Deliberately excluded from LLM transcoding for now; in newer live campaigns these are stamped out from reusable 7-component SPM platform templates independent of the client form. |

---

## 2. Why Generated CSVs Repeat Rows Halfway Down (Expected Behavior)

In Phoenix AI 3's flat `rules` table (`call_qa.qa_checklist_row`), **each disposition is stored as its own `Rule Name`** containing a complete set of rubric rows.
- Because [prompt.txt](file:///C:/Users/SPM/Documents/NLU-guideline-transcoder/prompt.txt) outputs both `<SLUG>_RIGHT_PARTY_CONTACT` and `<SLUG>_PTP` in a single CSV using the same skeleton (e.g., rows 2–49 and 50–97 in [v3b_homecredit_fintech.csv](file:///C:/Users/SPM/.t3/userdata/providers/antigravity/ac0a3dfd6dddb20962cecff6ee5fe65e19d3923be20e52c5ab52ff877f7e4c32/antigravity-acp/brain/343766d0-306d-4f9c-80a9-4f2305b76777/scratch/v3b_homecredit_fintech.csv)), the second half looks identical at a glance except for:
  1. **`Rule Name`** (`*_RIGHT_PARTY_CONTACT` vs. `*_PTP`), and
  2. **`Special Condition`** on PTP-only / no-PTP-only rows (toggled between `"not_applicable"` and active `""`).

---

## 3. Open Decisions & Known Blockers

1. **Encrypted Attachment (`CALLMON_2026_MADRID.xlsx`):**
   - `MBTC Cards 30DPD` could not be verified against its Excel attachment because the file is OLE2-encrypted (`StrongEncryptionDataSpace`). An unlocked copy or password is needed to confirm 1:1 fidelity with `MBTC_30DPD_RPC`/`PTP`.
2. **Defect-Deduction Scoring Convention (`EWBC` / `Orico`):**
   - Current [prompt.txt](file:///C:/Users/SPM/Documents/NLU-guideline-transcoder/prompt.txt) normalizes defect tiers (`Critical=25`, `Important=15`, `Essential=10`) to sum to `100` (e.g., `5.75` / `3.45` / `2.30`).
   - However, the literal newest live rule in Phoenix AI 3 (`EWB_PTP`, `id = 85844–85872`) puts raw `25` / `15` / `10` directly in `Component Score` and marks `Critical` items `auto_fail`. Confirm which scoring mode downstream QA scoring expects.
3. **`RIGHT_PARTY_CONTACT` vs. `RIGHT_PERSON_CONTACT`:**
   - Kept `RIGHT_PARTY_CONTACT` because the live DB has **64** `*_RIGHT_PARTY_CONTACT` rules / **209** text rows vs. **0** `RIGHT_PERSON_CONTACT`, and client PDFs print *"Right Party Contact"*. Easy 2-line change in [prompt.txt](file:///C:/Users/SPM/Documents/NLU-guideline-transcoder/prompt.txt) if a policy override to `Person` is ever desired.
4. **Multi-Factor PID OR-Groups (`Component Condition ID > 1`):**
   - Currently the LLM sometimes puts all 3 PID factors (`full_name`, `birthdate`, `address`) under `cid = 1` (requiring all 3 simultaneously). If strict 2-of-3 OR-matching is needed (e.g., `cid=1` Name+DOB, `cid=2` Name+Address), add an explicit OR-group rule to section 3 of [prompt.txt](file:///C:/Users/SPM/Documents/NLU-guideline-transcoder/prompt.txt).
5. **Automating Non-RPC Dispositions Later:**
   - Because `FOLLOW_UP`, `REMINDER`, `CLAIMING_PAID`, `3PC`, and `UNRELATED` are largely client-form-independent in the newest DB rules, they are best generated **deterministically in Python** from a single canonical platform template rather than by the LLM.
6. **Regex Pattern Population (`/intent`):**
   - Newly coined `*_intent` names produced by [prompt.txt](file:///C:/Users/SPM/Documents/NLU-guideline-transcoder/prompt.txt) that are not yet in Phoenix AI 3's `patterns/` catalog (1,611 intents) will need regex patterns added (or mapped to existing canonical intents) before live scoring.
