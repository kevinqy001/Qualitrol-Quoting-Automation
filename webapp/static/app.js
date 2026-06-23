/**
 * Qualitrol Quotation Agent — Vanilla JS frontend logic
 * Tabs + fetch() wiring against the FastAPI backend (same origin).
 * On GitHub Pages (data-static-host="github-pages") uses bundled JSON under data/.
 */
(function () {
  "use strict";

  const IS_STATIC = document.documentElement.dataset.staticHost === "github-pages";
  const API = IS_STATIC ? null : "/api/v1";
  const STATIC_DATA = {
    "/boq/sample": "data/boq-sample.json",
    "/spec/sample": "data/spec-sample.json",
    "/poc1/status": "data/poc1-status.json",
    "/sync/status": "data/sync-status.json",
  };
  const $ = (sel) => document.querySelector(sel);

  async function apiGet(path) {
    if (IS_STATIC) {
      const rel = STATIC_DATA[path];
      if (!rel) throw new Error(`Static demo does not support ${path}`);
      const res = await fetch(rel);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    }
    const res = await fetch(`${API}${path}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  function finalUnitPrice(cost, marginPct, discountPct) {
    const margin = marginPct / 100;
    const discount = discountPct / 100;
    if (margin >= 1.0) throw new Error("Gross margin must be less than 100%.");
    return Math.round((cost / (1 - margin)) * (1 - discount) * 100) / 100;
  }

  function priceBoqClient(payload, lineItems) {
    const unitPrice = finalUnitPrice(
      payload.cost,
      payload.grossMarginPercent,
      payload.discountPercent
    );
    const pricedItems = [];
    let subtotal = 0;
    for (const item of lineItems || []) {
      let qty = item.quantity ?? 1;
      qty = Number(qty);
      if (!Number.isFinite(qty)) qty = 1;
      const netUnit = Math.round(unitPrice * (1 - payload.discountPercent / 100) * 100) / 100;
      const lineTotal = Math.round(netUnit * qty * 100) / 100;
      subtotal += lineTotal;
      pricedItems.push({
        productCode: item.productCode || "",
        description: item.description || "",
        quantity: qty,
        unitPrice,
        discountPercent: payload.discountPercent,
        netUnitPrice: netUnit,
        lineTotal,
      });
    }
    return {
      formulaResult: {
        cost: payload.cost,
        grossMarginPercent: payload.grossMarginPercent,
        discountPercent: payload.discountPercent,
        finalUnitPrice: unitPrice,
        formula: "cost / (1 - margin) * (1 - discount)",
      },
      pricedBoq: {
        currency: payload.currency || "USD",
        lineItems: pricedItems,
        subtotal: Math.round(subtotal * 100) / 100,
        grandTotal: Math.round(subtotal * 100) / 100,
        validityDays: 90,
        paymentTerms: "Net 30",
      },
    };
  }

  const fmtMoney = (n, currency = "USD") =>
    new Intl.NumberFormat("en-US", { style: "currency", currency }).format(n);

  // ── Tab switching ──────────────────────────────────────────────────────
  const tabButtons = document.querySelectorAll(".tab-btn");

  function switchTab(name) {
    tabButtons.forEach((b) => {
      const isActive = b.dataset.tab === name;
      b.classList.toggle("active", isActive);
      b.setAttribute("aria-selected", String(isActive));
    });
    document.querySelectorAll(".tab-panel").forEach((p) => {
      const isActive = p.id === "tab-" + name;
      p.classList.toggle("active", isActive);
      if (isActive) {
        p.style.animation = "none";
        requestAnimationFrame(() => {
          p.style.animation = "";
        });
      }
    });
  }

  tabButtons.forEach((btn) =>
    btn.addEventListener("click", () => switchTab(btn.dataset.tab))
  );

  // ── Tab 1: Multi-Modal Ingestion ───────────────────────────────────────
  const dropZone = $("#drop-zone");
  const fileInput = $("#file-input");
  const startAnalysisBtn = $("#btn-start-analysis");
  const cancelAnalysisBtn = $("#btn-cancel-analysis");
  let selectedFiles = [];
  let activeAnalysisController = null;

  dropZone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) selectFiles(Array.from(fileInput.files));
  });

  ["dragover", "dragenter"].forEach((evt) =>
    dropZone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropZone.classList.add("border-brand-500", "bg-brand-50/50");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropZone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropZone.classList.remove("border-brand-500", "bg-brand-50/50");
    })
  );
  dropZone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) selectFiles(Array.from(e.dataTransfer.files));
  });

  startAnalysisBtn.addEventListener("click", () => {
    if (selectedFiles.length) analyzeSelectedFiles();
  });

  cancelAnalysisBtn.addEventListener("click", () => {
    if (activeAnalysisController) {
      activeAnalysisController.abort();
    }
  });

  function selectFiles(files) {
    selectedFiles = files;
    renderSelectedFiles();
    $("#selected-file-meta").textContent =
      `${selectedFiles.length} file(s), ${formatBytes(totalSelectedBytes())} total · ready for manual LLM analysis`;
    startAnalysisBtn.disabled = false;
    cancelAnalysisBtn.disabled = true;
    $("#upload-result").classList.add("hidden");
    fileInput.value = "";
  }

  function renderSelectedFiles() {
    const list = $("#selected-files-list");
    if (!selectedFiles.length) {
      $("#selected-file-name").textContent = "No file selected yet";
      $("#selected-file-meta").textContent = "Choose one or more documents first, then click Start LLM Analysis.";
      list.innerHTML = "";
      startAnalysisBtn.disabled = true;
      cancelAnalysisBtn.disabled = true;
      return;
    }

    $("#selected-file-name").textContent =
      selectedFiles.length === 1 ? selectedFiles[0].name : `${selectedFiles.length} files selected`;
    list.innerHTML = selectedFiles
      .map((file, idx) => `
        <div class="file-pill">
          <div class="file-pill-main">
            <p class="file-pill-name">${escapeHtml(file.name)}</p>
            <p class="file-pill-meta">${formatBytes(file.size)} · ${escapeHtml(file.type || "unknown type")}</p>
          </div>
          <button class="file-pill-remove" type="button" data-remove-file="${idx}" aria-label="Remove ${escapeHtml(file.name)}">×</button>
        </div>
      `)
      .join("");
    list.querySelectorAll("[data-remove-file]").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        const idx = Number(event.currentTarget.dataset.removeFile);
        selectedFiles.splice(idx, 1);
        renderSelectedFiles();
      });
    });
  }

  function setAnalysisRunning(isRunning) {
    startAnalysisBtn.disabled = isRunning || !selectedFiles.length;
    cancelAnalysisBtn.disabled = !isRunning;
    const fileLabel = selectedFiles.length > 1 ? `${selectedFiles.length} Files` : "File";
    startAnalysisBtn.textContent = isRunning ? `Analyzing ${fileLabel}...` : `Start LLM Analysis${selectedFiles.length > 1 ? ` (${selectedFiles.length})` : ""}`;
    cancelAnalysisBtn.textContent = isRunning ? "Terminate Current Analysis" : "No Active Analysis";
  }

  async function analyzeSelectedFiles() {
    if (IS_STATIC) {
      alert(
        "This GitHub Pages demo shows pre-built sample output only. " +
          "To upload documents and run Step 1 / Step 2 locally, use: python app.py"
      );
      return;
    }

    $("#upload-result").classList.add("hidden");
    $("#upload-progress").classList.remove("hidden");
    setAnalysisRunning(true);
    activeAnalysisController = new AbortController();

    const formData = new FormData();
    selectedFiles.forEach((file) => formData.append("files", file));

    try {
      const res = await fetch(`${API}/ingest/batch`, {
        method: "POST",
        body: formData,
        signal: activeAnalysisController.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      $("#upload-filename").textContent = data.fileName;
      const extraction = data.extraction || data.boq;
      $("#upload-meta").textContent =
        `${formatBytes(data.fileSizeBytes)} · ${data.fileCount || 1} file(s) · ingested ${new Date(data.ingestedAt).toLocaleTimeString()} · mode: ${extraction.extractionMode || "auto"}`;
      $("#stat-items").textContent = (extraction.requirements || extraction.lineItems || []).length;
      $("#stat-confidence").textContent = Math.round(data.confidence * 100) + "%";
      $("#stat-time").textContent = (data.processingTimeMs / 1000).toFixed(1) + "s";
      $("#stat-case").textContent = data.caseId.replace("CASE-", "");

      renderExtraction(extraction);
      $("#upload-result").classList.remove("hidden");
    } catch (err) {
      if (err.name === "AbortError") {
        $("#selected-file-meta").textContent =
          `${selectedFiles.length} file(s), ${formatBytes(totalSelectedBytes())} total · analysis terminated by user`;
      } else {
        alert("Analysis failed: " + err.message);
      }
    } finally {
      activeAnalysisController = null;
      $("#upload-progress").classList.add("hidden");
      setAnalysisRunning(false);
    }
  }

  function totalSelectedBytes() {
    return selectedFiles.reduce((sum, file) => sum + file.size, 0);
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / Math.pow(1024, index);
    return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  $("#goto-boq").addEventListener("click", () => switchTab("boq"));

  // ── Tab 2: BOQ Review ──────────────────────────────────────────────────
  function renderExtraction(boq) {
    $("#boq-ref").textContent = boq.boqId || boq.caseReference || "BOQ";
    $("#extraction-summary").textContent =
      boq.extractionSummary || "No extraction summary returned.";
    renderFeatures(boq.features || {});
    renderWarnings(boq.source?.warnings || []);
    renderMissingInfoQuestions(boq.missingInfoQuestions || []);
    renderRequirements(boq.requirements || []);
    if (boq.source) {
      $("#source-badge").textContent = `${boq.source.fileName || "uploaded"} · ${boq.source.fileType || "file"}`;
      $("#source-doc").textContent = boq.source.preview || "No readable source preview returned.";
    }

    // Empty-state notice: make it obvious when an upload extracted nothing
    // (rather than silently leaving the review screen looking blank).
    const isEmpty =
      !(boq.requirements?.length) && !(boq.lineItems?.length);
    const notice = $("#empty-extraction-notice");
    if (notice) {
      if (isEmpty) {
        const fileName = boq.source?.fileName || "the uploaded document";
        $("#empty-extraction-detail").textContent =
          `No Qualitrol requirements or BOQ lines were detected in ${fileName}. ` +
          `The file may be empty, image-only/scanned, or contain no recognizable ` +
          `Qualitrol monitoring requirements. Supported text types: PDF, DOCX, TXT, EML, MSG, MD.`;
        notice.classList.remove("hidden");
      } else {
        notice.classList.add("hidden");
      }
    }

    const tbody = $("#boq-table-body");
    tbody.innerHTML = "";

    if (!boq.lineItems?.length) {
      tbody.innerHTML =
        `<tr><td colspan="5" style="text-align:center; color: var(--muted); padding: 32px;">No Qualitrol product lines detected</td></tr>`;
      return;
    }

    boq.lineItems.forEach((item) => {
      const params = item.technicalParams || {};
      const chips = Object.entries(params)
        .map(([k, v]) => {
          const val = Array.isArray(v) ? v.join(", ") : String(v);
          return `<span class="badge" style="margin:0 4px 4px 0;">${escapeHtml(k)}: ${escapeHtml(val)}</span>`;
        })
        .join("");

      tbody.insertAdjacentHTML(
        "beforeend",
        `<tr class="hover:bg-slate-50">
          <td class="px-4 py-3 text-slate-400">${item.lineNumber}</td>
          <td class="px-4 py-3 font-mono text-xs font-semibold text-brand-700">${item.productCode}</td>
          <td class="px-4 py-3">${item.description}</td>
          <td class="px-4 py-3 text-right font-semibold">${item.quantity} ${item.unit || ""}</td>
          <td class="px-4 py-3">${chips || '<span class="text-slate-300">—</span>'}</td>
        </tr>`
      );
    });
  }

  const renderBoq = renderExtraction;

  function renderFeatures(features) {
    const labels = {
      dga_monitor: "DGA",
      temperature_monitor: "Temperature",
      bushing_monitor: "Bushing",
      fiber_optic: "Fiber Optic",
      iec61850: "IEC 61850",
      modbus_tcp: "Modbus TCP",
      dnp3: "DNP3",
    };
    const enabled = Object.entries(labels).filter(([key]) => features[key]);
    $("#feature-chips").innerHTML = enabled.length
      ? enabled.map(([, label]) => `<span class="badge success">${label}</span>`).join("")
      : `<span class="badge">No feature flags</span>`;
  }

  function renderWarnings(warnings) {
    const el = $("#warnings-list");
    if (!warnings.length) {
      el.classList.add("hidden");
      el.innerHTML = "";
      return;
    }
    el.innerHTML = warnings
      .map((warning) => `<div>${escapeHtml(warning)}</div>`)
      .join("");
    el.classList.remove("hidden");
  }

  function priorityClass(priority) {
    const p = String(priority || "").toLowerCase();
    if (p === "high") return "priority-high";
    if (p === "medium") return "priority-medium";
    return "priority-low";
  }

  function renderMissingInfoQuestions(questions) {
    const panel = $("#missing-info-panel");
    const list = $("#missing-info-list");
    const countBadge = $("#missing-info-count");

    if (!questions.length) {
      panel.classList.add("hidden");
      list.innerHTML = "";
      countBadge.textContent = "0 open";
      return;
    }

    countBadge.textContent = `${questions.length} open`;
    list.innerHTML = questions
      .map((q) => {
        const priority = q.priority || "Medium";
        const scenario = q.scenario_id ? escapeHtml(q.scenario_id) : "";
        const owner = q.owner ? escapeHtml(q.owner) : "";
        return `<article>
          <div style="display:flex; align-items:flex-start; justify-content:space-between; gap:16px;">
            <div style="min-width:0;">
              <p class="section-kicker" style="margin-bottom:4px;">
                ${scenario ? scenario + " · " : ""}Clarification needed
              </p>
              <h4 style="margin:0; font-weight:800; color: var(--ink);">${escapeHtml(q.missing_item || "Missing information")}</h4>
              ${q.why_it_matters ? `<p class="section-copy" style="margin-top:8px;">${escapeHtml(q.why_it_matters)}</p>` : ""}
            </div>
            <span class="badge ${priorityClass(priority)}">${escapeHtml(priority)}</span>
          </div>
          <blockquote>${escapeHtml(q.question || "")}</blockquote>
          ${owner ? `<p class="section-copy" style="margin:10px 0 0; font-size:12px;">Owner: ${owner}</p>` : ""}
        </article>`;
      })
      .join("");
    panel.classList.remove("hidden");
  }

  function renderRequirements(requirements) {
    const list = $("#requirements-list");
    if (!requirements.length) {
      list.innerHTML = `<div style="text-align:center; color: var(--muted);">No Qualitrol requirements detected</div>`;
      return;
    }
    list.innerHTML = requirements
      .map((req) => {
        // Pull the requirement "type" out as the headline badge; render any
        // remaining technical params as chips instead of raw JSON.
        const params = { ...(req.technicalParams || {}) };
        const reqType = params.type;
        delete params.type;

        const chips = Object.entries(params)
          .map(([k, v]) => {
            const val = Array.isArray(v) ? v.join(", ") : String(v);
            return `<span class="badge">${escapeHtml(k)}: ${escapeHtml(val)}</span>`;
          })
          .join("");

        const badgeText = req.quantity
          ? `${req.quantity} ${req.unit || ""}`.trim()
          : (reqType || "Requirement");
        const confidence = req.confidence
          ? Math.round(req.confidence * 100) + "%"
          : "—";
        const kicker = `${escapeHtml(req.category || "Requirement")}${
          req.productCode ? " · " + escapeHtml(req.productCode) : ""
        }`;

        return `<article>
          <div style="display:flex; align-items:flex-start; justify-content:space-between; gap:16px;">
            <div style="min-width:0;">
              <p class="section-kicker" style="margin-bottom:4px;">${kicker}</p>
              <h4 style="margin:0; font-weight:800; color: var(--ink);">${escapeHtml(req.requirement || "Untitled requirement")}</h4>
              ${chips ? `<div class="chip-row" style="justify-content:flex-start; margin-top:10px;">${chips}</div>` : ""}
            </div>
            <div style="flex:0 0 auto; text-align:right;">
              <span class="badge accent">${escapeHtml(String(badgeText))}</span>
              <p class="section-copy" style="margin:8px 0 0; font-size:12px;">${confidence} confidence</p>
            </div>
          </div>
          <blockquote>${escapeHtml(req.evidence || "No evidence snippet returned")}</blockquote>
        </article>`;
      })
      .join("");
  }

  async function loadSampleBoq() {
    try {
      const [boq, spec] = await Promise.all([
        apiGet("/boq/sample"),
        apiGet("/spec/sample"),
      ]);
      renderExtraction(boq);
      $("#source-doc").textContent = spec.content;
      $("#source-badge").textContent = spec.fileName;
    } catch (err) {
      $("#boq-table-body").innerHTML =
        `<tr><td colspan="5" class="px-4 py-8 text-center text-red-500">Failed to load BOQ: ${err.message}</td></tr>`;
    }
  }

  $("#btn-reload-boq").addEventListener("click", loadSampleBoq);
  $("#btn-approve-boq").addEventListener("click", () => {
    const badge = $("#boq-status");
    badge.textContent = "Reviewed";
    badge.className = "badge bg-emerald-100 text-emerald-700";
  });

  async function loadPoc1Status() {
    try {
      const data = await apiGet("/poc1/status");
      const llmConfigured = data.llm?.configured;
      $("#llm-mode-badge").textContent = IS_STATIC
        ? "GitHub Pages Demo"
        : llmConfigured
          ? "LLM Endpoint Connected"
          : "Local Rules Mode";
      $("#runtime-title").textContent = IS_STATIC
        ? "Static demo — sample data only"
        : llmConfigured
          ? "LLM extraction is configured"
          : "Local fallback extraction is active";
      $("#runtime-copy").textContent = IS_STATIC
        ? "This hosted demo loads sample project 00796547. Run python app.py locally to upload documents and execute the full pipeline."
        : llmConfigured
          ? `Requests will call ${data.llm.provider || "the configured Foundry provider"} using deployment ${data.llm.deploymentName || "not set"}.`
          : "No AI endpoint/key is configured yet, so uploads use deterministic local extraction for the POC harness.";
      $("#supported-types").innerHTML = Object.keys(data.supportedFileTypes || {})
        .map((ext) => `<span class="badge bg-brand-100 text-brand-700">${ext}</span>`)
        .join("");
    } catch (err) {
      $("#runtime-title").textContent = "Runtime status unavailable";
      $("#runtime-copy").textContent = err.message;
    }
  }

  // ── Tab 3: Dynamic Pricing ─────────────────────────────────────────────
  $("#btn-calculate").addEventListener("click", calculatePricing);

  async function calculatePricing() {
    const btn = $("#btn-calculate");
    const errEl = $("#pricing-error");
    errEl.classList.add("hidden");
    btn.disabled = true;
    btn.textContent = "Calculating…";

    try {
      const payload = {
        cost: parseFloat($("#input-cost").value) || 0,
        grossMarginPercent: parseFloat($("#input-margin").value) || 0,
        discountPercent: parseFloat($("#input-discount").value) || 0,
        currency: "USD",
      };

      let data;
      if (IS_STATIC) {
        const sample = await apiGet("/boq/sample");
        data = priceBoqClient(payload, sample.lineItems || []);
      } else {
        const res = await fetch(`${API}/pricing/calculate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        data = await res.json();
      }

      const f = data.formulaResult;
      $("#result-unit-price").textContent = fmtMoney(f.finalUnitPrice);
      $("#result-formula").textContent =
        `${fmtMoney(f.cost)} / (1 − ${f.grossMarginPercent}%) × (1 − ${f.discountPercent}%)`;

      const boq = data.pricedBoq;
      $("#result-grand-total").textContent = fmtMoney(boq.grandTotal, boq.currency);
      $("#result-terms").textContent =
        `${boq.paymentTerms} · valid ${boq.validityDays} days · ${boq.lineItems.length} line items`;

      const tbody = $("#pricing-table-body");
      tbody.innerHTML = "";
      boq.lineItems.forEach((item) => {
        tbody.insertAdjacentHTML(
          "beforeend",
          `<tr class="hover:bg-slate-50">
            <td class="px-4 py-3"><span class="font-mono text-xs font-semibold text-brand-700">${item.productCode}</span><br><span class="text-xs text-slate-500">${item.description}</span></td>
            <td class="px-4 py-3 text-right">${item.quantity}</td>
            <td class="px-4 py-3 text-right">${fmtMoney(item.unitPrice)}</td>
            <td class="px-4 py-3 text-right text-rose-600">−${item.discountPercent}%</td>
            <td class="px-4 py-3 text-right">${fmtMoney(item.netUnitPrice)}</td>
            <td class="px-4 py-3 text-right font-bold">${fmtMoney(item.lineTotal)}</td>
          </tr>`
        );
      });
    } catch (err) {
      errEl.textContent = "Calculation failed: " + err.message;
      errEl.classList.remove("hidden");
    } finally {
      btn.disabled = false;
      btn.textContent = "Calculate";
    }
  }

  // ── Tab 4: Sync Dashboard ──────────────────────────────────────────────
  async function loadSyncStatus() {
    try {
      const data = await apiGet("/sync/status");

      const sf = data.salesforce;
      const rows = [
        ["Case ID", sf.case.caseId],
        ["Subject", sf.case.subject],
        ["Account", sf.case.account],
        ["Priority", sf.case.priority],
        ["Customer Tier", sf.case.customerTier],
        ["Region", sf.case.region],
        ["Last Sync", new Date(sf.lastSyncAt).toLocaleString()],
      ];
      $("#sf-details").innerHTML = rows
        .map(
          ([k, v]) =>
            `<div class="flex justify-between gap-4 py-2"><dt class="text-slate-500">${k}</dt><dd class="font-medium text-right">${v}</dd></div>`
        )
        .join("");

      const dg = data.docgen;
      $("#docgen-badge").textContent = dg.templateReady
        ? "Template Loaded"
        : "Placeholder Mode";
      $("#docgen-rules").innerHTML = dg.conditionalRules
        .map(
          (r) =>
            `<li class="flex items-start gap-2">
              <svg class="h-4 w-4 mt-0.5 ${r.active ? "text-emerald-500" : "text-slate-300"}" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg>
              <span>${r.rule}</span>
            </li>`
        )
        .join("");
    } catch (err) {
      $("#sf-details").innerHTML =
        `<div class="py-2 text-red-500 text-sm">Failed to load status: ${err.message}</div>`;
    }
  }

  $("#btn-generate-doc").addEventListener("click", generateDoc);

  async function generateDoc() {
    const btn = $("#btn-generate-doc");
    btn.disabled = true;
    btn.textContent = "Assembling document…";

    try {
      let data;
      if (IS_STATIC) {
        data = {
          documentId: "DOC-DEMO",
          fileName: "Qualitrol_Quotation_DEMO.docx",
          documentUrl: "",
          fileSizeBytes: 0,
          clausesIncluded: ["BOQ Line Items", "Pricing Summary", "Open Clarification Questions"],
          clausesStripped: [],
          generatedAt: new Date().toISOString(),
          message:
            "Word export runs on the local FastAPI server. Clone the repo and run python app.py to generate .docx files.",
        };
      } else {
        const res = await fetch(`${API}/docgen/generate`, { method: "POST" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        data = await res.json();
      }

      const sizeKb = data.fileSizeBytes ? Math.round(data.fileSizeBytes / 1024) + " KB" : "";
      $("#docgen-filename").innerHTML =
        data.documentUrl
          ? `<a href="${data.documentUrl}" download style="color:var(--ralliant-brown);text-decoration:underline;">${data.fileName}</a>`
          : data.fileName;
      $("#docgen-meta").textContent =
        `${data.documentId}${sizeKb ? " · " + sizeKb : ""} · generated ${new Date(data.generatedAt).toLocaleTimeString()} · ${data.message || ""}`;
      $("#docgen-included").innerHTML = (data.clausesIncluded || [])
        .map((c) => `<li>• ${c}</li>`)
        .join("");
      $("#docgen-stripped").innerHTML = (data.clausesStripped || [])
        .map((c) => `<li>• ${c}</li>`)
        .join("");
      $("#docgen-result").classList.remove("hidden");
    } catch (err) {
      alert("Document generation failed: " + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Generate Quotation Document";
    }
  }

  // ── Initial data load ──────────────────────────────────────────────────
  loadPoc1Status();
  loadSampleBoq();
  loadSyncStatus();
})();

function toggleCollapsible(bodyId, chevronId, labelId) {
  const body = document.getElementById(bodyId);
  const chevron = document.getElementById(chevronId);
  const label = labelId ? document.getElementById(labelId) : null;
  if (!body) return;
  const isOpen = body.style.display !== "none";
  body.style.display = isOpen ? "none" : "";
  if (chevron) chevron.style.transform = isOpen ? "rotate(0deg)" : "rotate(-180deg)";
  if (label) label.textContent = isOpen ? "Expand" : "Collapse";
}
