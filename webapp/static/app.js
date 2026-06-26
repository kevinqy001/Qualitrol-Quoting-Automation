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
  const dropZoneDocs = $("#drop-zone-docs");
  const dropZoneSld  = $("#drop-zone-sld");
  const fileInputDocs = $("#file-input-docs");
  const fileInputSld  = $("#file-input-sld");
  const startAnalysisBtn = $("#btn-start-analysis");
  const cancelAnalysisBtn = $("#btn-cancel-analysis");

  // Each entry: { file: File, zone: "doc" | "sld" }
  let selectedDocFiles = [];   // project documents
  let selectedSldFiles = [];   // circuit diagrams / SLDs
  let activeAnalysisController = null;

  function allSelectedFiles() {
    return [
      ...selectedDocFiles.map((f) => ({ file: f, zone: "doc" })),
      ...selectedSldFiles.map((f) => ({ file: f, zone: "sld" })),
    ];
  }

  function totalSelectedCount() {
    return selectedDocFiles.length + selectedSldFiles.length;
  }

  // ---- Zone: Docs ----
  dropZoneDocs.addEventListener("click", () => fileInputDocs.click());
  fileInputDocs.addEventListener("change", () => {
    if (fileInputDocs.files.length) addDocFiles(Array.from(fileInputDocs.files));
  });
  ["dragover", "dragenter"].forEach((evt) =>
    dropZoneDocs.addEventListener(evt, (e) => { e.preventDefault(); dropZoneDocs.classList.add("dz-hover"); })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropZoneDocs.addEventListener(evt, (e) => { e.preventDefault(); dropZoneDocs.classList.remove("dz-hover"); })
  );
  dropZoneDocs.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) addDocFiles(Array.from(e.dataTransfer.files));
  });

  // ---- Zone: SLD ----
  dropZoneSld.addEventListener("click", () => fileInputSld.click());
  fileInputSld.addEventListener("change", () => {
    if (fileInputSld.files.length) addSldFiles(Array.from(fileInputSld.files));
  });
  ["dragover", "dragenter"].forEach((evt) =>
    dropZoneSld.addEventListener(evt, (e) => { e.preventDefault(); dropZoneSld.classList.add("dz-hover"); })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropZoneSld.addEventListener(evt, (e) => { e.preventDefault(); dropZoneSld.classList.remove("dz-hover"); })
  );
  dropZoneSld.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) addSldFiles(Array.from(e.dataTransfer.files));
  });

  function addDocFiles(files) {
    const existing = new Set(selectedDocFiles.map((f) => f.name));
    files.forEach((f) => { if (!existing.has(f.name)) selectedDocFiles.push(f); });
    dropZoneDocs.classList.toggle("dz-has-files", selectedDocFiles.length > 0);
    afterFileChange();
    fileInputDocs.value = "";
  }

  function addSldFiles(files) {
    const existing = new Set(selectedSldFiles.map((f) => f.name));
    files.forEach((f) => { if (!existing.has(f.name)) selectedSldFiles.push(f); });
    dropZoneSld.classList.toggle("dz-has-files", selectedSldFiles.length > 0);
    afterFileChange();
    fileInputSld.value = "";
  }

  function afterFileChange() {
    renderSelectedFiles();
    const total = totalSelectedCount();
    if (total > 0) {
      const sldNote = selectedSldFiles.length
        ? ` · ${selectedSldFiles.length} SLD diagram(s)` : "";
      $("#selected-file-meta").textContent =
        `${total} file(s), ${formatBytes(totalSelectedBytes())} total${sldNote} · ready for analysis`;
    }
    $("#upload-result").classList.add("hidden");
  }

  startAnalysisBtn.addEventListener("click", () => {
    if (totalSelectedCount()) analyzeSelectedFiles();
  });

  cancelAnalysisBtn.addEventListener("click", () => {
    if (activeAnalysisController) activeAnalysisController.abort();
  });

  function renderSelectedFiles() {
    const list = $("#selected-files-list");
    const all = allSelectedFiles();

    if (!all.length) {
      $("#selected-file-name").textContent = "No file selected yet";
      $("#selected-file-meta").textContent = "Choose documents and/or a circuit diagram, then click Start LLM Analysis.";
      list.innerHTML = "";
      startAnalysisBtn.disabled = true;
      cancelAnalysisBtn.disabled = true;
      return;
    }

    const total = totalSelectedCount();
    $("#selected-file-name").textContent =
      total === 1 ? all[0].file.name : `${total} files selected`;

    list.innerHTML = all
      .map(({ file, zone }, idx) => {
        const badge = zone === "sld"
          ? `<span class="file-pill-sld-badge">SLD</span>`
          : `<span class="file-pill-doc-badge">DOC</span>`;
        return `
          <div class="file-pill">
            <div class="file-pill-main">
              <p class="file-pill-name">${escapeHtml(file.name)}${badge}</p>
              <p class="file-pill-meta">${formatBytes(file.size)} · ${escapeHtml(file.type || "unknown type")}</p>
            </div>
            <button class="file-pill-remove" type="button" data-remove-file="${idx}" data-zone="${zone}" aria-label="Remove ${escapeHtml(file.name)}">×</button>
          </div>`;
      })
      .join("");

    list.querySelectorAll("[data-remove-file]").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        const idx = Number(event.currentTarget.dataset.removeFile);
        const zone = event.currentTarget.dataset.zone;
        const docCount = selectedDocFiles.length;
        if (zone === "sld") {
          selectedSldFiles.splice(idx - docCount, 1);
          dropZoneSld.classList.toggle("dz-has-files", selectedSldFiles.length > 0);
        } else {
          selectedDocFiles.splice(idx, 1);
          dropZoneDocs.classList.toggle("dz-has-files", selectedDocFiles.length > 0);
        }
        afterFileChange();
      });
    });

    startAnalysisBtn.disabled = false;
  }

  function setAnalysisRunning(isRunning) {
    const total = totalSelectedCount();
    startAnalysisBtn.disabled = isRunning || total === 0;
    cancelAnalysisBtn.disabled = !isRunning;
    const fileLabel = total > 1 ? `${total} Files` : "File";
    startAnalysisBtn.textContent = isRunning
      ? `Analyzing ${fileLabel}...`
      : `Start LLM Analysis${total > 1 ? ` (${total})` : ""}`;
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
    selectedDocFiles.forEach((file) => formData.append("files", file));
    selectedSldFiles.forEach((file) => formData.append("files", file));
    // Tell the backend which filenames came from the SLD zone.
    formData.append("sld_filenames", JSON.stringify(selectedSldFiles.map((f) => f.name)));

    try {
      const res = await fetch(`${API}/ingest/batch`, {
        method: "POST",
        body: formData,
        signal: activeAnalysisController.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      const total = totalSelectedCount();
      $("#upload-filename").textContent = data.fileName;
      const extraction = data.extraction || data.boq;
      $("#upload-meta").textContent =
        `${formatBytes(data.fileSizeBytes)} · ${data.fileCount || total} file(s) · ingested ${new Date(data.ingestedAt).toLocaleTimeString()} · mode: ${extraction.extractionMode || "auto"}`;
      $("#stat-items").textContent = (extraction.requirements || extraction.lineItems || []).length;
      $("#stat-confidence").textContent = Math.round(data.confidence * 100) + "%";
      $("#stat-time").textContent = (data.processingTimeMs / 1000).toFixed(1) + "s";
      $("#stat-case").textContent = data.caseId.replace("CASE-", "");

      renderExtraction(extraction);
      $("#upload-result").classList.remove("hidden");

      // Persist this case so it can be revisited from the History panel.
      saveCaseToHistory({
        caseId: data.caseId,
        fileName: data.fileName,
        ingestedAt: data.ingestedAt || new Date().toISOString(),
        confidence: data.confidence,
        fileCount: data.fileCount || total,
        fileSizeBytes: data.fileSizeBytes,
        processingTimeMs: data.processingTimeMs,
        extraction,
      });
    } catch (err) {
      if (err.name === "AbortError") {
        $("#selected-file-meta").textContent =
          `${totalSelectedCount()} file(s), ${formatBytes(totalSelectedBytes())} total · analysis terminated by user`;
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
    return [...selectedDocFiles, ...selectedSldFiles].reduce((sum, f) => sum + f.size, 0);
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
  // The currently displayed case + a pristine snapshot of its auto-generated
  // line items (used by the Edit BOQ "Reset" action).
  let currentExtraction = null;
  let originalLineItems = null;

  function renderExtraction(boq) {
    $("#boq-ref").textContent = boq.boqId || boq.caseReference || "BOQ";
    $("#extraction-summary").textContent =
      boq.extractionSummary || "No extraction summary returned.";
    renderFeatures(boq.features || {});
    renderMissingInfoQuestions(boq.missingInfoQuestions || []);
    renderRequirements(boq.requirements || []);
    if (boq.source) {
      $("#source-badge").textContent = `${boq.source.fileName || "uploaded"} · ${boq.source.fileType || "file"}`;
      $("#source-doc").textContent = boq.source.preview || "No readable source preview returned.";
    }

    // Show/hide the "Download BOQ (Excel)" button based on availability.
    const dlBtn = $("#btn-download-boq");
    if (dlBtn) {
      if (!IS_STATIC && boq.boqExcelUrl) {
        dlBtn.href = boq.boqExcelUrl;
        dlBtn.classList.remove("hidden");
      } else {
        dlBtn.removeAttribute("href");
        dlBtn.classList.add("hidden");
      }
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

    // Track the active case + pristine snapshot for the Edit BOQ feature.
    currentExtraction = boq;
    originalLineItems = JSON.parse(JSON.stringify(boq.lineItems || []));
    renderBoqTable(boq.lineItems || []);
  }

  function renderBoqTable(lineItems) {
    const tbody = $("#boq-table-body");
    tbody.innerHTML = "";

    if (!lineItems || !lineItems.length) {
      tbody.innerHTML =
        `<tr><td colspan="5" style="text-align:center; color: var(--muted); padding: 32px;">No Qualitrol product lines detected</td></tr>`;
      return;
    }

    lineItems.forEach((item, idx) => {
      const params = item.technicalParams || {};
      const chips = Object.entries(params)
        .map(([k, v]) => {
          const val = Array.isArray(v) ? v.join(", ") : String(v);
          return `<span class="badge" style="margin:0 4px 4px 0;">${escapeHtml(k)}: ${escapeHtml(val)}</span>`;
        })
        .join("");

      tbody.insertAdjacentHTML(
        "beforeend",
        `<tr>
          <td>${escapeHtml(String(item.lineNumber ?? idx + 1))}</td>
          <td style="font-weight:700;color:var(--ralliant-brown);">${escapeHtml(String(item.productCode ?? ""))}</td>
          <td>${escapeHtml(String(item.description ?? ""))}</td>
          <td class="text-right" style="font-weight:700;">${escapeHtml(String(item.quantity ?? ""))} ${escapeHtml(item.unit || "")}</td>
          <td>${chips || '<span style="color:var(--muted);">—</span>'}</td>
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
    // panel.classList.remove("hidden");  // section hidden per UI decision
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

  // ── Edit BOQ (manual product code & qty override) ──────────────────────
  function buildEditRows(items) {
    const c = $("#edit-boq-list");
    if (!c) return;
    c.innerHTML =
      `<table>
        <thead><tr><th>#</th><th>Product Code</th><th>Description</th><th class="text-right">Qty</th></tr></thead>
        <tbody>` +
      (items || [])
        .map(
          (it, i) => `<tr>
            <td>${escapeHtml(String(it.lineNumber ?? i + 1))}</td>
            <td><input class="field-input edit-pc" data-i="${i}" value="${escapeHtml(String(it.productCode ?? ""))}" /></td>
            <td style="color:var(--muted);font-size:13px;">${escapeHtml(String(it.description ?? ""))}</td>
            <td class="text-right"><input class="field-input edit-qty" data-i="${i}" value="${escapeHtml(String(it.quantity ?? ""))}" style="width:90px;text-align:right;" /></td>
          </tr>`
        )
        .join("") +
      `</tbody></table>`;
  }

  function openEditBoq() {
    const items = (currentExtraction && currentExtraction.lineItems) || [];
    if (!items.length) {
      alert("There are no BOQ line items to edit yet. Run an analysis first.");
      return;
    }
    buildEditRows(items);
    $("#edit-boq-modal").classList.remove("hidden");
  }

  function closeEditBoq() {
    $("#edit-boq-modal").classList.add("hidden");
  }

  function resetEditBoq() {
    // Restore the editor inputs to the pristine auto-generated values.
    buildEditRows(originalLineItems || []);
  }

  function persistCurrentCaseEdits() {
    // If this case is in local history, update its stored line items so the
    // manual edits survive page reloads and History "View".
    const caseId = currentExtraction && currentExtraction.caseReference;
    if (!caseId) return;
    const list = loadHistory();
    const rec = list.find((c) => c.caseId === caseId);
    if (rec && rec.extraction) {
      rec.extraction.lineItems = currentExtraction.lineItems;
      if (currentExtraction.boqExcelUrl) {
        rec.extraction.boqExcelUrl = currentExtraction.boqExcelUrl;
      }
      writeHistory(list);
    }
  }

  function triggerDownload(url, filename) {
    if (!url) return;
    const a = document.createElement("a");
    a.href = url;
    if (filename) a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function regenerateAndDownloadBoq(items) {
    const caseId = currentExtraction && currentExtraction.caseReference;
    if (IS_STATIC || !caseId) return;
    const saveBtn = $("#btn-edit-save");
    try {
      const res = await fetch(
        `${API}/boq/excel/${encodeURIComponent(caseId)}/regenerate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            lineItems: items.map((it) => ({
              lineNumber: it.lineNumber,
              productCode: it.productCode,
              quantity: it.quantity,
            })),
          }),
        }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      // Reveal / refresh the download button for the edited Excel.
      currentExtraction.boqExcelUrl = data.boqExcelUrl;
      const dlBtn = $("#btn-download-boq");
      if (dlBtn && data.boqExcelUrl) {
        dlBtn.href = data.boqExcelUrl;
        dlBtn.classList.remove("hidden");
      }
      persistCurrentCaseEdits();
      // Auto-download the freshly regenerated, edited BOQ Excel.
      triggerDownload(data.boqExcelUrl, data.fileName);
    } catch (err) {
      alert("Edits saved, but BOQ Excel regeneration failed: " + err.message);
    } finally {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.textContent = "Save";
      }
    }
  }

  async function saveEditBoq() {
    if (!currentExtraction) return closeEditBoq();
    const items = currentExtraction.lineItems || [];
    $("#edit-boq-list").querySelectorAll(".edit-pc").forEach((inp) => {
      const i = Number(inp.dataset.i);
      if (items[i]) items[i].productCode = inp.value.trim();
    });
    $("#edit-boq-list").querySelectorAll(".edit-qty").forEach((inp) => {
      const i = Number(inp.dataset.i);
      if (!items[i]) return;
      const v = inp.value.trim();
      const num = Number(v);
      items[i].quantity = v !== "" && Number.isFinite(num) ? num : v;
    });
    renderBoqTable(items);       // refresh the Step 2 BOQ table
    persistCurrentCaseEdits();   // persist into local history when applicable

    const saveBtn = $("#btn-edit-save");
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Saving…"; }
    closeEditBoq();              // exit back to the Step 2 page
    // Regenerate the BOQ Excel from the edits and download it.
    await regenerateAndDownloadBoq(items);
  }

  $("#btn-edit-boq").addEventListener("click", openEditBoq);
  $("#btn-edit-close").addEventListener("click", closeEditBoq);
  $("#btn-edit-reset").addEventListener("click", resetEditBoq);
  $("#btn-edit-save").addEventListener("click", saveEditBoq);
  $("#edit-boq-modal").addEventListener("click", (e) => {
    if (e.target === $("#edit-boq-modal")) closeEditBoq();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#edit-boq-modal").classList.contains("hidden")) closeEditBoq();
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
          ? ""
          : "No AI endpoint/key is configured yet — uploads use deterministic local extraction. Configure an LLM key to enable full AI-powered analysis.";
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

  // ── Case History (localStorage; no backend DB yet) ─────────────────────
  // Each completed analysis is stored so the user can revisit past scanned
  // cases. We persist the step1+step2-derived `extraction` (requirements,
  // BOQ line items, detected scenarios, missing-info questions, source
  // preview) — enough to fully re-render the Requirement Review screen.
  const HISTORY_KEY = "qualitrol_case_history_v1";
  const HISTORY_MAX = 25;

  function loadHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch {
      return [];
    }
  }

  function writeHistory(arr) {
    // Trim to the cap, then write; if the quota is exceeded, drop the oldest
    // entries one at a time until it fits.
    let list = arr.slice(0, HISTORY_MAX);
    while (list.length) {
      try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
        return true;
      } catch {
        list = list.slice(0, list.length - 1); // drop oldest and retry
      }
    }
    try { localStorage.removeItem(HISTORY_KEY); } catch {}
    return false;
  }

  function saveCaseToHistory(record) {
    const list = loadHistory().filter((c) => c.caseId !== record.caseId);
    list.unshift(record);
    writeHistory(list);
    updateHistoryCount();
  }

  function deleteCase(caseId) {
    writeHistory(loadHistory().filter((c) => c.caseId !== caseId));
    updateHistoryCount();
    renderHistoryList();
  }

  function clearHistory() {
    try { localStorage.removeItem(HISTORY_KEY); } catch {}
    updateHistoryCount();
    renderHistoryList();
  }

  function updateHistoryCount() {
    const n = loadHistory().length;
    const badge = $("#history-count");
    if (badge) badge.textContent = String(n);
  }

  function renderHistoryList() {
    const list = loadHistory();
    const container = $("#history-list");
    const foot = $("#history-foot-note");
    if (!container) return;

    if (!list.length) {
      container.innerHTML =
        `<div class="history-empty">No analyzed cases yet. Upload documents and run an analysis to build your history.</div>`;
      if (foot) foot.textContent = "";
      return;
    }

    if (foot) {
      foot.textContent = `${list.length} case(s) stored locally (max ${HISTORY_MAX}).`;
    }

    container.innerHTML = list
      .map((c) => {
        const ex = c.extraction || {};
        const reqCount = (ex.requirements || []).length;
        const boqCount = (ex.lineItems || []).length;
        const missCount = ex.missingInfoCount ?? (ex.missingInfoQuestions || []).length;
        const conf = Number.isFinite(c.confidence)
          ? Math.round(c.confidence * 100) + "%"
          : "—";
        const when = c.ingestedAt ? new Date(c.ingestedAt).toLocaleString() : "";
        const mode = ex.extractionMode === "llm" ? "LLM" : "Rules";
        const dlBtn = (!IS_STATIC && ex.boqExcelUrl)
          ? `<a class="btn-secondary" download href="${escapeHtml(ex.boqExcelUrl)}" title="Download BOQ Excel" style="min-height:36px;padding:8px 12px;">⬇ Excel</a>`
          : "";
        return `<div class="history-row">
          <div class="history-row-main">
            <p class="history-row-title">${escapeHtml(c.caseId || "Case")}</p>
            <p class="history-row-meta">${escapeHtml(c.fileName || "uploaded")} · ${escapeHtml(when)} · ${c.fileCount || 1} file(s)</p>
            <div class="history-row-badges">
              <span class="badge accent">${boqCount} BOQ line(s)</span>
              <span class="badge">${reqCount} requirement(s)</span>
              ${missCount ? `<span class="badge priority-medium">${missCount} clarification(s)</span>` : ""}
              <span class="badge">${conf} confidence</span>
              <span class="badge">${mode}</span>
            </div>
          </div>
          <div class="history-row-actions">
            ${dlBtn}
            <button class="btn-primary" type="button" data-view-case="${escapeHtml(c.caseId)}" style="min-height:36px;padding:8px 14px;">View</button>
            <button class="btn-secondary" type="button" data-delete-case="${escapeHtml(c.caseId)}" style="min-height:36px;padding:8px 12px;">Delete</button>
          </div>
        </div>`;
      })
      .join("");

    container.querySelectorAll("[data-view-case]").forEach((btn) =>
      btn.addEventListener("click", () => viewHistoricalCase(btn.dataset.viewCase))
    );
    container.querySelectorAll("[data-delete-case]").forEach((btn) =>
      btn.addEventListener("click", () => deleteCase(btn.dataset.deleteCase))
    );
  }

  function viewHistoricalCase(caseId) {
    const record = loadHistory().find((c) => c.caseId === caseId);
    if (!record || !record.extraction) return;
    renderExtraction(record.extraction);
    const badge = $("#boq-status");
    if (badge) {
      badge.textContent = `History · ${caseId}`;
      badge.className = "badge accent";
    }
    closeHistory();
    switchTab("boq");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function openHistory() {
    renderHistoryList();
    $("#history-modal").classList.remove("hidden");
  }

  function closeHistory() {
    $("#history-modal").classList.add("hidden");
  }

  $("#btn-history").addEventListener("click", openHistory);
  $("#btn-history-close").addEventListener("click", closeHistory);
  $("#btn-history-clear").addEventListener("click", () => {
    if (confirm("Clear all locally stored cases? This cannot be undone.")) clearHistory();
  });
  $("#history-modal").addEventListener("click", (e) => {
    if (e.target === $("#history-modal")) closeHistory(); // click backdrop to close
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#history-modal").classList.contains("hidden")) closeHistory();
  });

  // ── Initial data load ──────────────────────────────────────────────────
  loadPoc1Status();
  loadSampleBoq();
  loadSyncStatus();
  updateHistoryCount();
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
