# Lark Document Button -> LLM Pipeline: Official Documentation Research & Prototyping Guide

## 1. Executive Summary

**Goal:** Embed a button in a Lark document that sends a table row payload to an LLM for inference, then grade the LLM output before integrating it into a downstream application (whose ingestion spec is not yet defined).

**Key Findings from Official Lark Documentation:**
1. **Native Lark Docx tables cannot contain interactive buttons.** In the `docx/v1` data model, a Table block (`block_type: 31`) only contains Table Cell blocks (`block_type: 32`) with rich-text/list children—there is no native button block or interactive cell type.
2. **Docx Iframe blocks (`block_type: 26`) cannot embed arbitrary web apps.** Official docs restrict `IframeComponentType` to a fixed allowlist (Airtable, Figma, CodePen, Canva, Bilibili, etc.).
3. **True embedded buttons inside a Lark Docx require a "Docs Add-on" (Cloud Document Widget).** This is the official way to render custom HTML/JS buttons inside a document, but it requires creating a Custom App (`appID` + `blockTypeID`) in the Lark Developer Console and getting tenant admin approval to publish.
4. **Lark Base (Bitable) natively supports Button fields + HTTP Request automations (no custom Lark App required)—with one critical caveat:**
   - In a **standalone Lark Base**, a **Button field** can trigger a Base Automation that sends an **HTTP POST** request with the row's fields to your LLM server, parses the JSON response (up to 60s timeout), and writes the LLM output/grade back into the same row.
   - **However, official Lark docs explicitly state that Button fields do NOT work when a Base is embedded inside a Lark Doc (`block_type: 18`).** If the table must live inside a Docx via an embedded Base, you can trigger the same HTTP automation using a **Checkbox** or **Single Select status** field instead of a Button.
5. **Working around a restricted company tenant:** If you cannot create/install apps in the Product Owner's tenant, you can either:
   - Prototype **without any Lark App** using Lark Base Automations (HTTP Request), or
   - Use a **Lark Test Company** (or free personal Lark workspace) with a copy of the PO's document, where the Lark Developer Console's **Test Companies and Users** feature lets you create and test Custom Apps immediately without admin approval, or
   - Build a lightweight companion prototype on top of this repo's existing [lark_doc_client.py](lark_doc_client.py) to parse native Docx table rows and run LLM inference + grading locally.

---

## 2. Official Lark Documentation Findings

### 2.1 Native Docx Tables (`docx/v1` API)
- **Block structure:** A native table is `block_type: 31` (`table`), containing:
  - `table.property.row_size` and `table.property.column_size`
  - `table.property.header_row` / `header_column` / `merge_info` (`row_span`, `col_span`)
  - `table.cells` (and `children`): a flat array of `table_cell` (`block_type: 32`) block IDs ordered **left-to-right, top-to-bottom**.
- **Cell contents:** Each `table_cell` contains child blocks (typically `text` `block_type: 2`, `ordered`/`bullet` lists, etc.).
- **Restrictions:** Table cells **cannot** contain nested `Table`, `Sheet`, `Bitable`, or `OKR` blocks, and there is **no native button block** in `BlockType`.
- **Reading via API:** `GET /open-apis/docx/v1/documents/:document_id/blocks` (already implemented in [lark_doc_client.py](lark_doc_client.py#L176-L188)) returns all blocks in pre-order traversal, allowing you to reconstruct any native table into structured rows in Python.
- **Required scopes:** `docx:document:readonly` (plus `wiki:node:read` for `/wiki/` URLs). Supports both `user_access_token` and `tenant_access_token`.

### 2.2 Docx Iframe Blocks (`block_type: 26`)
- Although `docx/v1` has an `Iframe` block (`block_type: 26`), its `component.type` is an enum (`IframeComponentType`) restricted to specific third-party providers (e.g., Bilibili, Youku, Airtable, Baidu/Amap Map, Figma, Canva, CodePen, Lark Questionnaire, Gold Data).
- **Conclusion:** You cannot embed a custom self-hosted button webpage into a Lark Docx using an Iframe block.

### 2.3 Lark Base (Bitable) Button Fields & HTTP Automations
- **Button Field:** In a Lark Base table, you can add a **Button** column (`+` -> `Button`). Clicking a row's button can:
  - Open a link / record details / form, or
  - Trigger a **Base Automation / Workflow** (`When a button is clicked` -> `Button field`).
- **HTTP Request Action:** Base Automations include a native **HTTP Request** action:
  - Sends an HTTP request (e.g., `POST` JSON) to an external endpoint (your LLM prototype server).
  - **Response parsing:** If your endpoint returns HTTP `200–299` with a JSON body, you can paste a sample JSON response into the automation editor and map response fields into subsequent steps (such as **Update record** to write the LLM output and status back to the clicked row).
  - **Limits documented by Lark:**
    - **60-second timeout** per HTTP request (important if calling slow reasoning models—either use a fast model/streaming-to-sync under 60s, or return `202 Accepted` immediately and callback via a Base Incoming Webhook).
    - Maximum **100 key-value pairs** in request configuration.
    - Query parameters must be configured in the **Params** section rather than appended directly to the URL string.
- **Critical Limitation for Docs Embedding:**
  - Official Lark Help Center (*Insert a base in Docs*) explicitly notes:
    > *"Button fields are not supported on a base inserted into a document. Specifically, you can't create a button field in a new base inserted into a document and if a base with a button field and corresponding automation is inserted into a document, the buttons won't work."*
  - **Workaround:** If the Base must be viewed inside a Lark Doc, trigger the automation on **"When a record matches conditions"** (e.g., user ticks a `Run LLM` checkbox or sets `Status = Queued`), which triggers the same HTTP Request action.
- **Reading Embedded Base via API:** If you later read an embedded Base programmatically, `docx/v1` returns a `bitable` block (`block_type: 18`) with `token` formatted as `BitableToken_TableID`. Split on `_` and call `bitable/v1` (`GET /open-apis/bitable/v1/apps/:app_token/tables/:table_id/records`).

### 2.4 Lark Docs Add-on (Cloud Document Widget / `docs-addon`)
- **What it is:** The official extension mechanism for embedding custom interactive UI (HTML/JS/React, buttons, forms, tables) directly inside a Lark Docx as a body block (`block_type: 40` `add_ons` / `block_type: 28` `isv`).
- **Capabilities:**
  - Full custom rendering inside the document (embedded block, floating, or full screen).
  - Client SDK (`@lark-opdev/cli` + Docs Add-on Client API) can read document metadata (`getActiveDocumentRef`, `Document.getRootBlock`, `Block.getBlock`, `getActiveWikiToken`) and user permissions (`Service.Permission.getDocumentPermission`).
  - Note: In the Docs Add-on Client API `BlockType` reference, `TABLE` creation is marked *Under development*, so reading complex table data is more reliably done by passing the `document_id` to your backend and querying `docx/v1/documents/:document_id/blocks`.
- **Requirements & Blocker for Restricted Tenants:**
  - Requires Node.js + `@lark-opdev/cli` (`opdev login`, `opdev create` -> `docs-addon`).
  - Requires a **Custom App** in the Lark Developer Console (`appID` + `blockTypeID`), security domain configuration, and **tenant administrator review** to publish to your company's production tenant.

### 2.5 Prototyping Around Restricted Tenants (`Test Companies and Users`)
- Lark Open Platform provides a **Test Companies and Users** feature in the Developer Console.
- When you create a Custom App and attach a Test Company, installing the app generates a **Test Version** where **permission and configuration changes take effect immediately without administrator approval**.
- Alternatively, you can create a **free personal Lark organization** (with your own email/phone) where you are the admin, copy the PO's sample table into a Doc/Base there, and demo the full end-to-end flow freely.

---

## 3. Comparison of Prototyping Approaches

| Approach | Button inside Lark? | Works with Native Docx Table? | Needs Custom Lark App? | Works in Restricted Tenant? | Best For |
|---|---|---|---|---|---|
| **Option A: Standalone Lark Base + Button Field + HTTP Automation** | Yes (native per-row button in Base) | Requires copying table to Base | **No** | **Yes** (if Base automations are enabled) | **Fastest presentable in-Lark button demo** with zero app approval |
| **Option B: Embedded Base in Doc + Checkbox/Status Trigger** | Indirect (checkbox/dropdown in Doc) | Embedded Base inside Docx | **No** | **Yes** | Keeping the UI inside a Lark Doc without app approval |
| **Option C: Native Docx Table + Companion Prototype UI (using `lark_doc_client.py`)** | External web UI (or hyperlink column in Docx table) | **Yes** (reads `block_type: 31` directly) | Needs OAuth app or static `user_access_token` | Only in personal/test tenant (or with existing token) | Prototyping the exact Docx table parser + LLM + grading workflow |
| **Option D: Lark Docs Add-on (`docs-addon`)** | **Yes** (custom button directly in Docx) | Yes (via Client SDK + `docx/v1` API) | **Yes** (`appID` + `blockTypeID`) | **No** (needs admin approval; use Test Company for demo) | **Target production UX** once pipeline & grading are proven |

---

## 4. Walkthrough: How to Build a Presentable Prototype Now

Because the PO's document is in a restricted tenant and the downstream app's ingestion contract is still undefined, **separate the prototype into two decoupled parts**:
1. **Trigger & Table Extraction** (Lark side — easy to swap later from Base/Companion UI to a Docs Add-on).
2. **LLM Processing & Grading Service** (Local Python HTTP service — the core value you are validating right now).

### Recommended Path for a Presentable Demo: Option A (Lark Base Button) + Local Python LLM/Grading Server

If you want stakeholders to **click a real button inside Lark** and watch a table row get processed by an LLM and graded—without waiting on IT for app permissions—follow these steps:

#### Step 1: Build the Local LLM & Grading HTTP Service (Python)
Create a small FastAPI (or Flask) server in this repo with a single endpoint, e.g. `POST /api/process-row`:
1. **Input payload (JSON):** Accepts a generic dictionary representing one table row (column names -> values) plus optional metadata (`source_id`, `row_id`).
2. **LLM Inference:** Formats the row into your prompt template, calls your LLM API, and requests a **structured JSON output** (e.g., `transformed_output`, `confidence`, `reasoning`) so it is ready to map to any downstream app schema later.
3. **Automated Pre-Grading (optional) + Logging:** Runs basic schema/quality checks (or an LLM-as-a-judge rubric) and logs `{input_row, llm_output, auto_grade, timestamp}` to a local JSONL/SQLite file for evaluation.
4. **Response (HTTP 200 JSON, within 60s):** Returns:
   ```json
   {
     "status": "ok",
     "llm_output": "...",
     "auto_grade": "PASS",
     "grade_notes": "All required fields extracted cleanly"
   }
   ```

#### Step 2: Expose Your Local Server for the Demo
Lark's cloud automation needs a public HTTPS URL to POST to:
- Run a quick tunnel such as `cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`.
- Add a simple shared secret header (e.g., `X-Proto-Token: <secret>`) so random traffic cannot hit your LLM endpoint.

#### Step 3: Set Up the Demo Table in Lark Base
1. Create a new **Lark Base** (in your workspace or a free personal Lark org) and paste/import sample rows from the PO's document.
2. Add output & grading columns to the table:
   - `LLM Output` (Text)
   - `Auto Grade` (Single Select: `PASS` / `NEEDS_REVIEW` / `FAIL`)
   - `Human Grade` (Single Select: `Approved` / `Rejected`)
   - `Grading Notes` (Text)
3. Add a **Button** field named `Run LLM`:
   - Click `+` at the right of the header row -> select **Button**.
   - Label it `Send to LLM`, choose a color.
   - Under **Actions to perform**, select **Run Automation -> New Automation**.

#### Step 4: Wire the Base Automation to Your LLM Endpoint
1. **Trigger:** `When a button is clicked` -> select your `Run LLM` button field and current table.
2. **Action 1 — HTTP Request:**
   - **Method:** `POST`
   - **URL:** `https://<your-tunnel-domain>/api/process-row`
   - **Headers:** `Content-Type: application/json`, `X-Proto-Token: <secret>`
   - **Body (raw JSON):** Use the `+` variable picker to insert the clicked row's fields.
   - **Output Parameters / Response body:** Paste your sample JSON response from Step 1 so Lark parses `llm_output`, `auto_grade`, and `grade_notes`.
3. **Action 2 — Update Record:**
   - Target the triggering record.
   - Map `LLM Output` <- HTTP response `llm_output`, `Auto Grade` <- `auto_grade`, `Grading Notes` <- `grade_notes`.
4. Click **Save and Activate**.

#### Step 5: Demo & Grade
- Click **Send to LLM** on any row in Lark Base.
- Within a few seconds, the row populates with the LLM output and automated grade.
- The PO can review and set **Human Grade** (`Approved` / `Rejected`) right in the table, giving you a concrete dataset of graded outputs before committing to the downstream app's ingestion format.

---

### Alternative Path (Option C): If You Must Read Directly from a Native Lark Docx Table

If the PO insists on keeping the data in a **native Lark Docx table** (and you have a personal/test Lark org or a valid `LARK_USER_ACCESS_TOKEN`):
1. Copy the PO's document into a personal/test Lark tenant where you own the app (`LARK_APP_ID` / `LARK_APP_SECRET` in `.env`, then `python lark_oauth.py login`).
2. Extend [lark_doc_client.py](lark_doc_client.py) with a helper that parses `client.get_blocks(document_id)`:
   - Find blocks with `block_type == 31` (`table`).
   - Slice `table["cells"]` into rows of length `table["property"]["column_size"]`.
   - For each `table_cell` (`block_type == 32`), concatenate text from its child blocks.
3. Serve a lightweight local web page (FastAPI + simple HTML table, or Streamlit) that displays the parsed Docx table with a **"Send to LLM"** button per row, runs the same `/api/process-row` pipeline, and lets the PO grade each output.
4. *(Optional)* Add a column in the Docx table containing clickable links (`https://<tunnel>/trigger?doc_id=...&row=2`) so clicking inside the Doc opens the result/grading view directly.

---

## 5. Path to Production (After Prototype Approval)

Once the LLM output quality passes grading and the downstream app's API is defined:
1. **Replace the grading sink** in `/api/process-row` with the downstream app's ingestion client (keeping an optional human-approval gate if needed).
2. **Choose the permanent Lark UX:**
   - Keep **Lark Base + Button Field** if the team is happy managing the table in Base (lowest maintenance, no custom Lark app needed), **or**
   - Build a **Lark Docs Add-on (`docs-addon`)** in a **Test Company** first, then submit it to your company's Lark admin for production tenant approval so the button lives directly inside the native Docx document.

---

## 6. Official Documentation References

- **Docx Block Data Structure (`Table`, `TableCell`, `Iframe`, `AddOns`, `Bitable`):**
  https://open.larksuite.com/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/document-docx/docx-v1/data-structure/block
- **Docx Overview & Block Capability Boundaries:**
  https://open.larksuite.com/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/document-docx/docx-overview
- **Docx Query All Blocks API (`GET /open-apis/docx/v1/documents/:document_id/blocks`):**
  https://open.larksuite.com/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/document-docx/docx-v1/document-block/list
- **Docs Add-on Introduction:**
  https://open.larksuite.com/document/uAjLw4CM/uYjL24iN/docs-add-on/docs-add-on-introduction
- **Docs Add-on Quick Development Guide:**
  https://open.larksuite.com/document/uAjLw4CM/uYjL24iN/docs-add-on/03-cloud-document-widget-quick-development-guide/03-cloud-document-widget-quick-developme
- **Docs Add-on Client API Reference (`BlockType` & Document APIs):**
  https://open.larksuite.com/document/uAjLw4CM/uYjL24iN/docs-add-on/05-api-doc/05-api-doc
- **Base Overview & Embedded Base Token Format (`BitableToken_TableID`):**
  https://open.larksuite.com/document/ukTMukTMukTM/uUDN04SN0QjL1QDN/bitable-overview
- **Use Button Fields in Base (Lark Help Center):**
  https://larksuite.com/hc/en-US/articles/047081488669-use-button-fields-in-base
- **Use Base to Automate Sending HTTP Requests (60s timeout & response parsing):**
  https://larksuite.com/hc/en-US/articles/125809335872-use-base-to-automate-the-sending-of-http-requests
- **Insert a Base in Docs (Button field limitation in embedded Bases):**
  https://larksuite.com/hc/en-US/articles/360048488001?from=product_base
- **Test Companies and Users (Testing Custom Apps Without Admin Approval):**
  https://open.larksuite.com/document/home/automatic-attendance-management-based-on-approval/step-1-create-a-test-application
