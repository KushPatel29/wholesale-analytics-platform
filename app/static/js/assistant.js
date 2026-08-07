(function () {
  const page = document.getElementById("assistantPage");
  if (!page) return;

  const messagesEl = document.getElementById("assistantMessages");
  const form = document.getElementById("assistantForm");
  const input = document.getElementById("assistantInput");
  const sendBtn = document.getElementById("assistantSendBtn");
  const clearBtn = document.getElementById("assistantClearBtn");
  const banner = document.getElementById("assistantBanner");
  const promptsEl = document.getElementById("assistantPrompts");
  const proactiveEl = document.getElementById("assistantProactive");
  const refreshProactiveBtn = document.getElementById("assistantRefreshProactiveBtn");
  const modeEl = document.getElementById("assistantMode");
  const detailEl = document.getElementById("assistantDetailLevel");
  const voiceEl = document.getElementById("assistantVoiceReady");
  const debugToggleEl = document.getElementById("assistantDebugToggle");
  const csrf = document.getElementById("assistantCsrf")?.value || "";

  const contextUrl = page.dataset.contextUrl;
  const chatUrl = page.dataset.chatUrl;
  const suggestionsUrl = page.dataset.suggestionsUrl;
  const proactiveUrl = page.dataset.proactiveUrl;
  const healthUrl = page.dataset.healthUrl;
  const threadUrlBase = page.dataset.threadUrlBase || "";
  const exportJobUrlBase = page.dataset.exportJobUrlBase || "";
  let threadId = "";
  let initialContext = {};
  let inflight = false;
  const exportJobPollers = new Map();

  const parseInitialContext = () => {
    try {
      initialContext = JSON.parse(page.dataset.initialContext || "{}");
    } catch (err) {
      initialContext = {};
    }
  };

  const setBanner = (text, type = "warn") => {
    if (!banner) return;
    if (!text) {
      banner.classList.add("d-none");
      banner.textContent = "";
      return;
    }
    banner.classList.remove("d-none");
    banner.textContent = text;
    banner.style.borderColor = type === "error" ? "#efb7b3" : "#f1d086";
    banner.style.background = type === "error" ? "#fff4f3" : "#fff9ea";
    banner.style.color = type === "error" ? "#9f2d1d" : "#6f4a0f";
  };

  const scrollToBottom = () => {
    if (!messagesEl) return;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  };

  const escapeHtml = (value) =>
    String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");

  const isDebugEnabled = () => Boolean(initialContext?.debug_available && debugToggleEl?.checked);

  const formatBusinessValue = (value, metricName = "") => {
    if (value === null || value === undefined || value === "") return "";
    const num = Number(value);
    if (!Number.isFinite(num)) return String(value);
    const metric = String(metricName || "").toLowerCase();
    if (metric.includes("margin") || metric.includes("pct") || metric.includes("percent") || metric.includes("coverage") || metric.includes("confidence") || metric.includes("share")) {
      return `${num.toFixed(1)}%`;
    }
    if (metric.includes("revenue") || metric.includes("profit") || metric.includes("cost") || metric.includes("amount") || metric.includes("sales") || metric.includes("credit")) {
      return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: Math.abs(num) >= 100 ? 0 : 2 }).format(num);
    }
    if (metric.includes("count") || metric.includes("order") || metric.includes("return") || metric.includes("unit")) {
      return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(num);
    }
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: Math.abs(num) >= 100 ? 0 : 2 }).format(num);
  };

  const renderMessage = ({ role, html }) => {
    const card = document.createElement("article");
    card.className = `chat-message ${role}`;
    card.innerHTML = `<div class="chat-role">${escapeHtml(role)}</div>${html}`;
    messagesEl.appendChild(card);
    scrollToBottom();
    return card;
  };

  const renderPrompts = (prompts) => {
    if (!promptsEl) return;
    const items = Array.isArray(prompts) ? prompts.filter(Boolean).slice(0, 10) : [];
    if (!items.length) {
      promptsEl.innerHTML = `<p class="text-muted small mb-0">No prompt suggestions are configured for this page.</p>`;
      return;
    }
    promptsEl.innerHTML = items
      .map(
        (prompt) =>
          `<button class="prompt-btn" type="button" data-prompt="${escapeHtml(prompt)}">${escapeHtml(prompt)}</button>`
      )
      .join("");
  };

  const renderProactiveCards = (cards) => {
    if (!proactiveEl) return;
    const items = Array.isArray(cards) ? cards.filter(Boolean).slice(0, 6) : [];
    if (!items.length) {
      proactiveEl.innerHTML = `<p class="text-muted small mb-0">No high-confidence proactive insights for this scope/window.</p>`;
      return;
    }
    proactiveEl.innerHTML = items
      .map((card) => {
        const severity = String(card?.severity || "medium").toLowerCase();
        return `
          <article class="proactive-card">
            <p class="proactive-card-title">${escapeHtml(card?.title || "Signal")}</p>
            <p class="proactive-card-body">${escapeHtml(card?.narrative || "")}</p>
            <span class="severity-chip severity-${escapeHtml(severity)}">${escapeHtml(severity)}</span>
          </article>
        `;
      })
      .join("");
  };

  const chipsHtml = (answer) => {
    const chips = [];
    if (answer?.module) chips.push(`Module: ${answer.module}`);
    if (answer?.question_type) chips.push(`Mode: ${answer.question_type}`);
    if (answer?.response_mode) chips.push(`Style: ${answer.response_mode}`);
    if (answer?.detail_level) chips.push(`Depth: ${answer.detail_level}`);
    if (Array.isArray(answer?.export_actions) && answer.export_actions.length) {
      chips.push(`Exports: ${answer.export_actions.length}`);
    }
    if (answer?.permission_limited) chips.push("Permission-limited");
    if (answer?.scope_note) chips.push(answer.scope_note);
    return chips.length
      ? `<div class="answer-chip-row">${chips
          .slice(0, 4)
          .map((chip) => `<span class="answer-chip">${escapeHtml(chip)}</span>`)
          .join("")}</div>`
      : "";
  };

  const answerTypeHeading = (answer) => {
    const token = String(answer?.question_type || "").trim();
    if (!token) return "";
    const labels = {
      page_summary: "Page Summary",
      driver_mover: "Driver / Mover Analysis",
      risk_watchout: "Risk / Watchout Analysis",
      trust_quality: "Trust / Data Quality",
      forecast_outlook: "Forecast Outlook",
      analyst_detail: "Analyst Detail",
      history_analytics: "Historical Analysis",
      comparison_analytics: "Comparison Analysis",
      ranking_analytics: "Ranking Analysis",
      grouped_analytics: "Grouped Metric Analysis",
      cross_module: "Cross-Module Analysis",
      risk_action: "Risk / Action Prioritization",
      export_request: "Export Request",
      modify_request: "Reviewable Modifications",
      proactive_insights: "Proactive Insights",
      anomaly_risk: "Anomaly / Risk Narrative",
      executive_digest: "Executive Digest",
      executive_summary: "Executive Summary",
      workflow_assist: "Workflow Assist",
      page_bundle: "Page Bundle",
      definition_help: "Definition / Help",
      page_help: "Page Help",
      live_analytics: "Live Analytics",
      returns_analytics: "Returns Analytics",
      returns_workflow: "Returns Workflow",
    };
    const label = labels[token] || token.replaceAll("_", " ");
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Answer Type</h3>
        <p class="answer-body">${escapeHtml(label)}</p>
      </section>
    `;
  };

  const sectionsHtml = (sections) => {
    const items = Array.isArray(sections) ? sections : [];
    return items
      .slice(0, 6)
      .map((section) => {
        const title = escapeHtml(section?.title || "Section");
        const body = escapeHtml(section?.body || "");
        return `<section class="answer-block"><h3 class="answer-heading">${title}</h3><p class="answer-body">${body}</p></section>`;
      })
      .join("");
  };

  const leadSummaryHtml = (answer) => {
    const body = String(answer?.direct_answer || "").trim();
    if (!body) return "";
    return `
      <section class="answer-block answer-lead">
        <p class="answer-lead-body">${escapeHtml(body)}</p>
      </section>
    `;
  };

  const detailPanelsHtml = (panels) => {
    const items = Array.isArray(panels) ? panels.filter((item) => item && typeof item === "object" && !item.admin_only).slice(0, 6) : [];
    if (!items.length) return "";
    return `
      <section class="answer-block">
        <div class="detail-panel-list">
          ${items
            .map((panel) => {
              const body = String(panel?.body || "").trim();
              const rows = Array.isArray(panel?.items) ? panel.items.filter(Boolean).slice(0, 6) : [];
              const tone = escapeHtml(String(panel?.tone || "neutral").toLowerCase());
              return `
                <details class="detail-panel detail-panel-${tone}">
                  <summary>${escapeHtml(panel?.title || "Details")}</summary>
                  ${body ? `<p class="answer-meta detail-panel-body">${escapeHtml(body)}</p>` : ""}
                  ${rows.length ? `<ul class="evidence-list">${rows.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
                </details>
              `;
            })
            .join("")}
        </div>
      </section>
    `;
  };

  const debugHtml = (answer, toolTrace, latencyMs) => {
    if (!isDebugEnabled()) return "";
    const debug = answer?.debug && typeof answer.debug === "object" ? answer.debug : {};
    const slots = debug?.query_slots && typeof debug.query_slots === "object" ? debug.query_slots : {};
    const pageBundle = debug?.page_bundle && typeof debug.page_bundle === "object" ? debug.page_bundle : {};
    const trace = Array.isArray(debug?.tool_trace) ? debug.tool_trace : Array.isArray(toolTrace) ? toolTrace : [];
    const slotRows = [
      ["Question Type", debug?.question_type || answer?.question_type],
      ["Module", debug?.module || answer?.module],
      ["Mode", debug?.response_mode || answer?.response_mode],
      ["Depth", debug?.detail_level || answer?.detail_level],
      ["Metric", slots?.metric],
      ["Dimension", slots?.group_by_dimension || slots?.primary_entity_type],
      ["Shape", slots?.query_shape],
    ].filter((item) => item[1] !== undefined && item[1] !== null && String(item[1]).trim() !== "");
    return `
      <section class="answer-block">
        <details class="detail-panel detail-panel-debug" open>
          <summary>Admin Debug</summary>
          <ul class="evidence-list">
            ${slotRows.map((item) => `<li><strong>${escapeHtml(item[0])}:</strong> ${escapeHtml(String(item[1]))}</li>`).join("")}
            <li><strong>Latency:</strong> ${escapeHtml(String(debug?.latency_ms || latencyMs || 0))} ms</li>
            <li><strong>Tool Trace:</strong> ${escapeHtml(trace.map((item) => `${item.tool}:${item.status}`).join(" | ") || "none")}</li>
            <li><strong>Page Bundle:</strong> ${escapeHtml(JSON.stringify(pageBundle).slice(0, 400) || "{}")}</li>
          </ul>
        </details>
      </section>
    `;
  };

  const evidenceCardsHtml = (cards) => {
    const items = Array.isArray(cards) ? cards : [];
    if (!items.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Evidence</h3>
        <div class="evidence-cards">
          ${items
            .slice(0, 8)
            .map((card) => {
              const highlights = Array.isArray(card?.highlights) ? card.highlights : [];
              const notes = Array.isArray(card?.notes) ? card.notes : [];
              return `
                <details class="evidence-card">
                  <summary>
                    <span>${escapeHtml(card?.title || "Tool Result")}</span>
                    <span class="status-chip status-${escapeHtml(card?.status || "unknown")}">${escapeHtml(card?.status || "unknown")}</span>
                  </summary>
                  ${
                    highlights.length
                      ? `<ul class="evidence-list">${highlights
                          .slice(0, 5)
                          .map(
                            (item) =>
                              `<li><strong>${escapeHtml(item?.label || "value")}:</strong> ${escapeHtml(item?.value)}</li>`
                          )
                          .join("")}</ul>`
                      : ""
                  }
                  ${
                    notes.length
                      ? `<p class="answer-meta">${notes
                          .slice(0, 3)
                          .map((n) => escapeHtml(n))
                          .join(" ")}</p>`
                      : ""
                  }
                </details>
              `;
            })
            .join("")}
        </div>
      </section>
    `;
  };

  const proactiveCardsHtml = (cards) => {
    const items = Array.isArray(cards) ? cards : [];
    if (!items.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">What Stands Out</h3>
        <div class="evidence-cards">
          ${items
            .slice(0, 6)
            .map((item) => {
              const severity = escapeHtml(item?.severity || "medium");
              return `
                <div class="proactive-card">
                  <p class="proactive-card-title">${escapeHtml(item?.title || "Signal")}</p>
                  <p class="proactive-card-body">${escapeHtml(item?.narrative || "")}</p>
                  <span class="severity-chip severity-${severity.toLowerCase()}">${severity}</span>
                </div>
              `;
            })
            .join("")}
        </div>
      </section>
    `;
  };

  const riskNarrativesHtml = (rows) => {
    const items = Array.isArray(rows) ? rows : [];
    if (!items.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Anomaly / Risk Narratives</h3>
        <ul class="evidence-list">
          ${items
            .slice(0, 6)
            .map((item) => `<li><strong>${escapeHtml(item?.title || "Risk")}:</strong> ${escapeHtml(item?.narrative || "")}</li>`)
            .join("")}
        </ul>
      </section>
    `;
  };

  const guidedInvestigationsHtml = (rows) => {
    const items = Array.isArray(rows) ? rows : [];
    if (!items.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Guided Investigations</h3>
        <ul class="guided-list">
          ${items
            .slice(0, 6)
            .map(
              (item) =>
                `<li><button type="button" class="followup-chip" data-followup="${escapeHtml(
                  item?.question || item?.title || ""
                )}">${escapeHtml(item?.title || "Investigate")}</button></li>`
            )
            .join("")}
        </ul>
      </section>
    `;
  };

  const digestHtml = (digest) => {
    const data = digest && typeof digest === "object" ? digest : null;
    if (!data || !data.executive_summary) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Executive Digest</h3>
        <div class="digest-card">
          <p><strong>Summary:</strong> ${escapeHtml(data.executive_summary)}</p>
          <p><strong>Audience:</strong> ${escapeHtml(data.audience || "leadership")} · <strong>Length:</strong> ${escapeHtml(
      data.length || "medium"
    )}</p>
        </div>
      </section>
    `;
  };

  const workflowAssistHtml = (assist) => {
    const data = assist && typeof assist === "object" ? assist : null;
    if (!data || !Array.isArray(data.body_lines) || !data.body_lines.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Workflow Assist Draft (Review Required)</h3>
        <ul class="evidence-list">
          ${data.body_lines.slice(0, 8).map((line) => `<li>${escapeHtml(line)}</li>`).join("")}
        </ul>
      </section>
    `;
  };

  const exportActionsHtml = (actions) => {
    const items = Array.isArray(actions) ? actions : [];
    if (!items.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Exports</h3>
        <div class="evidence-cards">
          ${items
            .slice(0, 4)
            .map((item) => {
              const status = String(item?.status || (item?.download_url || item?.api_download_url ? "completed" : "ready")).toLowerCase();
              const jobId = String(item?.job_id || "");
              const resolvedStatusUrl =
                item?.status_url || item?.api_status_url || (jobId && exportJobUrlBase ? `${exportJobUrlBase}${encodeURIComponent(jobId)}` : "");
              const downloadUrl = item?.download_url || item?.api_download_url || "";
              const canPoll = Boolean(jobId && resolvedStatusUrl);
              return `
                <div class="digest-card" data-export-job-id="${escapeHtml(jobId)}" data-export-status="${escapeHtml(status)}" data-export-status-url="${escapeHtml(
                  resolvedStatusUrl
                )}">
                  <p><strong>${escapeHtml(item?.filename || "Workbook")}</strong></p>
                  <p class="answer-meta">Format: ${escapeHtml(item?.format || "xlsx")} · Sheets: ${escapeHtml(
      (item?.sheets || []).join(", ")
    )}</p>
                  <p class="answer-meta export-status-line">Status: ${escapeHtml(status)}</p>
                  ${
                    Number(item?.chart_count || 0) > 0
                      ? `<p class="answer-meta">Charts requested: ${escapeHtml(String(item?.chart_count || 0))}</p>`
                      : ""
                  }
                  <div class="export-action-slot">
                    ${
                      downloadUrl
                        ? `<a class="btn btn-sm btn-outline-primary" href="${escapeHtml(downloadUrl)}">Download File</a>`
                        : canPoll
                        ? `<button type="button" class="btn btn-sm btn-outline-secondary export-job-refresh" data-export-status-url="${escapeHtml(
                            resolvedStatusUrl
                          )}">Refresh Status</button>`
                        : `<span class="answer-meta">Download link unavailable</span>`
                    }
                  </div>
                </div>
              `;
            })
            .join("")}
        </div>
      </section>
    `;
  };

  const exportPlanHtml = (plan) => {
    const data = plan && typeof plan === "object" ? plan : null;
    if (!data || !Array.isArray(data.sheets) || !data.sheets.length) return "";
    const sheets = data.sheets
      .slice(0, 8)
      .map((sheet) => (sheet && typeof sheet === "object" ? `${sheet.name || "Sheet"} (${sheet.type || "data"})` : String(sheet)))
      .join(", ");
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Export Plan</h3>
        <ul class="evidence-list">
          <li><strong>Format:</strong> ${escapeHtml(String(data.format || "xlsx"))}</li>
          <li><strong>Mode:</strong> ${escapeHtml(String(data.mode || "standard"))}</li>
          <li><strong>Sheets:</strong> ${escapeHtml(sheets)}</li>
          <li><strong>All Allowed Columns:</strong> ${escapeHtml(String(Boolean(data.include_all_allowed_columns)))}</li>
        </ul>
      </section>
    `;
  };

  const exportColumnsHtml = (columns) => {
    const data = columns && typeof columns === "object" ? columns : null;
    if (!data) return "";
    const allowed = Array.isArray(data.all_allowed_columns) ? data.all_allowed_columns : [];
    const excluded = Array.isArray(data.all_excluded_columns) ? data.all_excluded_columns : [];
    if (!allowed.length && !excluded.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Column Access</h3>
        <p class="answer-body">Allowed columns: ${escapeHtml(String(allowed.length))} · Excluded sensitive columns: ${escapeHtml(
      String(excluded.length)
    )}</p>
        ${allowed.length ? `<p class="answer-meta">${escapeHtml(allowed.slice(0, 16).join(", "))}</p>` : ""}
      </section>
    `;
  };

  const rankedResultsHtml = (rows, heading) => {
    const items = Array.isArray(rows) ? rows : [];
    if (!items.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">${escapeHtml(heading || "Ranked Results")}</h3>
        <div class="answer-table-wrap">
          <table class="answer-table">
            <thead>
              <tr>
                <th>Rank</th>
                <th>Entity</th>
                <th>Metric</th>
              </tr>
            </thead>
            <tbody>
              ${items
                .slice(0, 10)
                .map((row, index) => {
                  const label = row?.display_label || row?.label || row?.name || row?.group || "Entity";
                  const rawValue = row?.metric_value ?? row?.value ?? row?.revenue ?? "";
                  const value = row?.display_value || formatBusinessValue(rawValue, row?.metric_name || row?.metric || "");
                  const rank = row?.rank || index + 1;
                  return `
                    <tr>
                      <td>${escapeHtml(String(rank))}</td>
                      <td>${escapeHtml(label)}</td>
                      <td>${escapeHtml(String(value))}</td>
                    </tr>
                  `;
                })
                .join("")}
            </tbody>
          </table>
        </div>
      </section>
    `;
  };

  const nestedResultsHtml = (nested) => {
    const data = nested && typeof nested === "object" ? nested : null;
    const groups = Array.isArray(data?.groups) ? data.groups : [];
    if (!groups.length) return "";
    const renderStrategy = String(data?.render_strategy || "inline");
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Hierarchical Breakdown</h3>
        ${
          renderStrategy !== "inline"
            ? `<p class="answer-meta">Inline output is compacted. Use export for the full parent/child result.</p>`
            : ""
        }
        <div class="nested-group-list">
          ${groups
            .slice(0, renderStrategy === "inline" ? 6 : 4)
            .map((group) => {
              const children = Array.isArray(group?.children) ? group.children : [];
              return `
                <article class="nested-group-card">
                  <div class="nested-group-header">
                    <div>
                      <p class="nested-group-title">${escapeHtml(group?.parent_label || "Parent")}</p>
                      <p class="answer-meta">Rank ${escapeHtml(String(group?.rank || ""))} · Metric ${escapeHtml(
                String(group?.display_value || formatBusinessValue(group?.metric_value ?? "", data?.metric || ""))
              )}</p>
                    </div>
                  </div>
                  <ul class="nested-child-list">
                    ${children
                      .slice(0, renderStrategy === "inline" ? 5 : 3)
                      .map(
                        (child) => `
                          <li>
                            <span>${escapeHtml(child?.display_label || child?.label || child?.child_label || "Child")}</span>
                            <strong>${escapeHtml(String(child?.display_value || formatBusinessValue(child?.metric_value ?? child?.child_metric_value ?? child?.value ?? "", data?.metric || "")))}</strong>
                          </li>
                        `
                      )
                      .join("")}
                  </ul>
                </article>
              `;
            })
            .join("")}
        </div>
      </section>
    `;
  };

  const querySlotsHtml = (slots) => {
    const data = slots && typeof slots === "object" ? slots : null;
    if (!data) return "";
    const fields = [
      ["Intent", data.intent_type],
      ["Shape", data.query_shape],
      ["Metric", data.metric],
      ["Dimension", data.group_by_dimension || data.primary_entity_type],
      ["Parent", data.parent_entity_type],
      ["Child", data.child_entity_type],
      ["Filter", data.relationship_entity_name ? `${data.relationship_entity_type || "entity"}: ${data.relationship_entity_name}` : ""],
      ["Direction", data.ranking_direction],
      ["Limit", data.limit_n],
      ["Window", data.time_window],
    ].filter((item) => item[1] !== undefined && item[1] !== null && String(item[1]).trim() !== "");
    if (!fields.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Query Interpretation</h3>
        <ul class="evidence-list">
          ${fields.slice(0, 6).map((item) => `<li><strong>${escapeHtml(item[0])}:</strong> ${escapeHtml(String(item[1]))}</li>`).join("")}
        </ul>
      </section>
    `;
  };

  const modifyPreviewHtml = (preview) => {
    const items = Array.isArray(preview?.items) ? preview.items : [];
    if (!items.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Reviewable Modifications</h3>
        <ul class="evidence-list">
          ${items
            .slice(0, 6)
            .map((item) => {
              const data = item?.data && typeof item.data === "object" ? item.data : {};
              const sample = Object.entries(data)
                .slice(0, 3)
                .map(([key, value]) => `${key}=${String(value)}`)
                .join(", ");
              return `<li><strong>${escapeHtml(item?.title || "Draft")}:</strong> ${escapeHtml(sample || "Review-ready draft generated.")}</li>`;
            })
            .join("")}
        </ul>
      </section>
    `;
  };

  const pageBundleHtml = (bundle) => {
    const items = Array.isArray(bundle?.items) ? bundle.items : [];
    if (!items.length) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Page Bundle Context</h3>
        <ul class="evidence-list">
          ${items
            .slice(0, 4)
            .map((item) => {
              const data = item?.data && typeof item.data === "object" ? item.data : {};
              const module = data?.module || data?.page || "unknown";
              const selectedEntity = data?.selected_entity && typeof data.selected_entity === "object" ? data.selected_entity : {};
              const entityLabel = selectedEntity?.label || selectedEntity?.id || "none";
              const windowLabel =
                (data?.active_window && typeof data.active_window === "object" ? data.active_window.label : "") || "current";
              const sections = Array.isArray(data?.visible_sections) ? data.visible_sections.slice(0, 4).join(", ") : "";
              return `<li><strong>${escapeHtml(item?.title || "Context")}:</strong> module=${escapeHtml(
                module
              )}, entity=${escapeHtml(entityLabel)}, window=${escapeHtml(windowLabel)}${
                sections ? `, sections=${escapeHtml(sections)}` : ""
              }</li>`;
            })
            .join("")}
        </ul>
      </section>
    `;
  };

  const spokenSummaryHtml = (text) => {
    if (!text) return "";
    return `
      <section class="answer-block">
        <h3 class="answer-heading">Spoken Summary</h3>
        <div class="spoken-summary">${escapeHtml(text)}</div>
      </section>
    `;
  };

  const followupsHtml = (followups, actions) => {
    const followupItems = Array.isArray(followups) ? followups.filter(Boolean).slice(0, 8) : [];
    const actionItems = Array.isArray(actions) ? actions.filter(Boolean).slice(0, 8) : [];
    const followupSection = followupItems.length
      ? `
        <section class="answer-block">
          <h3 class="answer-heading">Follow-ups</h3>
          <div class="followup-chip-row">
            ${followupItems
              .map(
                (item) =>
                  `<button type="button" class="followup-chip" data-followup="${escapeHtml(item)}">${escapeHtml(item)}</button>`
              )
              .join("")}
          </div>
        </section>
      `
      : "";
    const actionSection = actionItems.length
      ? `
        <section class="answer-block">
          <h3 class="answer-heading">Recommended Actions</h3>
          <ul class="evidence-list">${actionItems.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </section>
      `
      : "";
    return followupSection + actionSection;
  };

  const renderAssistantAnswer = (answer, toolTrace, latencyMs) => {
    const fallbackSections = [
      { title: "What Matters", body: answer?.explanation || "" },
    ];
    const sections = Array.isArray(answer?.sections) && answer.sections.length ? answer.sections : fallbackSections;
    const proactiveCards = Array.isArray(answer?.proactive_cards) ? answer.proactive_cards : [];
    const riskNarratives = Array.isArray(answer?.risk_narratives) ? answer.risk_narratives : [];
    const guidedInvestigations = Array.isArray(answer?.guided_investigations) ? answer.guided_investigations : [];
    const exportActions = Array.isArray(answer?.export_actions) ? answer.export_actions : [];
    const detailPanels = Array.isArray(answer?.detail_panels) ? answer.detail_panels : [];
    const rankedResults = Array.isArray(answer?.ranked_results) ? answer.ranked_results : [];
    const groupedResults = Array.isArray(answer?.grouped_results) ? answer.grouped_results : [];
    const nestedResults = answer?.nested_results && typeof answer.nested_results === "object" ? answer.nested_results : {};
    const followups = Array.isArray(answer?.follow_up_suggestions) ? answer.follow_up_suggestions : answer?.next_actions || [];
    const actions = Array.isArray(answer?.action_suggestions) ? answer.action_suggestions : answer?.next_actions || [];
    const digest = answer?.digest && typeof answer.digest === "object" ? answer.digest : {};
    const workflowAssist = answer?.workflow_assist && typeof answer.workflow_assist === "object" ? answer.workflow_assist : {};
    const visibleSections = sections.filter((section) => String(section?.body || "").trim());
    const nonLeadSections = visibleSections.filter((section) => String(section?.body || "").trim() !== String(answer?.direct_answer || "").trim());
    const html = `
      ${leadSummaryHtml(answer)}
      ${sectionsHtml(nonLeadSections)}
      ${proactiveCardsHtml(proactiveCards)}
      ${riskNarrativesHtml(riskNarratives)}
      ${guidedInvestigationsHtml(guidedInvestigations)}
      ${digestHtml(digest)}
      ${workflowAssistHtml(workflowAssist)}
      ${exportActionsHtml(exportActions)}
      ${nestedResultsHtml(nestedResults)}
      ${rankedResultsHtml(rankedResults, "Ranked Results")}
      ${rankedResultsHtml(groupedResults, "Grouped Breakdown")}
      ${detailPanelsHtml(detailPanels)}
      ${spokenSummaryHtml(answer?.spoken_summary)}
      ${followupsHtml(followups, actions)}
      ${debugHtml(answer, toolTrace, latencyMs)}
    `;
    const node = renderMessage({ role: "assistant", html });
    startExportJobPolling(node);
  };

  const setExportCardStatus = (card, payload) => {
    if (!card || !payload || typeof payload !== "object") return;
    const status = String(payload.status || "unknown").toLowerCase();
    card.setAttribute("data-export-status", status);
    const statusLine = card.querySelector(".export-status-line");
    if (statusLine) {
      statusLine.textContent = `Status: ${status}`;
    }
    const actionSlot = card.querySelector(".export-action-slot");
    if (!actionSlot) return;
    const downloadUrl = payload.download_url || payload.api_download_url || "";
    if (downloadUrl) {
      actionSlot.innerHTML = `<a class="btn btn-sm btn-outline-primary" href="${escapeHtml(downloadUrl)}">Download File</a>`;
      return;
    }
    if (status === "error") {
      const errorText = payload.error ? ` ${String(payload.error)}` : "";
      actionSlot.innerHTML = `<span class="answer-meta">Export failed.${escapeHtml(errorText)}</span>`;
      return;
    }
    actionSlot.innerHTML = `<button type="button" class="btn btn-sm btn-outline-secondary export-job-refresh" data-export-status-url="${escapeHtml(
      String(payload.status_url || payload.api_status_url || "")
    )}">Refresh Status</button>`;
  };

  const pollExportJobCard = async (card, attempt = 0) => {
    if (!card || attempt > 80) return;
    const jobId = String(card.getAttribute("data-export-job-id") || "");
    if (!jobId) return;
    const statusUrl = String(card.getAttribute("data-export-status-url") || "");
    if (!statusUrl) return;
    try {
      const resp = await (window.authFetch || window.fetch)(statusUrl, { headers: { Accept: "application/json" } });
      if (!resp.ok) {
        if (exportJobPollers.has(jobId)) {
          window.clearTimeout(exportJobPollers.get(jobId));
          exportJobPollers.delete(jobId);
        }
        return;
      }
      const body = await resp.json();
      const job = body?.job && typeof body.job === "object" ? body.job : null;
      if (!job) return;
      setExportCardStatus(card, job);
      const nextStatus = String(job.status || "").toLowerCase();
      if (nextStatus === "pending" || nextStatus === "running") {
        const timeout = window.setTimeout(() => {
          void pollExportJobCard(card, attempt + 1);
        }, 2000);
        exportJobPollers.set(jobId, timeout);
      } else if (exportJobPollers.has(jobId)) {
        window.clearTimeout(exportJobPollers.get(jobId));
        exportJobPollers.delete(jobId);
      }
    } catch (_err) {
      if (exportJobPollers.has(jobId)) {
        window.clearTimeout(exportJobPollers.get(jobId));
        exportJobPollers.delete(jobId);
      }
    }
  };

  const startExportJobPolling = (root) => {
    const scope = root && typeof root.querySelectorAll === "function" ? root : messagesEl;
    const cards = scope ? scope.querySelectorAll("[data-export-job-id]") : [];
    cards.forEach((card) => {
      const status = String(card.getAttribute("data-export-status") || "").toLowerCase();
      const jobId = String(card.getAttribute("data-export-job-id") || "");
      if (!jobId) return;
      if (status !== "pending" && status !== "running") return;
      if (exportJobPollers.has(jobId)) return;
      const timeout = window.setTimeout(() => {
        void pollExportJobCard(card, 0);
      }, 1500);
      exportJobPollers.set(jobId, timeout);
    });
  };

  const setLoading = (isLoading) => {
    inflight = Boolean(isLoading);
    sendBtn.disabled = inflight;
    input.disabled = inflight;
    sendBtn.textContent = inflight ? "Working..." : "Ask Assistant";
  };

  const postChat = async (message) => {
    const mode = modeEl?.value || "standard";
    const detailLevel = detailEl?.value || "standard";
    const voiceReady = Boolean(voiceEl?.checked);
    const payload = {
      thread_id: threadId || undefined,
      message,
      mode,
      detail_level: detailLevel,
      voice_ready: voiceReady,
      context: {
        page: initialContext?.page || "overview",
        ref_path: window.location.pathname,
        filters: initialContext?.filters || {},
        entity: initialContext?.entity || initialContext?.page_state?.selected_entity || null,
        visible_sections: initialContext?.page_state?.visible_sections || [],
        mode,
        detail_level: detailLevel,
      },
    };
    const resp = await (window.authFetch || window.fetch)(chatUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf,
      },
      body: JSON.stringify(payload),
    });
    let body = {};
    try {
      body = await resp.json();
    } catch (err) {
      body = {};
    }
    if (!resp.ok) {
      const msg = body?.message || body?.error || `Request failed (${resp.status})`;
      throw new Error(msg);
    }
    return body;
  };

  const runQuestion = async (message) => {
    if (!message || inflight) return;
    setBanner("");
    renderMessage({ role: "user", html: `<p class="answer-body">${escapeHtml(message)}</p>` });
    setLoading(true);
    try {
      const out = await postChat(message);
      threadId = out.thread_id || threadId;
      if (out.status === "forbidden") {
        setBanner(
          "Response is permission-limited. The assistant only returned data allowed by your role/scope.",
          "warn"
        );
      }
      renderAssistantAnswer(out.answer || {}, out.tool_trace || [], out.latency_ms || 0);
      if (threadId && threadUrlBase) {
        void loadThread(threadId);
      }
    } catch (err) {
      setBanner(err.message || "Assistant request failed. Try again.", "error");
      renderMessage({
        role: "assistant",
        html: `<p class="answer-body">I couldn't complete that request. Please retry, or narrow your question.</p>`,
      });
    } finally {
      setLoading(false);
    }
  };

  const bindPrompts = () => {
    if (!promptsEl) return;
    promptsEl.addEventListener("click", (event) => {
      const target = event.target.closest("[data-prompt]");
      if (!target) return;
      const prompt = target.getAttribute("data-prompt") || "";
      input.value = prompt;
      input.focus();
      runQuestion(prompt);
    });
  };

  const bindFollowups = () => {
    if (!messagesEl) return;
    messagesEl.addEventListener("click", (event) => {
      const target = event.target.closest("[data-followup]");
      if (!target) return;
      const prompt = target.getAttribute("data-followup") || "";
      if (!prompt) return;
      input.value = prompt;
      input.focus();
      runQuestion(prompt);
    });
    messagesEl.addEventListener("click", (event) => {
      const target = event.target.closest(".export-job-refresh");
      if (!target) return;
      const card = target.closest("[data-export-job-id]");
      if (!card) return;
      const statusUrl = target.getAttribute("data-export-status-url") || card.getAttribute("data-export-status-url") || "";
      if (!statusUrl) return;
      card.setAttribute("data-export-status-url", statusUrl);
      void pollExportJobCard(card, 0);
    });
  };

  const loadThread = async (id) => {
    if (!id || !threadUrlBase) return;
    try {
      await (window.authFetch || window.fetch)(`${threadUrlBase}${encodeURIComponent(id)}`, {
        headers: { Accept: "application/json" },
      });
    } catch (err) {
      // Non-blocking.
    }
  };

  const loadHealth = async () => {
    if (!healthUrl) return;
    try {
      const resp = await (window.authFetch || window.fetch)(healthUrl, {
        headers: { Accept: "application/json" },
      });
      if (!resp.ok) return;
      const body = await resp.json();
      if (body?.status === "degraded") {
        setBanner("Assistant provider is degraded. Answers will use fallback synthesis where needed.", "warn");
      }
    } catch (err) {
      // Non-blocking.
    }
  };

  const loadSuggestions = async () => {
    if (!suggestionsUrl) return;
    try {
      const query = new URLSearchParams({
        page: initialContext?.page || "overview",
        ref: window.location.pathname,
      });
      const resp = await (window.authFetch || window.fetch)(`${suggestionsUrl}?${query.toString()}`, {
        headers: { Accept: "application/json" },
      });
      if (!resp.ok) return;
      const body = await resp.json();
      if (body?.status === "ok") {
        renderPrompts(body.suggestions || []);
      }
    } catch (err) {
      // Non-blocking.
    }
  };

  const loadProactive = async (triggeredBy = "page_load") => {
    if (!proactiveUrl || !proactiveEl) return;
    try {
      const query = new URLSearchParams({
        page: initialContext?.page || "overview",
        ref: window.location.pathname,
        triggered_by: triggeredBy,
      });
      const resp = await (window.authFetch || window.fetch)(`${proactiveUrl}?${query.toString()}`, {
        headers: { Accept: "application/json" },
      });
      if (!resp.ok) {
        renderProactiveCards([]);
        return;
      }
      const body = await resp.json();
      renderProactiveCards(body?.cards || []);
    } catch (err) {
      renderProactiveCards([]);
    }
  };

  const loadContext = async () => {
    if (!contextUrl) return;
    try {
      const resp = await (window.authFetch || window.fetch)(contextUrl, {
        headers: { Accept: "application/json" },
      });
      if (!resp.ok) return;
      const body = await resp.json();
      if (body?.context) {
        initialContext = body.context;
        if (Array.isArray(initialContext?.suggested_prompts)) {
          renderPrompts(initialContext.suggested_prompts);
        }
        void loadProactive("context_refresh");
      }
    } catch (err) {
      // Non-blocking.
    }
  };

  const renderWelcome = () => {
    const moduleList = Object.entries(initialContext?.module_access || {})
      .filter((entry) => Boolean(entry[1]))
      .map((entry) => entry[0])
      .join(", ");
    const pageState = initialContext?.page_state || {};
    const windowLabel = pageState?.active_window?.label || "auto";
    renderMessage({
      role: "assistant",
      html: `
        <section class="answer-block answer-lead">
          <p class="answer-lead-body">Ask for trends, rankings, drivers, risks, or next actions. I’ll answer from your live scoped analytics and keep the summary concise by default.</p>
          <p class="answer-meta">Available modules: ${escapeHtml(moduleList || "none")} · Active window: ${escapeHtml(windowLabel)}</p>
        </section>
      `,
    });
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const msg = String(input.value || "").trim();
    if (!msg) return;
    input.value = "";
    runQuestion(msg);
  });

  clearBtn.addEventListener("click", () => {
    threadId = "";
    messagesEl.innerHTML = "";
    setBanner("");
    renderWelcome();
    void loadProactive("manual_refresh");
  });

  if (refreshProactiveBtn) {
    refreshProactiveBtn.addEventListener("click", () => {
      void loadProactive("manual_refresh");
    });
  }

  parseInitialContext();
  if (Array.isArray(initialContext?.suggested_prompts)) {
    renderPrompts(initialContext.suggested_prompts);
  }
  bindPrompts();
  bindFollowups();
  renderWelcome();
  loadContext();
  loadSuggestions();
  loadProactive("page_load");
  loadHealth();
})();
