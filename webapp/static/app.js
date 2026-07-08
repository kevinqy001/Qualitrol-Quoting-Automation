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
    renderEtaEstimate();
    setAnalysisRunning(true);
    activeAnalysisController = new AbortController();

    const formData = new FormData();
    selectedDocFiles.forEach((file) => formData.append("files", file));
    selectedSldFiles.forEach((file) => formData.append("files", file));
    // Tell the backend which filenames came from the SLD zone.
    formData.append("sld_filenames", JSON.stringify(selectedSldFiles.map((f) => f.name)));
    // Free-text project context typed by the user; fed to the LLM as extra
    // context when generating requirements and evidence.
    const contextNotes = ($("#context-notes")?.value || "").trim();
    if (contextNotes) formData.append("context_notes", contextNotes);

    try {
      const res = await fetch(`${API}/ingest/batch`, {
        method: "POST",
        body: formData,
        signal: activeAnalysisController.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      let data = await res.json();

      // Analysis runs as a background job (to avoid the cloud gateway request
      // timeout on long LLM/SLD analyses). Poll for the result until it's ready.
      if (data && data.status === "processing" && (data.jobId || data.caseId)) {
        const jobId = data.jobId || data.caseId;
        while (true) {
          await new Promise((r) => setTimeout(r, 3000));
          if (activeAnalysisController && activeAnalysisController.signal.aborted) {
            throw new DOMException("Aborted", "AbortError");
          }
          const pollRes = await fetch(`${API}/ingest/result/${encodeURIComponent(jobId)}`, {
            signal: activeAnalysisController.signal,
          });
          if (!pollRes.ok) {
            let msg = `HTTP ${pollRes.status}`;
            try { const e = await pollRes.json(); if (e && e.detail) msg = e.detail; } catch (_) {}
            throw new Error(msg);
          }
          const pollData = await pollRes.json();
          if (pollData && pollData.status === "processing") continue;
          data = pollData;
          break;
        }
      }

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

  // Draft BOQ "Evidence" column: jump to the matching evidence in the list
  // below so the user can see which requirement/evidence produced this line.
  $("#boq-table-body").addEventListener("click", (e) => {
    const btn = e.target.closest(".evidence-jump-btn");
    if (!btn) return;
    jumpToEvidence(btn.dataset.scenario || "", btn.dataset.product || "");
  });

  if ($("#btn-regenerate-boq")) {
    $("#btn-regenerate-boq").addEventListener("click", regenerateBoqFromFeedback);
  }

  // ── Tab 2: BOQ Review ──────────────────────────────────────────────────
  // The currently displayed case + a pristine snapshot of its auto-generated
  // line items (used by the Edit BOQ "Reset" action).
  let currentExtraction = null;
  let originalLineItems = null;

  // Split the server's one-paragraph summary into three labelled points:
  // Project Type · Monitoring Applications · Qualitrol Products. Falls back to
  // the raw paragraph if the expected markers aren't present.
  function renderSummaryPoints(summary) {
    const el = $("#extraction-summary");
    if (!el) return;
    const boldify = (s) =>
      escapeHtml(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    // For the Applications & Qualitrol Products points, keep only the product
    // count bold; application names and product descriptions render as plain
    // text (their emphasis markers are stripped).
    const boldifyCountOnly = (s) =>
      escapeHtml(s).replace(/\*\*(.+?)\*\*/g, (_m, inner) =>
        /product line/i.test(inner) ? `<strong>${inner}</strong>` : inner
      );
    const text = (summary || "").trim();
    const APP = "Monitoring applications identified:";
    const CAT = "Qualitrol product categories involved:";
    const iApp = text.indexOf(APP);
    const iCat = text.indexOf(CAT);

    if (iApp === -1 || iCat === -1 || iCat < iApp) {
      el.innerHTML = `<p class="section-copy" style="margin:0;">${boldify(
        text || "No extraction summary returned."
      )}</p>`;
      return;
    }

    let projectType = text.slice(0, iApp).trim().replace(/\.\s*$/, "");
    projectType = projectType.replace(/^This is an?\s+/i, "");
    const applications = text.slice(iApp + APP.length, iCat).trim().replace(/\.\s*$/, "");
    const products = text.slice(iCat + CAT.length).trim();

    const points = [
      { title: "Project Type", body: projectType, render: boldify },
      { title: "Monitoring Applications", body: applications, render: boldifyCountOnly },
      { title: "Qualitrol Products", body: products, render: boldifyCountOnly },
    ];

    el.innerHTML = points
      .filter((p) => p.body)
      .map(
        (p, i) => `<div style="margin:${i === 0 ? "0" : "10px"} 0 0;">
          <p style="margin:0 0 3px; font-size:11px; font-weight:850; letter-spacing:0.08em; text-transform:uppercase; color:var(--ralliant-brown);">${escapeHtml(p.title)}</p>
          <p class="section-copy" style="margin:0;">${p.render(p.body)}</p>
        </div>`
      )
      .join("");
  }

  function renderExtraction(boq) {
    $("#boq-ref").textContent = boq.boqId || boq.caseReference || "BOQ";
    renderSummaryPoints(boq.extractionSummary);
    renderFeatures(boq.features || {});
    renderMissingInfoQuestions(boq.missingInfoQuestions || []);
    renderEvidenceList(boq);
    if (boq.source) {
      const sourceBadge = $("#source-badge");
      const sourceDoc = $("#source-doc");
      if (sourceBadge) sourceBadge.textContent = `${boq.source.fileName || "uploaded"} · ${boq.source.fileType || "file"}`;
      if (sourceDoc) sourceDoc.textContent = boq.source.preview || "No readable source preview returned.";
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
    setupFeedback(boq);
    updateRegenerateButton();
  }

  // Show "Apply feedback & regenerate" only when at least one BOQ line has been
  // thumbed-down (and we have a backend to regenerate with).
  function updateRegenerateButton() {
    const btn = $("#btn-regenerate-boq");
    if (!btn) return;
    const items = (currentExtraction && currentExtraction.lineItems) || [];
    const hasNeg = !IS_STATIC && items.some((it) => it.feedback === "Negative");
    btn.classList.toggle("hidden", !hasNeg);
  }

  async function regenerateBoqFromFeedback() {
    if (IS_STATIC) return;
    const caseId =
      currentExtraction && (currentExtraction.caseReference || currentExtraction.boqId);
    if (!caseId) return;
    const btn = $("#btn-regenerate-boq");
    const orig = btn ? btn.innerHTML : "";
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = "<span>Regenerating…</span>";
    }
    try {
      const res = await fetch(
        `${API}/boq/${encodeURIComponent(caseId)}/regenerate`,
        { method: "POST" }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
      if (data.extraction) {
        renderExtraction(data.extraction);
        const list = loadHistory();
        const rec = list.find((c) => c.caseId === (data.caseId || caseId));
        if (rec) {
          rec.extraction = data.extraction;
          writeHistory(list);
        }
      }
      const n = (data.changes || []).length;
      alert(`BOQ regenerated from your feedback — ${n} line(s) updated. Revised lines are marked "Needs Review".`);
    } catch (err) {
      alert(`Couldn't regenerate the BOQ: ${err.message}`);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = orig;
      }
    }
  }

  function renderBoqTable(lineItems) {
    const tbody = $("#boq-table-body");
    tbody.innerHTML = "";

    if (!lineItems || !lineItems.length) {
      tbody.innerHTML =
        `<tr><td colspan="6" style="text-align:center; color: var(--muted); padding: 32px;">No Qualitrol product lines detected</td></tr>`;
      return;
    }

    // Map scenario_id -> human-readable scenario name (from detected scenarios).
    const scenarioNames = {};
    ((currentExtraction && currentExtraction.detectedScenarios) || []).forEach((d) => {
      if (d && d.scenario_id) {
        scenarioNames[String(d.scenario_id).trim().toLowerCase()] = d.scenario || "";
      }
    });

    lineItems.forEach((item, idx) => {
      const params = item.technicalParams || {};
      const HIDDEN_PARAMS = new Set(["review", "basis", "related"]);
      // "scenario" is shown as its readable name heading, not the raw code.
      let scenarioVal = "";
      let scenarioId = "";
      Object.entries(params).forEach(([k, v]) => {
        if (String(k).trim().toLowerCase() === "scenario") {
          const id = Array.isArray(v) ? v.join(", ") : String(v);
          scenarioId = id.trim();
          scenarioVal = scenarioNames[id.trim().toLowerCase()] || id;
        }
      });
      const scenarioHeading = scenarioVal
        ? `<div style="font-size:13px; font-weight:750; line-height:1.35; color:var(--ralliant-brown); margin:0 0 6px;">${escapeHtml(scenarioVal)}</div>`
        : "";
      const chips = Object.entries(params)
        .filter(([k]) => {
          const key = String(k).trim().toLowerCase();
          return !HIDDEN_PARAMS.has(key) && key !== "scenario";
        })
        .map(([k, v]) => {
          const raw = Array.isArray(v) ? v.join("; ") : String(v);
          // Multi-value params (e.g. "related") read poorly as one long chip
          // that wraps mid-text. Break them into one small chip per value,
          // under a compact label, so they wrap cleanly between chips.
          const tokens = raw.split(";").map((t) => t.trim()).filter(Boolean);
          if (tokens.length > 1) {
            const label = `<span style="font-size:11px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; color:var(--muted);">${escapeHtml(k)}</span>`;
            const items = tokens
              .map((t) => `<span class="badge" style="margin:0;">${escapeHtml(t)}</span>`)
              .join("");
            return `<span style="display:inline-flex; flex-wrap:wrap; align-items:center; gap:6px; margin:0 8px 6px 0; vertical-align:top;">${label}${items}</span>`;
          }
          return `<span class="badge" style="margin:0 4px 4px 0; white-space:normal; word-break:break-word;">${escapeHtml(k)}: ${escapeHtml(raw)}</span>`;
        })
        .join("");

      const evidenceCell = scenarioId
        ? `<button type="button" class="evidence-jump-btn" data-scenario="${escapeHtml(scenarioId)}" data-product="${escapeHtml(String(item.product_id ?? ""))}" title="Jump to the evidence this product line was derived from">
             <svg width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
             <span>Evidence</span>
           </button>`
        : '<span style="color:var(--muted);">—</span>';

      tbody.insertAdjacentHTML(
        "beforeend",
        `<tr>
          <td>${escapeHtml(String(item.lineNumber ?? idx + 1))}</td>
          <td style="font-weight:700;color:var(--ralliant-brown);">${escapeHtml(String(item.productCode ?? ""))}</td>
          <td>${escapeHtml(String(item.description ?? ""))}</td>
          <td class="text-right" style="font-weight:700;">${escapeHtml(String(item.quantity ?? ""))} ${escapeHtml(item.unit || "")}</td>
          <td>${scenarioHeading}${chips || (scenarioHeading ? "" : '<span style="color:var(--muted);">—</span>')}</td>
          <td class="text-right">${evidenceCell}</td>
        </tr>`
      );
    });
  }

  const renderBoq = renderExtraction;

  // ── BOQ feedback (thumbs up/down + comments), linked to the case/history ID ──
  function setFbState(overall, opts) {
    opts = opts || {};
    const up = $("#btn-fb-up");
    const down = $("#btn-fb-down");
    const status = $("#fb-status");
    const positive = overall === "Positive";
    const negative = overall === "Negative";
    // Selected thumb uses the brand palette; the other side is gently muted.
    if (up) {
      up.classList.toggle("selected-up", positive);
      up.classList.toggle("dim", negative);
    }
    if (down) {
      down.classList.toggle("selected-down", negative);
      down.classList.toggle("dim", positive);
    }
    if (status && !opts.skipStatus) {
      status.textContent =
        positive ? "Thanks — marked satisfied 👍"
        : negative ? "Thanks — feedback recorded 👎"
        : "";
      status.style.color = negative ? "var(--ralliant-orange)" : "var(--success)";
    }
  }

  async function setupFeedback(boq) {
    const grp = $("#boq-feedback");
    if (!grp) return;
    const caseId = boq && (boq.caseReference || boq.boqId);
    const hasLines = !!(boq && (boq.lineItems || []).length);
    if (IS_STATIC || !caseId || !hasLines) {
      grp.classList.add("hidden");
      return;
    }
    grp.classList.remove("hidden");
    setFbState(null);
    // Restore any previously submitted feedback for this case.
    try {
      const prev = await apiGet(`/feedback/${encodeURIComponent(caseId)}`);
      if (prev && prev.exists) setFbState(prev.overallFeedback);
    } catch (_) {}
  }

  async function submitFeedback(overall, comments) {
    const caseId =
      currentExtraction && (currentExtraction.caseReference || currentExtraction.boqId);
    if (!caseId) return;
    const items = (currentExtraction.lineItems || []).map((it, i) => ({
      lineNumber: it.lineNumber ?? i + 1,
      productCode: it.productCode || "",
      description: it.description || "",
      quantity: it.quantity,
      unit: it.unit || "",
    }));
    const status = $("#fb-status");
    // Optimistic UI: highlight the chosen thumb instantly and save in the
    // background, so the interaction feels immediate instead of waiting on the
    // network round-trip before anything visibly changes.
    setFbState(overall, { skipStatus: true });
    if (status) {
      status.innerHTML = `<span class="fb-spinner" aria-hidden="true"></span><span>Saving…</span>`;
      status.style.color = "var(--muted)";
    }
    try {
      const res = await fetch(`${API}/feedback/${encodeURIComponent(caseId)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          overallFeedback: overall,
          comments: comments || "",
          boqId: currentExtraction.boqId || "",
          items,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setFbState(overall); // swap the "Saving…" hint for the confirmed message
    } catch (err) {
      if (status) {
        status.textContent = "Couldn't save — please try again";
        status.style.color = "var(--ralliant-orange)";
      }
    }
  }

  function closeFeedbackModal() {
    const m = $("#feedback-modal");
    if (m) m.classList.add("hidden");
  }

  if ($("#btn-fb-up")) {
    $("#btn-fb-up").addEventListener("click", () => submitFeedback("Positive", ""));
  }
  if ($("#btn-fb-down")) {
    $("#btn-fb-down").addEventListener("click", () => {
      const ta = $("#fb-comment");
      if (ta) ta.value = "";
      $("#feedback-modal").classList.remove("hidden");
      setTimeout(() => ta && ta.focus(), 40);
    });
  }
  if ($("#btn-fb-close")) $("#btn-fb-close").addEventListener("click", closeFeedbackModal);
  if ($("#btn-fb-cancel")) $("#btn-fb-cancel").addEventListener("click", closeFeedbackModal);
  if ($("#btn-fb-submit")) {
    $("#btn-fb-submit").addEventListener("click", async () => {
      const comment = ($("#fb-comment").value || "").trim();
      await submitFeedback("Negative", comment);
      closeFeedbackModal();
    });
  }

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
    $("#feature-chips").innerHTML = enabled
      .map(([, label]) => `<span class="badge success">${label}</span>`)
      .join("");
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

  // Evidence & recommendation basis — one card per BOQ product line, explaining
  // why the product was recommended and from which source text it was derived.
  // Each card carries data-scenario so the Draft BOQ "Evidence" button can jump
  // straight to it.
  function renderEvidenceList(boq) {
    const list = $("#requirements-list");
    if (!list) return;

    const lineItems = boq.lineItems || [];
    const matching = boq.productMatching || [];
    const requirements = boq.requirements || [];
    const detected = boq.detectedScenarios || [];

    if (!lineItems.length) {
      list.innerHTML = `<div style="text-align:center; color: var(--muted);">No BOQ product lines to explain yet</div>`;
      return;
    }

    // scenario_id -> readable name / detected confidence.
    const scenarioNames = {};
    const scenarioConf = {};
    detected.forEach((d) => {
      if (d && d.scenario_id) {
        const k = String(d.scenario_id).trim().toLowerCase();
        scenarioNames[k] = d.scenario || "";
        scenarioConf[k] = d.confidence;
      }
    });

    // candidate_product_id -> product-matching entry (why it was recommended).
    const matchByProduct = {};
    const matchByFamily = {};
    matching.forEach((m) => {
      if (!m) return;
      if (m.candidate_product_id) matchByProduct[m.candidate_product_id] = m;
      if (m.family_id && !matchByFamily[m.family_id]) matchByFamily[m.family_id] = m;
    });

    // scenario_id -> unique source-text snippets pulled from the documents.
    const evBySid = {};
    requirements.forEach((r) => {
      const sid = String(r.productCode || "").trim().toLowerCase();
      if (!sid) return;
      const snip = (r.evidence || "").trim();
      if (!snip || /^no evidence/i.test(snip)) return;
      (evBySid[sid] = evBySid[sid] || []);
      if (!evBySid[sid].includes(snip)) evBySid[sid].push(snip);
    });

    // Solid pill colour scaled by confidence over the 60%–100% band:
    // ~60% = red, ~70% = orange, ~80% = yellow, ~100% = green (below 60% clamps
    // to red). Full 0–100% range isn't useful since scores rarely go that low.
    const confColor = (v) => {
      if (v == null) return "#9aa0a6"; // neutral grey when unknown
      const t = Math.max(0, Math.min(1, (v - 0.6) / 0.4)); // 0.6→0(red) … 1.0→1(green)
      const hue = Math.round(t * 120); // 0 = red, 60 = yellow, 120 = green
      return `hsl(${hue}, 70%, 42%)`;
    };

    const reasonRow = (label, value) =>
      `<div style="display:flex; gap:8px; margin:0 0 5px;">
         <span style="flex:0 0 118px; font-size:11px; font-weight:800; letter-spacing:0.03em; text-transform:uppercase; color:var(--muted); padding-top:1px;">${escapeHtml(label)}</span>
         <span style="flex:1; min-width:0; font-size:13px; color:var(--ink); line-height:1.45;">${value}</span>
       </div>`;

    list.innerHTML = lineItems
      .map((item) => {
        const sid = String((item.technicalParams && item.technicalParams.scenario) || "").trim();
        const sidLow = sid.toLowerCase();
        const scenarioName = scenarioNames[sidLow] || sid || "General scope";
        const productName = item.product_model || item.productCode || "TBD";
        const desc = item.description || "";
        const match = matchByProduct[item.product_id] || null;

        // Confidence: prefer the product line's own score, then the product
        // match score, then the detected-scenario confidence.
        let confVal = null;
        if (typeof item.confidence === "number" && item.confidence > 0) confVal = item.confidence;
        else if (match && typeof match.match_score === "number" && match.match_score > 0) confVal = match.match_score;
        else if (typeof scenarioConf[sidLow] === "number") confVal = scenarioConf[sidLow];
        const confPct = confVal != null ? Math.round(confVal * 100) + "%" : "—";

        // "Why recommended" rows.
        const rows = [];
        rows.push(reasonRow("Application", `<strong>${escapeHtml(scenarioName)}</strong> — matched from the project documents`));
        if (match && match.matched_parameters) {
          rows.push(reasonRow("Confirmed specs", escapeHtml(match.matched_parameters)));
        }
        const basis = item.quantityBasis || (item.technicalParams && item.technicalParams.basis) || "";
        if (basis) rows.push(reasonRow("Quantity basis", escapeHtml(basis)));
        const recommendation = (match && match.recommendation) || "";
        if (recommendation) rows.push(reasonRow("Recommendation", escapeHtml(recommendation)));
        const gap = (match && match.gap_or_risk) || item.assumption || "";
        if (gap) rows.push(reasonRow("To confirm", escapeHtml(gap)));

        // Source evidence: the actual sentence(s) from the spec, if any.
        const snippets = (evBySid[sidLow] || []).slice(0, 2);
        const evidenceHtml = snippets.length
          ? snippets.map((s) => `<blockquote>${escapeHtml(s)}</blockquote>`).join("")
          : `<blockquote style="color:var(--muted);">Derived from the drawing / scope (no single spec sentence). Confirm against the source documents.</blockquote>`;

        const fbKeyEsc = escapeHtml(String(lineFbKey(item, 0)));
        const feedbackBtn = IS_STATIC ? "" : `<button type="button" class="btn-primary req-fb-open" data-fbkey="${fbKeyEsc}" title="Give feedback on this line" style="padding:7px 14px;">
                <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.5a2.25 2.25 0 002.25 2.25h9.19l3.31 3.31V6.75A2.25 2.25 0 0016.94 4.5H4.5A2.25 2.25 0 002.25 6.75v6z"/></svg>
                <span>Feedback</span>
              </button>`;
        return `<article data-scenario="${escapeHtml(sid)}" data-product="${escapeHtml(String(item.product_id ?? ""))}">
          <div style="display:flex; align-items:flex-start; gap:16px;">
            <div style="flex:1; min-width:0;">
              <h4 style="margin:0; font-weight:800; color: var(--ink);">${escapeHtml(productName)}${desc ? ` <span style="font-weight:600; color:var(--muted);">— ${escapeHtml(desc)}</span>` : ""}</h4>
              <div style="margin-top:12px;">${rows.join("")}</div>
              ${evidenceHtml}
            </div>
            <div style="flex:0 0 auto; display:flex; flex-direction:column; align-items:flex-end; gap:8px;">
              <span class="conf-pill" style="background:${confColor(confVal)};" title="Confidence combines evidence strength in the documents, drawing/asset corroboration, and product-parameter match.">
                <span class="conf-dot"></span>${confPct} confidence
              </span>
              ${lineFbControl(item)}
              ${feedbackBtn}
            </div>
          </div>
        </article>`;
      })
      .join("");
  }

  // ── Per-item evidence feedback (👍/👎 + comments) ───────────────────────
  // Mirrors the Draft BOQ "Rate this draft" control, scoped to each evidence
  // card (one per BOQ line). State lives on the line item (feedback/comments)
  // and is persisted server-side via /requirements/{caseId}/feedback.
  function lineFbKey(item, i) {
    return (item && (item.feedbackKey || (item.lineNumber != null ? `L${item.lineNumber}` : null))) || `IDX-${i}`;
  }

  function lineFbControl(item) {
    if (IS_STATIC) return ""; // no backend to record feedback in the static demo
    const key = lineFbKey(item, 0);
    const fb = item.feedback || "";
    const upCls = fb === "Positive" ? " selected-up" : fb === "Negative" ? " dim" : "";
    const downCls = fb === "Negative" ? " selected-down" : fb === "Positive" ? " dim" : "";
    const statusTxt =
      fb === "Positive" ? "Marked helpful 👍" : fb === "Negative" ? "Feedback recorded 👎" : "";
    const statusColor = fb === "Negative" ? "var(--ralliant-orange)" : "var(--success)";
    const k = escapeHtml(String(key));
    return `<div class="fb-group req-fb" data-fbkey="${k}" style="display:inline-flex; margin:0;">
      <span class="fb-label">Rate this item</span>
      <span class="fb-btns">
        <button class="fb-btn req-fb-btn${upCls}" data-act="up" data-fbkey="${k}" type="button" title="This line looks right" aria-label="Thumbs up">👍</button>
        <button class="fb-btn req-fb-btn${downCls}" data-act="down" data-fbkey="${k}" type="button" title="Not right — tell us why" aria-label="Thumbs down">👎</button>
      </span>
      <span class="fb-status req-fb-status" data-fbkey="${k}" style="color:${statusColor};">${statusTxt}</span>
    </div>`;
  }

  function findLineByKey(key) {
    const items = (currentExtraction && currentExtraction.lineItems) || [];
    for (let i = 0; i < items.length; i++) {
      if (lineFbKey(items[i], i) === key) return { item: items[i], index: i };
    }
    return null;
  }

  // Escape a value for use inside a CSS attribute selector.
  function cssEsc(value) {
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function setReqFbState(key, feedback, opts) {
    opts = opts || {};
    const scope = $("#requirements-list");
    if (!scope) return;
    const up = scope.querySelector(`.req-fb-btn[data-act="up"][data-fbkey="${cssEsc(key)}"]`);
    const down = scope.querySelector(`.req-fb-btn[data-act="down"][data-fbkey="${cssEsc(key)}"]`);
    const status = scope.querySelector(`.req-fb-status[data-fbkey="${cssEsc(key)}"]`);
    const positive = feedback === "Positive";
    const negative = feedback === "Negative";
    if (up) {
      up.classList.toggle("selected-up", positive);
      up.classList.toggle("dim", negative);
    }
    if (down) {
      down.classList.toggle("selected-down", negative);
      down.classList.toggle("dim", positive);
    }
    if (status && !opts.skipStatus) {
      if (opts.saving) {
        status.innerHTML = `<span class="fb-spinner" aria-hidden="true"></span><span>Saving…</span>`;
        status.style.color = "var(--muted)";
      } else if (opts.error) {
        status.textContent = "Couldn't save — try again";
        status.style.color = "var(--ralliant-orange)";
      } else {
        status.textContent = positive
          ? "Marked helpful 👍"
          : negative
            ? "Feedback recorded 👎"
            : "";
        status.style.color = negative ? "var(--ralliant-orange)" : "var(--success)";
      }
    }
  }

  async function submitLineFeedback(key, feedback, comments) {
    const found = findLineByKey(key);
    if (!found) return;
    const caseId =
      currentExtraction && (currentExtraction.caseReference || currentExtraction.boqId);
    if (!caseId) return;
    // Optimistic UI.
    found.item.feedback = feedback;
    found.item.comments = comments || "";
    setReqFbState(key, feedback, { saving: true });
    try {
      const res = await fetch(
        `${API}/requirements/${encodeURIComponent(caseId)}/feedback`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            requirementId: key,
            requirement:
              (found.item.product_model || found.item.productCode || "") +
              (found.item.description ? ` — ${found.item.description}` : ""),
            scenarioId:
              (found.item.technicalParams && found.item.technicalParams.scenario) || "",
            feedback,
            comments: comments || "",
          }),
        }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setReqFbState(key, feedback);
      persistLineFeedback();
      updateRegenerateButton();
    } catch (err) {
      setReqFbState(key, feedback, { error: true });
    }
  }

  // Update the localStorage history snapshot so per-item feedback survives a
  // page reload / History revisit (server-side storage is the source of truth).
  function persistLineFeedback() {
    const caseId = currentExtraction && currentExtraction.caseReference;
    if (!caseId) return;
    const list = loadHistory();
    const rec = list.find((c) => c.caseId === caseId);
    if (rec && rec.extraction) {
      rec.extraction.lineItems = currentExtraction.lineItems;
      writeHistory(list);
    }
  }

  // ── Feedback comment modal (negative feedback) ──────────────────────────
  let pendingReqFbKey = null;

  function openReqFbModal(key) {
    const found = findLineByKey(key);
    pendingReqFbKey = key;
    const ta = $("#req-fb-comment");
    if (ta) ta.value = (found && found.item.comments) || "";
    const target = $("#req-fb-target");
    if (target) {
      target.textContent = found
        ? `Item: ${found.item.product_model || found.item.productCode || "product line"}`
        : "";
    }
    const modal = $("#req-feedback-modal");
    if (modal) modal.classList.remove("hidden");
    setTimeout(() => ta && ta.focus(), 40);
  }

  function closeReqFbModal() {
    const modal = $("#req-feedback-modal");
    if (modal) modal.classList.add("hidden");
    pendingReqFbKey = null;
  }

  if ($("#requirements-list")) {
    $("#requirements-list").addEventListener("click", (e) => {
      // "Feedback" button (brand-coloured): open the comment box directly.
      const openBtn = e.target.closest(".req-fb-open");
      if (openBtn) {
        openReqFbModal(openBtn.dataset.fbkey);
        return;
      }
      const btn = e.target.closest(".req-fb-btn");
      if (!btn) return;
      const key = btn.dataset.fbkey;
      if (btn.dataset.act === "up") {
        submitLineFeedback(key, "Positive", "");
      } else {
        openReqFbModal(key);
      }
    });
  }
  if ($("#btn-req-fb-close")) $("#btn-req-fb-close").addEventListener("click", closeReqFbModal);
  if ($("#btn-req-fb-cancel")) $("#btn-req-fb-cancel").addEventListener("click", closeReqFbModal);
  if ($("#btn-req-fb-submit")) {
    $("#btn-req-fb-submit").addEventListener("click", async () => {
      const key = pendingReqFbKey;
      if (!key) return closeReqFbModal();
      const comment = ($("#req-fb-comment").value || "").trim();
      closeReqFbModal();
      await submitLineFeedback(key, "Negative", comment);
    });
  }
  if ($("#req-feedback-modal")) {
    $("#req-feedback-modal").addEventListener("click", (e) => {
      if (e.target === $("#req-feedback-modal")) closeReqFbModal();
    });
  }
  document.addEventListener("keydown", (e) => {
    if (
      e.key === "Escape" &&
      $("#req-feedback-modal") &&
      !$("#req-feedback-modal").classList.contains("hidden")
    ) {
      closeReqFbModal();
    }
  });

  // Reveal the evidence list (expanding it if collapsed) and scroll to the card
  // for the clicked BOQ line, briefly highlighting it. Prefers the exact product
  // line; falls back to all cards sharing the same scenario.
  function jumpToEvidence(scenarioId, productId) {
    const list = $("#requirements-list");
    if (!list) return;
    if (list.style.display === "none") {
      toggleCollapsible("requirements-list", "requirements-chevron", "requirements-label");
    }
    const sid = (scenarioId || "").trim().toLowerCase();
    const pid = (productId || "").trim().toLowerCase();
    const articles = Array.from(list.querySelectorAll("article[data-scenario]"));
    let matches = pid
      ? articles.filter((a) => (a.dataset.product || "").trim().toLowerCase() === pid)
      : [];
    if (!matches.length && sid) {
      matches = articles.filter((a) => (a.dataset.scenario || "").trim().toLowerCase() === sid);
    }
    const target = matches[0];
    if (!target) {
      // No specific evidence match — just reveal and scroll to the list.
      list.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    // Defer to the next frame so the just-expanded list has laid out.
    requestAnimationFrame(() => {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      matches.forEach((a) => {
        a.classList.remove("evidence-flash");
        void a.offsetWidth; // restart the CSS animation
        a.classList.add("evidence-flash");
        setTimeout(() => a.classList.remove("evidence-flash"), 2400);
      });
    });
  }

  async function loadSampleBoq() {
    try {
      const [boq, spec] = await Promise.all([
        apiGet("/boq/sample"),
        apiGet("/spec/sample"),
      ]);
      // Guard against a late-resolving sample fetch clobbering a case the user
      // already opened (e.g. from History) while this request was in flight.
      if (currentExtraction) return;
      renderExtraction(boq);
      const sourceDoc = $("#source-doc");
      const sourceBadge = $("#source-badge");
      if (sourceDoc) sourceDoc.textContent = spec.content;
      if (sourceBadge) sourceBadge.textContent = spec.fileName;
    } catch (err) {
      $("#boq-table-body").innerHTML =
        `<tr><td colspan="6" class="px-4 py-8 text-center text-red-500">Failed to load BOQ: ${err.message}</td></tr>`;
    }
  }

  // ── Edit BOQ (manual product code & qty override) ──────────────────────
  function buildEditRows(items) {
    const c = $("#edit-boq-list");
    if (!c) return;
    c.innerHTML =
      `<table>
        <thead><tr><th>#</th><th>Product Code</th><th>Description</th><th class="text-right">Qty</th><th></th></tr></thead>
        <tbody>` +
      (items || [])
        .map(
          (it, i) => `<tr>
            <td>${escapeHtml(String(it.lineNumber ?? i + 1))}</td>
            <td><input class="field-input edit-pc" data-i="${i}" value="${escapeHtml(String(it.productCode ?? ""))}" /></td>
            <td style="color:var(--muted);font-size:13px;">${escapeHtml(String(it.description ?? ""))}</td>
            <td class="text-right"><input class="field-input edit-qty" data-i="${i}" value="${escapeHtml(String(it.quantity ?? ""))}" style="width:90px;text-align:right;" /></td>
            <td class="text-right"><button type="button" class="file-pill-remove edit-del" data-i="${i}" title="Delete this line" aria-label="Delete line ${escapeHtml(String(it.lineNumber ?? i + 1))}" style="width:28px;height:28px;">×</button></td>
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

  // Read the current editor inputs back into the given items array (by row
  // index), so typed-but-unsaved values survive a re-render (add line / etc.).
  function readEditRowsInto(items) {
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
  }

  function addEditLine() {
    if (!currentExtraction) return;
    if (!Array.isArray(currentExtraction.lineItems)) currentExtraction.lineItems = [];
    const items = currentExtraction.lineItems;
    readEditRowsInto(items); // preserve any edits already typed into the rows
    const nextNum =
      items.reduce((mx, it, i) => Math.max(mx, Number(it.lineNumber) || i + 1), 0) + 1;
    items.push({ lineNumber: nextNum, productCode: "", description: "", quantity: 1 });
    buildEditRows(items);
    // Focus the product-code field of the newly added row.
    const pcs = $("#edit-boq-list").querySelectorAll(".edit-pc");
    if (pcs.length) pcs[pcs.length - 1].focus();
  }

  function deleteEditLine(i) {
    if (!currentExtraction) return;
    if (!Array.isArray(currentExtraction.lineItems)) return;
    const items = currentExtraction.lineItems;
    readEditRowsInto(items); // preserve any edits already typed into the rows
    if (!Number.isInteger(i) || i < 0 || i >= items.length) return;
    items.splice(i, 1);
    // Renumber sequentially so the BOQ stays 1..N without gaps.
    items.forEach((it, idx) => { it.lineNumber = idx + 1; });
    buildEditRows(items);
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
    readEditRowsInto(items);
    renderBoqTable(items);       // refresh the Step 2 BOQ table
    persistCurrentCaseEdits();   // persist into local history when applicable

    const saveBtn = $("#btn-edit-save");
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Saving…"; }
    closeEditBoq();              // exit back to the Step 2 page
    // Regenerate the edited BOQ Excel and refresh the download button (no auto-download).
    await regenerateAndDownloadBoq(items);
  }

  $("#btn-edit-boq").addEventListener("click", openEditBoq);
  $("#btn-edit-close").addEventListener("click", closeEditBoq);
  $("#btn-edit-addline").addEventListener("click", addEditLine);
  $("#btn-edit-reset").addEventListener("click", resetEditBoq);
  $("#btn-edit-save").addEventListener("click", saveEditBoq);
  // Rows are re-rendered on every change, so delegate the per-row delete click.
  $("#edit-boq-list").addEventListener("click", (e) => {
    const btn = e.target.closest(".edit-del");
    if (!btn) return;
    deleteEditLine(Number(btn.dataset.i));
  });
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
      const llmBadge = $("#llm-mode-badge");
      if (llmBadge) {
        llmBadge.textContent = IS_STATIC
          ? "GitHub Pages Demo"
          : llmConfigured
            ? "LLM Endpoint Connected"
            : "Local Rules Mode";
      }
      const runtimeTitle = $("#runtime-title");
      if (runtimeTitle) {
        runtimeTitle.textContent = IS_STATIC
          ? "Static demo — sample data only"
          : llmConfigured
            ? "LLM extraction is configured"
            : "Local fallback extraction is active";
      }
      const runtimeCopy = $("#runtime-copy");
      if (runtimeCopy) {
        runtimeCopy.textContent = IS_STATIC
          ? "This hosted demo loads sample project 00796547. Run python app.py locally to upload documents and execute the full pipeline."
          : llmConfigured
            ? ""
            : "No AI endpoint/key is configured yet — uploads use deterministic local extraction. Configure an LLM key to enable full AI-powered analysis.";
      }
      const supportedTypes = $("#supported-types");
      if (supportedTypes) {
        supportedTypes.innerHTML = Object.keys(data.supportedFileTypes || {})
          .map((ext) => `<span class="badge bg-brand-100 text-brand-700">${ext}</span>`)
          .join("");
      }
    } catch (err) {
      const runtimeTitle = $("#runtime-title");
      const runtimeCopy = $("#runtime-copy");
      if (runtimeTitle) runtimeTitle.textContent = "Runtime status unavailable";
      if (runtimeCopy) runtimeCopy.textContent = err.message;
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
    renderEtaEstimate();
  }

  // ── Estimated analysis time ────────────────────────────────────────────
  // Fixed estimate (no dynamic per-file computation).
  function renderEtaEstimate() {
    const el = $("#eta-summary");
    if (!el) return;
    el.textContent = "Usually about 1.5–2 min. Scanned PDFs or SLD diagrams may take longer.";
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

  // ── Welcome chooser: start a new case, or continue a saved one ───────────
  function closeWelcome() {
    const m = $("#welcome-modal");
    if (m) m.classList.add("hidden");
  }

  function showWelcome() {
    const m = $("#welcome-modal");
    if (!m) return;
    const hasCases = loadHistory().length > 0;
    const contBtn = $("#welcome-continue");
    const contDesc = $("#welcome-continue-desc");
    if (contBtn) contBtn.disabled = !hasCases;
    if (contDesc) {
      contDesc.textContent = hasCases
        ? "Reopen a previously analyzed case from your history."
        : "No saved cases yet — start a new one first.";
    }
    m.classList.remove("hidden");
  }

  function startNewCase() {
    closeWelcome();
    switchTab("ingestion");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function continueSavedCase() {
    closeWelcome();
    openHistory();
  }

  if ($("#welcome-new")) $("#welcome-new").addEventListener("click", startNewCase);
  if ($("#welcome-continue")) {
    $("#welcome-continue").addEventListener("click", () => {
      if ($("#welcome-continue").disabled) return;
      continueSavedCase();
    });
  }
  if ($("#btn-welcome-close")) $("#btn-welcome-close").addEventListener("click", closeWelcome);
  if ($("#welcome-modal")) {
    $("#welcome-modal").addEventListener("click", (e) => {
      if (e.target === $("#welcome-modal")) closeWelcome(); // backdrop → default to blank step 1
    });
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("#welcome-modal") && !$("#welcome-modal").classList.contains("hidden")) closeWelcome();
  });

  // ── Tab 3: Configure & Quote ────────────────────────────────────────────
  // selectedDiscount: a discount % chosen by clicking a Discount Sensitivity
  // tile. When set, the Overall Summary shows that scenario; when null, it
  // shows the totals from the user's own per-line inputs.
  const marginState = { id: null, lines: [], selectedDiscount: null };
  const mNum = (v) => {
    const x = parseFloat(v);
    return Number.isFinite(x) ? x : 0;
  };
  const mPct = (v) => `${(Number(v) || 0).toFixed(1)}%`;

  let marginCatalog = { families: [], byFamily: {}, familyIdByName: {} };
  let marginCatalogLoaded = false;

  function marginSetCatalogStatus(text, ok) {
    const el = $("#margin-catalog-status");
    if (!el) return;
    if (!text) {
      el.style.display = "none";
      return;
    }
    el.style.display = "";
    el.textContent = text;
    el.style.background = ok ? "#dcfce7" : "#fef3c7";
    el.style.color = ok ? "#166534" : "#92400e";
  }

  // Health colour bands by TOTAL gross margin % (after the assigned discount):
  //   <75 red · 75-80 light red · 80-85 yellow · 85-90 light green · >90 green
  function marginHealthStyle(gm) {
    if (gm < 75) return { bg: "#b42318", fg: "#ffffff" };  // red — danger
    if (gm < 80) return { bg: "#f97066", fg: "#3a0a06" };  // light red
    if (gm < 85) return { bg: "#eab308", fg: "#3a2e05" };  // yellow
    if (gm < 90) return { bg: "#86efac", fg: "#0b3d22" };  // light green
    return { bg: "#067647", fg: "#ffffff" };               // green — healthy
  }

  function marginHealthLabel(gm) {
    if (gm < 75) return "Critical";
    if (gm < 80) return "Poor";
    if (gm < 85) return "OK";
    if (gm < 90) return "Good";
    return "Excellent";
  }

  async function marginLoadCatalog() {
    if (IS_STATIC) {
      marginSetCatalogStatus("Catalog unavailable in demo mode — type values manually", false);
      return;
    }
    marginSetCatalogStatus("Loading product catalog…", false);
    try {
      const data = await apiGet("/margin/catalog");
      const rawFamilies = data.families || [];

      // The price lists and the data package use different naming systems. Each
      // monitoring family carries a `priceListFamilyId` cross-link; fold that
      // priced family's models into the monitoring family so picking a model
      // also pulls its price, and hide the standalone priced family so the
      // dropdown shows one consistent (knowledge-base) family per product line.
      const pricedById = {};
      rawFamilies.forEach((f) => { if (f.priced) pricedById[f.id] = f; });
      const absorbed = new Set();
      const families = rawFamilies.map((f) => {
        const link = f.priceListFamilyId && pricedById[f.priceListFamilyId];
        if (!link) return f;
        absorbed.add(f.priceListFamilyId);
        return { ...f, models: (f.models || []).concat(link.models || []) };
      });

      const byFamily = {};
      const familyIdByName = {};
      let dlHtml = "";
      let total = 0;
      families.forEach((f) => {
        byFamily[f.id] = { name: f.name, models: {} };
        familyIdByName[(f.name || "").trim().toLowerCase()] = f.id;
        const opts = [];
        (f.models || []).forEach((m) => {
          let value = m.partNo ? `${m.model} · ${m.partNo}` : m.model;
          let uniq = value;
          let n = 2;
          while (byFamily[f.id].models[uniq]) uniq = `${value} (${n++})`;
          byFamily[f.id].models[uniq] = m;
          opts.push(`<option value="${escapeHtml(uniq)}">${escapeHtml(m.section || "")}</option>`);
          total++;
        });
        dlHtml += `<datalist id="mdl-${escapeHtml(f.id)}">${opts.join("")}</datalist>`;
      });

      // The family combobox only lists the canonical (non-absorbed) families.
      const dropdownFamilies = families.filter((f) => !absorbed.has(f.id));
      dlHtml +=
        `<datalist id="margin-families">` +
        dropdownFamilies
          .map((f) => `<option value="${escapeHtml(f.name)}"></option>`)
          .join("") +
        `</datalist>`;
      $("#margin-datalists").innerHTML = dlHtml;
      marginCatalog = {
        families: dropdownFamilies.map((f) => ({ id: f.id, name: f.name })),
        byFamily,
        familyIdByName,
      };
      marginCatalogLoaded = true;
      marginSetCatalogStatus(`Catalog ready · ${total} models`, true);
      setTimeout(() => marginSetCatalogStatus("", true), 4000);
      renderMarginTable();
    } catch (_) {
      marginSetCatalogStatus("Catalog failed to load — you can still type values manually", false);
    }
  }

  // Resolve a typed/selected family name to its catalog id ("" when custom).
  function marginFamilyIdFromName(name) {
    return marginCatalog.familyIdByName[(name || "").trim().toLowerCase()] || "";
  }

  function marginApplyCatalogModel(idx, rec) {
    const cur = $("#margin-currency").value;
    const line = marginState.lines[idx];
    const lp = rec.listPrice || {};
    const cc = rec.cost || {};
    line.description = rec.model;
    line.productCode = rec.partNo || "";
    line.catalogRef = { familyId: line.familyId, model: rec.model };
    line.unitListPrice = lp[cur] != null ? lp[cur] : lp.USD != null ? lp.USD : "";
    line.unitCost = cc[cur] != null ? cc[cur] : cc.USD != null ? cc.USD : "";
  }

  function marginReprice() {
    const cur = $("#margin-currency").value;
    let any = false;
    marginState.lines.forEach((line) => {
      const ref = line.catalogRef;
      const fam = ref && marginCatalog.byFamily[ref.familyId];
      if (!fam) return;
      const rec = Object.values(fam.models).find((m) => m.model === ref.model);
      if (!rec) return;
      const lp = rec.listPrice || {};
      const cc = rec.cost || {};
      line.unitListPrice = lp[cur] != null ? lp[cur] : lp.USD != null ? lp.USD : "";
      line.unitCost = cc[cur] != null ? cc[cur] : cc.USD != null ? cc.USD : "";
      any = true;
    });
    if (any) renderMarginTable();
    else recomputeMargin();
  }

  function renderSensitivity(totals) {
    const el = $("#margin-sensitivity");
    if (!totals || totals.totalList <= 0) {
      el.innerHTML =
        `<p class="section-copy" style="grid-column:1/-1;">Configure lines above to see the discount sensitivity.</p>`;
      return;
    }
    const currency = $("#margin-currency").value;
    const steps = [0, 5, 10, 15, 20];
    el.innerHTML = steps
      .map((d) => {
        const quoted = totals.totalList * (1 - d / 100);
        // Colour reflects total-margin health behind the scenes; only the
        // total quoted price is shown (no cost/margin numbers exposed).
        const gm = quoted > 0 ? (1 - totals.cogs / quoted) * 100 : 0;
        const st = marginHealthStyle(gm);
        const selected = marginState.selectedDiscount === d;
        const ring = selected
          ? "outline:3px solid #1f3a5f; outline-offset:2px; box-shadow:0 2px 8px rgba(0,0,0,0.18);"
          : "";
        return `<div data-d="${d}" title="Click to apply this discount to the Overall Summary" style="cursor:pointer; border-radius:10px; padding:14px 8px; text-align:center; background:${st.bg}; color:${st.fg}; ${ring}">
          <div style="font-size:12px; font-weight:600; opacity:.92;">${d}% discount${selected ? " ✓" : ""}</div>
          <div style="font-size:18px; font-weight:800; margin-top:5px; white-space:nowrap;">${fmtMoney(quoted, currency)}</div>
        </div>`;
      })
      .join("");
  }

  function marginReadGlobals() {
    // These cost/discount fields were removed from the UI; read safely so the
    // calculation logic still works (defaults to 0 when a field is absent).
    const val = (id) => {
      const el = $("#" + id);
      return el ? el.value : 0;
    };
    return {
      discountPct: val("margin-discount"),
      freight: val("margin-freight"),
      labour: val("margin-labour"),
      overheads: val("margin-overheads"),
      fieldService: val("margin-fieldservice"),
    };
  }

  function computeMarginsClient() {
    const g = marginReadGlobals();
    const dd = mNum(g.discountPct);
    const lines = marginState.lines.map((ln) => {
      const qty = mNum(ln.qty);
      const ul = mNum(ln.unitListPrice);
      const uc = mNum(ln.unitCost);
      const hasLineDisc = ln.discountPct !== "" && ln.discountPct != null;
      const disc = hasLineDisc ? mNum(ln.discountPct) : dd;
      const extList = qty * ul;
      const netUnit = ul * (1 - disc / 100);
      const extNet = qty * netUnit;
      const extCost = qty * uc;
      const margin = extNet > 0 ? (1 - extCost / extNet) * 100 : 0;
      return { extList, netUnit, extNet, extCost, margin, family: ln.family || "Unassigned" };
    });
    const totalList = lines.reduce((s, l) => s + l.extList, 0);
    const totalNet = lines.reduce((s, l) => s + l.extNet, 0);
    const totalMat = lines.reduce((s, l) => s + l.extCost, 0);
    const cogs =
      totalMat + mNum(g.freight) + mNum(g.labour) + mNum(g.overheads) + mNum(g.fieldService);
    const fams = {};
    const order = [];
    lines.forEach((l) => {
      if (!fams[l.family]) {
        fams[l.family] = { family: l.family, lines: 0, extList: 0, extNet: 0, extCost: 0 };
        order.push(l.family);
      }
      const f = fams[l.family];
      f.lines += 1;
      f.extList += l.extList;
      f.extNet += l.extNet;
      f.extCost += l.extCost;
    });
    const families = order.map((k) => {
      const f = fams[k];
      return {
        ...f,
        margin: f.extNet > 0 ? (1 - f.extCost / f.extNet) * 100 : 0,
      };
    });
    return {
      lines,
      families,
      totals: {
        totalList,
        totalNet,
        cogs,
        overallDiscount: totalList > 0 ? ((totalList - totalNet) / totalList) * 100 : 0,
        listGM: totalList > 0 ? (1 - cogs / totalList) * 100 : 0,
        quotedGM: totalNet > 0 ? (1 - cogs / totalNet) * 100 : 0,
      },
    };
  }

  function recomputeMargin() {
    const currency = $("#margin-currency").value;
    const r = computeMarginsClient();
    const tbody = $("#margin-table-body");
    r.lines.forEach((l, i) => {
      const tr = tbody.querySelector(`tr[data-row="${i}"]`);
      if (!tr) return;
      tr.querySelector('[data-c="extList"]').textContent = fmtMoney(l.extList, currency);
      tr.querySelector('[data-c="net"]').textContent = fmtMoney(l.extNet, currency);
    });
    const t = r.totals;
    $("#sum-list").textContent = fmtMoney(t.totalList, currency);

    const sel = marginState.selectedDiscount;
    const note = $("#margin-scenario-note");
    let dispQuoted, dispDisc;
    if (sel != null) {
      dispQuoted = t.totalList * (1 - sel / 100);
      dispDisc = sel;
      if (note) {
        note.textContent = `Showing the ${sel}% discount scenario from Discount Sensitivity. Click the highlighted tile again (or edit a line) to use your own line discounts.`;
      }
    } else {
      dispQuoted = t.totalNet;
      dispDisc = t.overallDiscount;
      if (note) note.textContent = "";
    }
    $("#sum-quoted").textContent = fmtMoney(dispQuoted, currency);
    $("#sum-discount").textContent = mPct(dispDisc);

    // Colour band for the CURRENT result's margin health — works at any
    // discount, including beyond the 0-20% sensitivity tiles.
    const band = $("#margin-health-band");
    if (band) {
      if (t.totalList > 0) {
        const gm = dispQuoted > 0 ? (1 - t.cogs / dispQuoted) * 100 : 0;
        const st = marginHealthStyle(gm);
        band.style.display = "";
        band.style.background = st.bg;
        band.style.color = st.fg;
        band.textContent = `At ${mPct(dispDisc)} discount: ${marginHealthLabel(gm)}`;
      } else {
        band.style.display = "none";
      }
    }
    renderSensitivity(t);

    // The per-family breakdown table was removed from the UI; compute still
    // runs (families are available for export) but there is nothing to render.
    const fb = $("#margin-family-body");
    if (fb) {
      fb.innerHTML = r.families
        .map(
          (f) => `<tr>
            <td>${escapeHtml(f.family)}</td>
            <td class="text-right">${f.lines}</td>
            <td class="text-right">${fmtMoney(f.extList, currency)}</td>
            <td class="text-right">${fmtMoney(f.extNet, currency)}</td>
          </tr>`
        )
        .join("");
    }
  }

  function renderMarginTable() {
    const tbody = $("#margin-table-body");
    if (!marginState.lines.length) {
      tbody.innerHTML =
        `<tr><td colspan="8" style="text-align:center; color: var(--muted); padding: 28px;">No lines yet — auto-fill from the BOQ, add a line, or load a record.</td></tr>`;
      recomputeMargin();
      return;
    }
    tbody.innerHTML = marginState.lines
      .map((ln, i) => {
        const numInp = (field, val, ph = "", w = 70) =>
          `<input type="number" step="any" class="field-input text-right" style="width:${w}px; min-width:${w}px; padding:6px 8px;" data-i="${i}" data-f="${field}" value="${val == null || val === "" ? "" : val}" placeholder="${ph}" />`;
        // Searchable combo styled like the custom dropdowns: brown chevron +
        // a round clear (×) button that appears once the field has a value.
        const chevronSvg =
          `<svg class="cs-combo-chevron" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`;
        const comboInput = (field, val, listAttr, minW, ph) => {
          const v = val == null ? "" : String(val);
          const clearBtn = v !== ""
            ? `<button type="button" class="cs-combo-clear" data-clear="${i}" data-cf="${field}" tabindex="-1" aria-label="Clear">&times;</button>`
            : "";
          return `<div class="cs-combo" style="min-width:${minW}px;">` +
            `<input class="field-input cs-combo-input" data-i="${i}" data-f="${field}"${listAttr} value="${escapeHtml(v)}" placeholder="${ph}" />` +
            clearBtn + chevronSvg +
            `</div>`;
        };
        const famVal =
          ln.family ||
          (ln.familyId && marginCatalog.byFamily[ln.familyId]
            ? marginCatalog.byFamily[ln.familyId].name
            : "");
        const famInput = comboInput("familyName", famVal, ` list="margin-families"`, 210, "select or type a family");
        const listAttr = ln.familyId ? ` list="mdl-${escapeHtml(ln.familyId)}"` : "";
        const modelInp =
          comboInput("model", ln.description, listAttr, 330, ln.familyId ? "type to search a model…" : "type a model or description") +
          (ln.productCode ? `<div style="font-size:11px; color:var(--muted); margin-top:3px;">${escapeHtml(ln.productCode)}</div>` : "");
        return `<tr data-row="${i}">
          <td style="min-width:220px;">${famInput}</td>
          <td style="min-width:340px;">${modelInp}</td>
          <td class="text-right">${numInp("qty", ln.qty, "", 56)}</td>
          <td class="text-right">${numInp("unitListPrice", ln.unitListPrice)}</td>
          <td class="text-right">${numInp("discountPct", ln.discountPct, "0")}</td>
          <td class="text-right" data-c="extList">-</td>
          <td class="text-right" data-c="net">-</td>
          <td class="text-right"><button class="btn-secondary" style="padding:4px 10px;" data-remove="${i}" type="button">×</button></td>
        </tr>`;
      })
      .join("");
    recomputeMargin();
  }

  // Delegated edits on the line table. Numeric/text fields recompute live
  // (keeping input focus); family/model selection is committed on `change`.
  $("#margin-table-body").addEventListener("input", (e) => {
    const el = e.target;
    if (!el.dataset || el.dataset.i == null || !el.dataset.f) return;
    const idx = Number(el.dataset.i);
    const line = marginState.lines[idx];
    if (!line) return;
    const f = el.dataset.f;
    if (f === "familyName") {
      line.family = el.value; // free text; id resolved on change
      return;
    }
    if (f === "model") {
      line.description = el.value; // live text; catalog match resolved on change
      return;
    }
    line[f] = el.value;
    marginState.selectedDiscount = null; // own input takes over the summary
    recomputeMargin();
  });
  $("#margin-table-body").addEventListener("change", (e) => {
    const el = e.target;
    if (!el.dataset || el.dataset.i == null || !el.dataset.f) return;
    const idx = Number(el.dataset.i);
    const line = marginState.lines[idx];
    if (!line) return;
    const f = el.dataset.f;
    marginState.selectedDiscount = null; // configuring a line uses own inputs
    if (f === "familyName") {
      line.family = el.value;
      line.familyId = marginFamilyIdFromName(el.value); // "" when custom
      line.catalogRef = null;
      renderMarginTable();
      return;
    }
    if (f === "model") {
      const fam = line.familyId;
      const rec =
        fam && marginCatalog.byFamily[fam]
          ? marginCatalog.byFamily[fam].models[el.value]
          : null;
      if (rec) marginApplyCatalogModel(idx, rec);
      else {
        line.description = el.value;
        line.catalogRef = null;
      }
      renderMarginTable();
    }
  });
  $("#margin-table-body").addEventListener("click", (e) => {
    // Clear (×) button inside a family/model combo: reset that field only.
    const clearBtn = e.target.closest("[data-clear]");
    if (clearBtn) {
      const idx = Number(clearBtn.dataset.clear);
      const line = marginState.lines[idx];
      if (!line) return;
      marginState.selectedDiscount = null;
      if (clearBtn.dataset.cf === "familyName") {
        line.family = "";
        line.familyId = "";
        line.catalogRef = null;
      } else if (clearBtn.dataset.cf === "model") {
        line.description = "";
        line.productCode = "";
        line.catalogRef = null;
      }
      renderMarginTable();
      return;
    }
    const btn = e.target.closest("[data-remove]");
    if (!btn) return;
    marginState.lines.splice(Number(btn.dataset.remove), 1);
    marginState.selectedDiscount = null;
    renderMarginTable();
  });

  // Currency change re-prices catalog-linked lines (other global cost inputs
  // were removed from the UI).
  $("#margin-currency").addEventListener("change", marginReprice);

  // Clicking a Discount Sensitivity tile applies that discount to the Overall
  // Summary; clicking the active tile again clears it (back to own inputs).
  $("#margin-sensitivity").addEventListener("click", (e) => {
    const cell = e.target.closest("[data-d]");
    if (!cell) return;
    const d = Number(cell.dataset.d);
    marginState.selectedDiscount = marginState.selectedDiscount === d ? null : d;
    recomputeMargin();
  });

  function marginScenarioName(boq, sid) {
    const det = (boq.detectedScenarios || []).find((d) => d.scenario_id === sid);
    return det ? det.scenario : sid || "";
  }

  // Map a BOQ line to its catalog family id. The data-package product_id encodes
  // the family (e.g. PROD_PF_GIS_PD_01 -> PF_GIS_PD); fall back to matching the
  // line's product family description against the catalog family names.
  function marginFamilyIdFromBoqItem(item) {
    const pid = (item.product_id || "").trim();
    const m = pid.match(/^PROD_(PF_[A-Z0-9_]+?)_\d+$/);
    if (m && marginCatalog.byFamily[m[1]]) return m[1];
    return marginFamilyIdFromName(item.description || "");
  }

  function marginAutofillFromBoq(boq) {
    boq = boq || currentExtraction;
    if (!boq || !(boq.lineItems || []).length) return false;
    const cur = $("#margin-currency").value;
    $("#margin-project").value = boq.caseReference || boq.boqId || "";
    marginState.id = null;
    marginState.selectedDiscount = null;
    marginState.lines = boq.lineItems.map((item) => {
      // Use the resolved model name only; never surface a raw PROD_* id.
      const model = item.product_model || "";
      const familyId = marginFamilyIdFromBoqItem(item);
      const fam = familyId ? marginCatalog.byFamily[familyId] : null;
      const line = {
        description: model,
        family: fam ? fam.name : (item.description || ""),
        familyId: fam ? familyId : "",
        productCode: model,
        qty: item.quantity || 0,
        unitListPrice: "",
        unitCost: "",
        discountPct: "",
        catalogRef: null,
      };
      // Auto-price when the BOQ model exactly matches a catalog model that
      // carries pricing (covers price-list models folded into the family).
      if (fam && model) {
        const want = model.trim().toLowerCase();
        const rec = Object.values(fam.models).find(
          (mm) => (mm.model || "").trim().toLowerCase() === want
        );
        if (rec) {
          const lp = rec.listPrice || {};
          const cc = rec.cost || {};
          line.catalogRef = { familyId, model: rec.model };
          line.description = rec.model;
          line.productCode = rec.partNo || line.productCode;
          line.unitListPrice = lp[cur] != null ? lp[cur] : lp.USD != null ? lp.USD : "";
          line.unitCost = cc[cur] != null ? cc[cur] : cc.USD != null ? cc.USD : "";
        }
      }
      return line;
    });
    $("#margin-source").textContent = `From BOQ ${boq.caseReference || boq.boqId || ""}`;
    renderMarginTable();
    return true;
  }

  function marginPayload() {
    return {
      id: marginState.id || undefined,
      name: $("#margin-project").value || "Margin",
      caseReference: $("#margin-project").value || "",
      currency: $("#margin-currency").value,
      globals: marginReadGlobals(),
      lines: marginState.lines.map((l) => ({
        description: l.description || "",
        family: l.family || "",
        familyId: l.familyId || "",
        catalogRef: l.catalogRef || null,
        productCode: l.productCode || "",
        qty: l.qty,
        unitListPrice: l.unitListPrice,
        unitCost: l.unitCost,
        discountPct: l.discountPct,
      })),
    };
  }

  $("#btn-margin-autofill").addEventListener("click", () => {
    if (!marginAutofillFromBoq()) {
      alert("No BOQ available to auto-fill. Upload documents or load the sample in Step 2 first.");
    }
  });

  $("#btn-margin-addline").addEventListener("click", () => {
    marginState.lines.push({
      description: "", family: "", productCode: "", qty: 1,
      unitListPrice: "", unitCost: "", discountPct: "",
    });
    marginState.selectedDiscount = null;
    renderMarginTable();
  });

  // The "Load saved record" picker is wired to the local case History (the same
  // cases shown in the History panel) plus any saved margin quotes. Selecting a
  // case loads its BOQ and auto-fills the calculator; selecting a saved quote
  // restores that calculator record.
  async function marginRefreshRecords() {
    const sel = $("#margin-load");
    if (!sel) return;
    const groups = [];

    const cases = loadHistory();
    if (cases.length) {
      const caseOpts = cases.map((c) => {
        const ex = c.extraction || {};
        const n = (ex.lineItems || []).length;
        const when = c.ingestedAt ? new Date(c.ingestedAt).toLocaleDateString() : "";
        return `<option value="case:${escapeHtml(c.caseId)}">${escapeHtml(c.caseId)} — ${n} line(s)${when ? " · " + escapeHtml(when) : ""}</option>`;
      });
      groups.push(`<optgroup label="Project cases (from History)">${caseOpts.join("")}</optgroup>`);
    }

    if (!IS_STATIC) {
      try {
        const data = await apiGet("/margin/records");
        const recs = data.records || [];
        if (recs.length) {
          const recOpts = recs.map((r) => {
            const gm = r.summary && r.summary.quotedMarginPct != null ? ` · GM ${r.summary.quotedMarginPct}%` : "";
            return `<option value="rec:${escapeHtml(r.id)}">${escapeHtml(r.name)} (${escapeHtml(r.savedAt || "")})${gm}</option>`;
          });
          groups.push(`<optgroup label="Saved quotes">${recOpts.join("")}</optgroup>`);
        }
      } catch (_) {}
    }

    sel.innerHTML =
      `<option value="">— load a previous case or quote —</option>` + groups.join("");
  }

  async function marginLoadSavedRecord(id) {
    const rec = await apiGet(`/margin/records/${id}`);
    marginState.id = rec.id || id;
    marginState.selectedDiscount = null;
    $("#margin-project").value = rec.caseReference || rec.name || "";
    $("#margin-currency").value = rec.currency || "USD";
    syncCustomSelect("margin-currency");
    const g = rec.globals || {};
    const setVal = (idAttr, v) => {
      const el = $("#" + idAttr);
      if (el) el.value = v;
    };
    setVal("margin-discount", g.discountPct != null ? g.discountPct : 0);
    setVal("margin-freight", g.freight != null ? g.freight : 0);
    setVal("margin-labour", g.labour != null ? g.labour : 0);
    setVal("margin-overheads", g.overheads != null ? g.overheads : 0);
    setVal("margin-fieldservice", g.fieldService != null ? g.fieldService : 0);
    marginState.lines = (rec.lines || []).map((l) => ({
      description: l.description || "", family: l.family || "",
      familyId: l.familyId || "", catalogRef: l.catalogRef || null,
      productCode: l.productCode || "", qty: l.qty,
      unitListPrice: l.unitListPrice, unitCost: l.unitCost, discountPct: l.discountPct,
    }));
    $("#margin-source").textContent = `Loaded quote ${rec.id || id}`;
    renderMarginTable();
  }

  $("#margin-load").addEventListener("change", async (e) => {
    const v = e.target.value;
    if (!v) return;
    try {
      if (v.startsWith("case:")) {
        const caseId = v.slice(5);
        const record = loadHistory().find((c) => c.caseId === caseId);
        if (!record || !record.extraction) {
          alert("That case is no longer in local history.");
          return;
        }
        renderExtraction(record.extraction); // sets currentExtraction + syncs Step 2
        marginAutofillFromBoq(record.extraction);
        $("#margin-source").textContent = `From case ${caseId}`;
      } else if (v.startsWith("rec:")) {
        await marginLoadSavedRecord(v.slice(4));
      }
    } catch (err) {
      alert("Failed to load selection: " + err.message);
    }
  });

  $("#btn-margin-save").addEventListener("click", async () => {
    if (IS_STATIC) {
      alert("Saving is only available when running the local backend (python app.py).");
      return;
    }
    if (!marginState.lines.length) {
      alert("Nothing to save — add at least one line.");
      return;
    }
    const status = $("#margin-status");
    try {
      const res = await fetch(`${API}/margin/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(marginPayload()),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      marginState.id = data.id;
      status.textContent = `Saved as ${data.id} at ${new Date(data.savedAt).toLocaleTimeString()}.`;
      await marginRefreshRecords();
    } catch (err) {
      status.textContent = "Save failed: " + err.message;
    }
  });

  $("#btn-margin-export").addEventListener("click", async () => {
    if (IS_STATIC) {
      alert("Excel export is only available when running the local backend (python app.py).");
      return;
    }
    if (!marginState.lines.length) {
      alert("Nothing to export — add at least one line.");
      return;
    }
    const btn = $("#btn-margin-export");
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Exporting…";
    try {
      const res = await fetch(`${API}/margin/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(marginPayload()),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const ref = ($("#margin-project").value || "DRAFT").replace(/[^A-Za-z0-9_-]/g, "");
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Qualitrol_Quote_${ref}.xlsx`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      alert("Export failed: " + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });

  // Opening the Configure & Quote tab carries over the reviewed BOQ when the
  // calculator is still empty (master's nav has no explicit hand-off button).
  document.querySelector('.tab-btn[data-tab="margin"]')?.addEventListener("click", () => {
    marginRefreshRecords(); // keep the case/quote picker in sync with History
    if (!marginState.lines.length) marginAutofillFromBoq();
  });

  // ── Custom dropdowns (styled option lists for native <select>s) ─────────
  // Native <select> popups can't be themed, so we hide the real element and
  // render a styled trigger + panel over it, mirroring its options and keeping
  // its value + `change` events intact (so existing handlers still fire).
  const csRegistry = {};

  function enhanceSelect(select) {
    if (!select || select.dataset.enhanced === "1") return;
    select.dataset.enhanced = "1";
    select.style.display = "none";

    const wrap = document.createElement("div");
    wrap.className = "cs";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cs-trigger";
    trigger.innerHTML =
      `<span class="cs-trigger-label"></span>` +
      `<svg class="cs-chevron" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`;
    const panel = document.createElement("div");
    panel.className = "cs-panel";
    panel.style.display = "none";

    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    wrap.appendChild(trigger);
    wrap.appendChild(panel);
    const labelEl = trigger.querySelector(".cs-trigger-label");

    const selectedOpt = () => select.options[select.selectedIndex] || null;
    function refreshLabel() {
      const opt = selectedOpt();
      labelEl.textContent = opt ? opt.textContent : "";
      labelEl.classList.toggle("placeholder", !opt || opt.value === "");
    }
    function addOption(o) {
      const el = document.createElement("div");
      el.className = "cs-option";
      if (o.value === "") el.classList.add("placeholder");
      if (o.selected) el.classList.add("selected");
      el.textContent = o.textContent;
      el.addEventListener("click", () => {
        select.value = o.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        refreshLabel();
        close();
      });
      panel.appendChild(el);
    }
    function buildPanel() {
      panel.innerHTML = "";
      Array.from(select.children).forEach((node) => {
        if (node.tagName === "OPTGROUP") {
          const lab = document.createElement("div");
          lab.className = "cs-optgroup-label";
          lab.textContent = node.label;
          panel.appendChild(lab);
          Array.from(node.children).forEach(addOption);
        } else if (node.tagName === "OPTION") {
          addOption(node);
        }
      });
    }
    function onDocClick(e) { if (!wrap.contains(e.target)) close(); }
    function onKey(e) { if (e.key === "Escape") close(); }
    function open() {
      buildPanel();
      panel.style.display = "";
      wrap.classList.add("open");
      document.addEventListener("click", onDocClick, true);
      document.addEventListener("keydown", onKey);
    }
    function close() {
      panel.style.display = "none";
      wrap.classList.remove("open");
      document.removeEventListener("click", onDocClick, true);
      document.removeEventListener("keydown", onKey);
    }
    trigger.addEventListener("click", (e) => {
      e.preventDefault();
      wrap.classList.contains("open") ? close() : open();
    });

    // Re-sync when options are repopulated (e.g. margin-load records refresh).
    const obs = new MutationObserver(() => {
      refreshLabel();
      if (wrap.classList.contains("open")) buildPanel();
    });
    obs.observe(select, { childList: true, subtree: true });

    csRegistry[select.id] = {
      refresh: () => {
        refreshLabel();
        if (wrap.classList.contains("open")) buildPanel();
      },
    };
    refreshLabel();
  }

  function syncCustomSelect(id) {
    const r = csRegistry[id];
    if (r) r.refresh();
  }

  ["margin-currency", "margin-load"].forEach((id) => enhanceSelect($("#" + id)));

  // ── Initial data load ──────────────────────────────────────────────────
  loadPoc1Status();
  loadSampleBoq();
  loadSyncStatus();
  updateHistoryCount();
  marginLoadCatalog();
  marginRefreshRecords();

  // Greet the user on entry: start a new case or continue a saved one.
  showWelcome();
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
