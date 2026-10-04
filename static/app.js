const state = {
  view: "overview",
  inputs: null,
  plans: [],
  plan: null,
  runs: [],
  run: null,
  target: null,
  reconcile: null,
  quarantine: { cases: [] },
  history: [],
  error: "",
  busy: false,
};

const views = [
  ["overview", "Overview"],
  ["inputs", "Data"],
  ["agent", "Agent"],
  ["plan", "Plan"],
  ["dryrun", "Dry run"],
  ["target", "Staging"],
  ["reconcile", "Reconciliation"],
  ["history", "History"],
];

document.body.addEventListener("click", onClick);
document.body.addEventListener("change", onChange);
boot();

async function boot() {
  try {
    await refresh();
  } catch (error) {
    state.error = error.message;
    render();
  }
}

async function refresh(planId) {
  const [inputs, plans, runs, history, target, reconcile, quarantine] = await Promise.all([
    api("/api/inputs"),
    api("/api/plans"),
    api("/api/runs"),
    api("/api/history"),
    api("/api/target"),
    api("/api/reconcile"),
    api("/api/quarantine"),
  ]);
  state.inputs = inputs;
  state.plans = plans;
  state.runs = runs;
  state.history = history;
  state.target = target;
  state.reconcile = reconcile;
  state.quarantine = quarantine;
  const wanted = planId || state.plan?.id;
  const chosen = plans.find((plan) => plan.id === wanted) || preferredPlan(plans);
  state.plan = chosen ? await api(`/api/plans/${chosen.id}`) : null;
  if (state.run && !runs.some((run) => run.id === state.run.id)) state.run = null;
  const chip = document.querySelector("#sample-chip");
  if (chip) chip.textContent = `${inputs.sample_count} customers`;
  render();
}

function preferredPlan(plans) {
  return [...plans].reverse().find((plan) => plan.status !== "superseded") || plans.at(-1) || null;
}

async function onClick(event) {
  const button = event.target.closest("button[data-action]");
  if (!button || button.disabled || state.busy) return;
  const action = button.dataset.action;
  if (action === "reset" && !window.confirm("Clear plans, runs, quarantine, and staging?")) return;
  try {
    state.error = "";
    if (action === "view") {
      state.view = button.dataset.view;
      if (state.view === "dryrun" && (!state.run || state.run.mode !== "dry_run")) {
        const latest = [...state.runs].reverse().find((run) => run.mode === "dry_run");
        if (latest) {
          setBusy(true);
          state.run = await api(`/api/runs/${latest.id}`);
        }
      }
      render();
      return;
    }
    if (action === "open-plan") {
      setBusy(true);
      state.plan = await api(`/api/plans/${button.dataset.plan}`);
      state.view = "plan";
      render();
      return;
    }
    if (action === "open-run") {
      setBusy(true);
      state.run = await api(`/api/runs/${button.dataset.run}`);
      state.view = state.run.mode === "dry_run" ? "dryrun" : "target";
      render();
      return;
    }
    setBusy(true);
    if (action === "propose") {
      state.plan = await api("/api/agent/propose", { method: "POST" });
      state.view = "agent";
      await refresh(state.plan.id);
    } else if (action === "propose-model") {
      state.plan = await api("/api/agent/propose?mode=model", { method: "POST" });
      state.view = "agent";
      await refresh(state.plan.id);
    } else if (action === "release-case") {
      const card = button.closest("section[data-case]");
      const record = {};
      card.querySelectorAll("[data-field]").forEach((input) => {
        record[input.dataset.field] = input.value;
      });
      const released = await api(`/api/quarantine/${card.dataset.case}/release`, {
        method: "POST",
        body: JSON.stringify({ record }),
      });
      if (released.release_errors?.length) {
        state.error = released.release_errors
          .map((error) => `${error.field}: ${error.message}`)
          .join(" ");
      }
      state.view = "quarantine";
      await refresh(state.plan?.id);
    } else if (action === "answer") {
      state.plan = await api(`/api/plans/${state.plan.id}/answers`, {
        method: "POST",
        body: JSON.stringify({ question_id: button.dataset.question, option_id: button.dataset.option }),
      });
      await refresh(state.plan.id);
    } else if (action === "approve") {
      const note = document.querySelector("#approval-note")?.value || "";
      state.plan = await api(`/api/plans/${state.plan.id}/approve`, {
        method: "POST",
        body: JSON.stringify({ note }),
      });
      await refresh(state.plan.id);
    } else if (action === "dry-run") {
      state.run = await api(`/api/plans/${state.plan.id}/dry-run`, { method: "POST" });
      state.view = "dryrun";
      await refresh(state.plan.id);
    } else if (action === "execute") {
      const planId = button.dataset.plan || state.plan.id;
      state.run = await api(`/api/plans/${planId}/execute`, { method: "POST" });
      state.view = "target";
      await refresh(planId);
    } else if (action === "rollback") {
      state.run = await api(`/api/runs/${button.dataset.run}/rollback`, { method: "POST" });
      state.view = "target";
      await refresh(state.plan?.id);
    } else if (action === "save-revision") {
      state.plan = await api(`/api/plans/${state.plan.id}/revisions`, {
        method: "POST",
        body: JSON.stringify({ mappings: collectMappings() }),
      });
      state.view = "plan";
      await refresh(state.plan.id);
    } else if (action === "reset") {
      await api("/api/reset", { method: "POST" });
      state.plan = null;
      state.run = null;
      state.view = "overview";
      await refresh();
    }
  } catch (error) {
    state.error = error.message;
  } finally {
    state.busy = false;
    document.body.classList.remove("busy");
    if (state.inputs) render();
  }
}

function onChange(event) {
  if (event.target.dataset.action !== "transform-changed") return;
  const row = event.target.closest("tr");
  row.querySelector(".params").innerHTML = paramFields(event.target.value, {});
}

function setBusy(busy) {
  state.busy = busy;
  document.body.classList.toggle("busy", busy);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}

function render() {
  document.querySelectorAll(".rail button").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === state.view);
  });
  const banner = document.querySelector("#banner");
  banner.hidden = !state.error;
  banner.textContent = state.error || "";
  if (!state.inputs) {
    document.querySelector("#main").innerHTML = `<div class="card">The workbench API is not reachable.</div>`;
    return;
  }
  const draw = {
    overview: viewOverview,
    inputs: viewInputs,
    agent: viewAgent,
    plan: viewPlan,
    dryrun: viewDryRun,
    quarantine: viewQuarantine,
    target: viewTarget,
    reconcile: viewReconcile,
    history: viewHistory,
  }[state.view];
  try {
    document.querySelector("#main").innerHTML = draw();
  } catch (error) {
    banner.hidden = false;
    banner.textContent = error.message;
    document.querySelector("#main").innerHTML = `<div class="card">This page could not be drawn.</div>`;
  }
}

function viewOverview() {
  const step = nextStep();
  const latest = latestRun();
  return `
    <div class="section-title"><h2>Customer migration</h2></div>
    <p class="lede">Move <strong>legacy_customer_export</strong> into <strong>customer_master</strong>. The agent proposes mappings. You approve the plan, dry-run it, then load accepted rows into staging.</p>
    <div class="notice">
      <div class="split"><strong>${esc(step.text)}</strong><button class="primary" type="button" data-action="view" data-view="${step.view}">${esc(step.label)}</button></div>
    </div>
    <div class="grid stats">
      ${stat(state.inputs.sample_count, "Source rows")}
      ${stat(state.plan ? `v${state.plan.version}` : "—", state.plan ? state.plan.status : "No plan")}
      ${stat(latest ? latest.accepted_count : "—", latestLabel(latest))}
      ${stat(state.target.count, "Rows in staging")}
    </div>
    <div class="grid two" style="margin-top:14px">
      <section class="card">
        <p class="kicker">Source</p>
        <ul>
          <li>legacy_customer_export</li>
          <li>${state.inputs.sample_count} customer rows</li>
          <li>${state.inputs.source_schema.fields.length} fields</li>
        </ul>
      </section>
      <section class="card">
        <p class="kicker">Staging</p>
        <ul>
          <li>customer_master</li>
          <li>${state.target.count} loaded rows</li>
          <li>${state.plan ? `${esc(state.plan.id)} · ${esc(state.plan.status)}` : "No plan yet"}</li>
        </ul>
      </section>
    </div>`;
}

function viewInputs() {
  return `
    <div class="section-title"><h2>Data</h2></div>
    <p class="lede">Source schema, target schema, the customer extract, and the transforms available for mapping.</p>
    <div class="grid two">
      ${schemaCard("Source", state.inputs.source_schema)}
      ${schemaCard("Target", state.inputs.target_schema)}
    </div>
    <h3>Transforms</h3>
    <div class="scroll"><table>
      <thead><tr><th>Name</th><th>What it does</th><th>Params</th></tr></thead>
      <tbody>${state.inputs.transforms.map((item) => `<tr><td><code>${esc(item.name)}</code></td><td>${esc(item.description)}</td><td>${esc(item.params.join(", ") || "—")}</td></tr>`).join("")}</tbody>
    </table></div>
    <h3>Customer extract</h3>
    ${recordTable(state.inputs.sample, Object.keys(state.inputs.sample[0]))}`;
}

function viewAgent() {
  const body = state.plan?.body;
  const configured = state.inputs.agent?.configured;
  return `
    <div class="section-title">
      <h2>Mapping agent</h2>
      <div class="actions">
        <button class="primary" type="button" data-action="propose" ${state.busy ? "disabled" : ""}>Run rules agent</button>
        <button class="secondary" type="button" data-action="propose-model" ${state.busy ? "disabled" : ""}>Run language-model agent</button>
      </div>
    </div>
    <p class="lede">Drafts field mappings, names incompatible and missing fields, and lists risks. Rows load only after you approve a plan.${configured ? ` Language model: ${esc(state.inputs.agent.model)}.` : ""}</p>
    ${body ? `
      <div class="notice">${esc(body.agent_summary)} ${body.agent_mode ? `<span class="pill">${esc(body.agent_mode)}</span>` : ""}</div>
      <div class="grid three">
        ${listCard("Incompatible fields", body.incompatibilities.map((item) => `${item.source_field} (${item.source_type}) → ${item.target_field} (${item.target_type}). Use ${item.suggested_transform}.`))}
        ${listCard("Missing or dropped", [...body.missing_target_fields.map((item) => `Missing on source: ${item}`), ...body.unmapped_source_fields.map((item) => `No target column: ${item}`)])}
        ${listCard("Risks", body.risks.map((item) => `${item.severity}: ${item.message}`))}
      </div>
      <h3>Proposed mappings</h3>
      ${mappingTable(body.mappings, false)}
      <h3>Tool trace</h3>
      <div class="card trace">${body.tool_trace.map((event) => `<div><strong>${esc(event.tool)}</strong> — ${esc(event.summary)}</div>`).join("")}</div>
    ` : `<div class="card">No proposal yet.</div>`}`;
}

function viewPlan() {
  if (!state.plan) return empty("Run the mapping agent before editing a plan.", "agent", "Go to agent");
  const plan = state.plan;
  const draft = plan.status === "draft";
  return `
    <div class="section-title">
      <h2>Plan ${esc(plan.id)}</h2>
      <div class="actions">
        ${draft && plan.open_questions === 0 ? `<button class="primary" type="button" data-action="approve">Approve plan</button>` : ""}
        ${plan.status === "approved" ? `<button class="primary" type="button" data-action="dry-run">Dry run</button><button class="secondary" type="button" data-action="execute">Execute</button>` : ""}
      </div>
    </div>
    <p>${pill(plan.status)} Version ${plan.version}${plan.parent_id ? ` · based on ${esc(plan.parent_id)}` : ""}${plan.approval_note ? ` · ${esc(plan.approval_note)}` : ""}</p>
    <div class="grid two">
      <section class="card">
        <h3>Versions</h3>
        ${state.plans.map((item) => `<div class="split"><button class="text-button" type="button" data-action="open-plan" data-plan="${item.id}">${esc(item.id)} · v${item.version}</button>${pill(item.status)}</div>`).join("")}
      </section>
      <section class="card">
        <h3>Clarifications</h3>
        ${plan.body.questions.map((question) => `
          <div class="question">
            <strong>${esc(question.target_field)}</strong>
            <p>${esc(question.prompt)}</p>
            ${question.answer ? `<span class="pill ok">Answered: ${esc(question.answer)}</span>` : (draft ? question.options.map((option) => `<button class="secondary" type="button" data-action="answer" data-question="${esc(question.id)}" data-option="${esc(option.id)}">${esc(option.label)}</button>`).join(" ") : `<span class="pill">Unanswered</span>`)}
          </div>`).join("")}
        ${draft && plan.open_questions === 0 ? `<label>Approval note<input id="approval-note" type="text" placeholder="Optional note kept in the history"></label>` : ""}
      </section>
    </div>
    <h3>Mappings</h3>
    ${draft ? editorTable(plan.body.mappings) : mappingTable(plan.body.mappings, true)}
    ${draft ? `<p><button class="secondary" type="button" data-action="save-revision">Save as new version</button></p>` : ""}`;
}

function viewDryRun() {
  if (!state.plan) return empty("A plan is required before a dry run.", "plan", "Go to plan");
  const runs = state.runs.filter((run) => run.mode === "dry_run");
  const run = state.run && state.run.mode === "dry_run" ? state.run : null;
  return `
    <div class="section-title">
      <h2>Dry run</h2>
      <button class="primary" type="button" data-action="dry-run" ${state.plan.status === "superseded" ? "disabled" : ""}>Run ${esc(state.plan.id)}</button>
    </div>
    <p class="muted">Checks this plan against every source row. Staging is not changed. The same plan produces the same counts.</p>
    ${runList(runs)}
    ${run ? runDetail(run) : `<div class="card">${runs.length ? "Select a dry run to see the field evidence." : "No dry run yet. Run the current plan to see counts and field evidence."}</div>`}`;
}

function viewTarget() {
  const executes = state.runs.filter((run) => run.mode === "execute");
  const approved = approvedPlan();
  const viewingApproved = state.plan?.id === approved?.id;
  return `
    <div class="section-title">
      <h2>Staging</h2>
      <div class="actions">
        <button class="primary" type="button" data-action="execute" ${approved ? `data-plan="${esc(approved.id)}"` : "disabled"}>Load ${approved ? esc(approved.id) : "approved plan"}</button>
      </div>
    </div>
    <p class="muted">Loads an approved plan into customer_master. A later run skips customers already loaded. Rollback removes only that run’s rows.</p>
    ${approved && !viewingApproved ? `<div class="notice">${state.plan ? `${esc(state.plan.id)} is ${esc(state.plan.status)}. ` : ""}Loading uses approved plan ${esc(approved.id)}.</div>` : ""}
    ${approved ? "" : `<div class="notice">Approve a plan before loading staging.</div>`}
    <div class="grid stats">
      ${stat(state.target.count, "Loaded rows")}
      ${stat(executes.length, "Execute runs")}
      ${stat(executes.filter((run) => run.is_retry && run.accepted_count === 0).length, "Retries")}
      ${stat(executes.filter((run) => run.status === "rolled_back").length, "Rollbacks")}
    </div>
    <h3>Runs</h3>
    ${executes.length ? `<div class="scroll"><table><thead><tr><th>Run</th><th>Plan</th><th>Status</th><th>Inserted</th><th>Skipped duplicates</th><th>Rejected</th><th></th></tr></thead><tbody>
      ${executes.map((run) => `<tr><td>${esc(run.id)}${run.is_retry && run.accepted_count === 0 ? " · retry" : ""}</td><td>${esc(run.plan_id)}</td><td>${pill(run.status)}</td><td>${run.accepted_count}</td><td>${run.duplicate_skipped_count}</td><td>${run.rejected_count}</td><td>${run.status === "completed" ? `<button class="danger" type="button" data-action="rollback" data-run="${run.id}">Roll back</button>` : ""} <button class="text-button" type="button" data-action="open-run" data-run="${run.id}">Evidence</button></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="card">No load yet.</div>`}
    <h3>customer_master</h3>
    ${state.target.rows.length ? recordTable(state.target.rows, ["customer_id", "full_name", "email", "phone_e164", "signed_up_on", "status", "credit_limit", "loyalty_points", "region_code", "_load_run_id"]) : `<div class="card">Staging is empty.</div>`}
    ${state.run && state.run.mode === "execute" ? `<h3>${esc(state.run.id)} evidence</h3>${runDetail(state.run)}` : ""}`;
}

function viewReconcile() {
  const item = state.reconcile;
  if (!item.expected) {
    return `<div class="section-title"><h2>Reconciliation</h2></div><div class="card">${esc(item.note)} Target currently holds ${item.target_count} rows.</div>`;
  }
  const rows = [
    ["Accepted rows", item.expected.accepted_rows, item.target.accepted_rows, item.deltas.accepted_rows],
    ["Credit limit sum", item.expected.credit_limit_sum, item.target.credit_limit_sum, item.deltas.credit_limit_sum],
    ["Loyalty points sum", item.expected.loyalty_points_sum, item.target.loyalty_points_sum, item.deltas.loyalty_points_sum],
  ];
  return `
    <div class="section-title"><h2>Reconciliation</h2><span class="${item.in_balance ? "delta-ok" : "delta-bad"}">${item.in_balance ? "In balance" : "Out of balance"}</span></div>
    <p class="lede">Approved plan ${esc(item.plan_id)}. ${item.source_sample_count} source rows, ${item.rejected_source_count} rejected by the plan.</p>
    ${item.in_balance ? "" : `<p class="muted">Staging can differ from the approved plan after a quarantine release or a rollback. The delta is that difference.</p>`}
    <h3>Source extract</h3>
    <p class="muted">Totals before invalid rows are removed. Values that cannot be parsed are counted separately.</p>
    <div class="scroll"><table>
      <thead><tr><th>Total</th><th>Parsable sum</th><th>Unparsed values</th><th>Blank values</th></tr></thead>
      <tbody>
        <tr><td>Rows</td><td>${esc(item.raw_source.rows)}</td><td>—</td><td>—</td></tr>
        <tr><td>Credit limit</td><td>${esc(item.raw_source.credit_limit_sum)}</td><td>${esc(item.raw_source.credit_unparsed)}</td><td>${esc(item.raw_source.credit_blank)}</td></tr>
        <tr><td>Loyalty points</td><td>${esc(item.raw_source.loyalty_points_sum)}</td><td>${esc(item.raw_source.loyalty_unparsed)}</td><td>${esc(item.raw_source.loyalty_blank)}</td></tr>
      </tbody>
    </table></div>
    <h3>Approved plan versus staging</h3>
    <div class="scroll"><table>
      <thead><tr><th>Total</th><th>Accepted source</th><th>Staging</th><th>Delta</th></tr></thead>
      <tbody>${rows.map(([name, expected, actual, delta]) => `<tr><td>${esc(name)}</td><td>${esc(expected)}</td><td>${esc(actual)}</td><td class="${delta === "0" || delta === "0.00" ? "delta-ok" : "delta-bad"}">${esc(delta)}</td></tr>`).join("")}</tbody>
    </table></div>`;
}

function viewQuarantine() {
  const cases = state.quarantine?.cases || [];
  const fields = state.inputs.source_schema.fields.map((field) => field.name);
  const approved = state.plans.some((plan) => plan.status === "approved");
  return `
    <div class="section-title"><h2>Quarantine</h2></div>
    <p class="lede">Rows that failed validation, with the field and the rule. Correct a row, then release it through the approved plan. A customer already in staging is skipped.</p>
    ${cases.length ? cases.map((item) => `
      <section class="card question" data-case="${esc(item.id)}">
        <div class="split"><h3>${esc(item.source_key || "Missing source key")}</h3><span>${esc(item.run_id)} ${pill(item.status)}</span></div>
        <ul>${item.errors.map((error) => `<li><code>${esc(error.field)}</code> ${esc(error.value || "")} — ${esc(error.message)}</li>`).join("")}</ul>
        ${item.status === "released" ? `<p class="muted">Released by ${esc(item.released_run_id || "a load")}.</p>` : `
          ${item.status === "skipped_duplicate" ? `<p class="muted">This customer is already in staging, so another release is skipped unless the source key changes.</p>` : ""}
          <div class="grid two">${fields.map((field) => `<label>${esc(field)}<input data-field="${esc(field)}" type="text" value="${esc((item.correction || item.record)[field] ?? "")}"></label>`).join("")}</div>
          <p><button class="primary" type="button" data-action="release-case" data-case="${esc(item.id)}" ${approved ? "" : "disabled"}>Release through approved plan</button></p>
          ${approved ? "" : `<p class="muted">Approve a plan before a quarantined row can be loaded.</p>`}
        `}
      </section>
    `).join("") : `<div class="card">No quarantined rows yet. Dry-run or execute a plan to fill this inbox.</div>`}`;
}

function eventLabel(type) {
  return {
    workspace_reset: "Workspace reset",
    plan_proposed: "Plan proposed",
    plan_revised: "Plan revised",
    plan_approved: "Plan approved",
    dry_run_completed: "Dry run completed",
    migration_executed: "Migration loaded",
    migration_retried: "Migration retried",
    migration_rolled_back: "Migration rolled back",
    quarantine_released: "Row released",
    quarantine_skipped_duplicate: "Duplicate skipped",
  }[type] || String(type || "").replaceAll("_", " ");
}

function historyDetail(event) {
  const detail = event.detail;
  if (!detail || typeof detail !== "object") return "";
  if (detail.customer_id) return detail.customer_id;
  if (detail.accepted_count != null) return `${detail.accepted_count} accepted, ${detail.rejected_count} rejected`;
  if (detail.deleted_rows != null) return `${detail.deleted_rows} rows removed`;
  if (detail.option_id) return String(detail.option_id).replaceAll("_", " ");
  if (detail.note) return detail.note;
  return "";
}

function viewHistory() {
  return `
    <div class="section-title"><h2>History</h2></div>
    <p class="muted">Approvals, dry runs, loads, retries, and rollbacks.</p>
    <ul class="timeline">
      ${state.history.map((event) => `<li><div><strong>${esc(eventLabel(event.event_type))}</strong><div class="muted">${esc(event.at)}</div></div><div>${esc([event.plan_id, event.run_id, historyDetail(event)].filter(Boolean).join(" · "))}</div></li>`).join("") || "<li>No events yet.</li>"}
    </ul>`;
}

function runDetail(run) {
  return `
    <div class="grid stats">
      ${stat(run.source_count, "Source")}
      ${stat(run.transformed_count, "Transformed")}
      ${stat(run.accepted_count, "Accepted")}
      ${stat(run.rejected_count, "Rejected")}
    </div>
    <p class="muted">${esc(run.plan_id)} · duplicates skipped: ${run.duplicate_skipped_count}.</p>
    <h3>Field-level evidence</h3>
    ${run.errors.length ? `<div class="scroll"><table><thead><tr><th>Source key</th><th>Field</th><th>Value</th><th>Rule</th><th>Message</th></tr></thead><tbody>
      ${run.errors.map((error) => `<tr><td>${esc(error.source_key || "—")}</td><td>${esc(error.field)}</td><td>${esc(error.value || "")}</td><td><code>${esc(error.rule)}</code></td><td>${esc(error.message)}</td></tr>`).join("")}
    </tbody></table></div>` : `<div class="card">No quarantined fields.</div>`}
    ${run.skipped?.length ? `<h3>Skipped as already loaded</h3><div class="card">${run.skipped.map((item) => esc(item.customer_id)).join(", ")}</div>` : ""}`;
}

function editorTable(mappings) {
  const byTarget = Object.fromEntries(mappings.map((mapping) => [mapping.target_field, mapping]));
  const transforms = state.inputs.transforms;
  return `<div class="scroll"><table><thead><tr><th>Target</th><th>Sources</th><th>Transform</th><th>Params</th></tr></thead><tbody>
    ${state.inputs.target_schema.fields.map((field) => {
      const mapping = byTarget[field.name] || { source_fields: [], transform: "trim", params: {} };
      return `<tr data-mapping="${esc(field.name)}">
        <td><strong>${esc(field.name)}</strong><div class="muted">${esc(field.type)}${field.required ? " · required" : ""}</div></td>
        <td>${state.inputs.source_schema.fields.map((source) => `<label class="inline"><input class="js-source" type="checkbox" value="${esc(source.name)}" ${mapping.source_fields.includes(source.name) ? "checked" : ""}> ${esc(source.name)}</label>`).join("")}</td>
        <td><select class="js-transform" data-action="transform-changed">${transforms.map((item) => `<option value="${esc(item.name)}" ${item.name === mapping.transform ? "selected" : ""}>${esc(item.label)}</option>`).join("")}</select></td>
        <td class="params">${paramFields(mapping.transform, mapping.params)}</td>
      </tr>`;
    }).join("")}
  </tbody></table></div>`;
}

function paramFields(transform, params) {
  if (transform === "parse_date") return `<input data-param="format" type="text" value="${esc(params.format || "%m/%d/%Y")}">`;
  if (transform === "constant") return `<input data-param="value" type="text" value="${esc(params.value || "")}">`;
  if (transform === "concat") return `<input data-param="separator" type="text" value="${esc(params.separator ?? " ")}">`;
  if (transform === "reject") return `<input data-param="message" type="text" value="${esc(params.message || "")}">`;
  if (transform === "map_enum") return `<textarea data-param="map">${esc(JSON.stringify(params.map || { A: "active", I: "inactive", S: "suspended" }, null, 2))}</textarea>`;
  return `<span class="muted">No parameters</span>`;
}

function collectMappings() {
  return [...document.querySelectorAll("[data-mapping]")].map((row) => {
    const target = row.dataset.mapping;
    const base = (state.plan.body.mappings || []).find((mapping) => mapping.target_field === target) || {};
    const transform = row.querySelector(".js-transform").value;
    const sourceFields = [...row.querySelectorAll(".js-source:checked")].map((input) => input.value);
    const params = {};
    row.querySelectorAll("[data-param]").forEach((input) => {
      if (input.dataset.param === "map") {
        try {
          params.map = JSON.parse(input.value);
        } catch (error) {
          throw new Error(`Map codes for ${target} must be JSON, for example {"A": "active"}.`);
        }
      } else params[input.dataset.param] = input.value;
    });
    return {
      ...base,
      target_field: target,
      source_fields: sourceFields,
      transform,
      params,
    };
  });
}

function mappingTable(mappings, showRisk) {
  return `<div class="scroll"><table><thead><tr><th>Target</th><th>Sources</th><th>Transform</th><th>Why</th>${showRisk ? "<th>Risk</th>" : ""}</tr></thead><tbody>
    ${mappings.map((mapping) => `<tr><td>${esc(mapping.target_field)}<div class="muted">${mapping.confidence == null ? "" : Number(mapping.confidence).toFixed(2)}</div></td><td>${esc(mapping.source_fields.join(", ") || "—")}</td><td><code>${esc(mapping.transform)}</code><div class="muted">${esc(JSON.stringify(mapping.params))}</div></td><td>${esc(mapping.rationale || "")}</td>${showRisk ? `<td>${esc(mapping.risk || "")}</td>` : ""}</tr>`).join("")}
  </tbody></table></div>`;
}

function schemaCard(title, schema) {
  return `<section class="card"><p class="kicker">${esc(title)}</p><h3>${esc(schema.name)}</h3><p>${esc(schema.description)}</p><p class="muted">Primary key ${esc(schema.primary_key.join(", "))}</p>
    <table><tbody>${schema.fields.map((field) => `<tr><td><code>${esc(field.name)}</code></td><td>${esc(field.type)}${field.enum ? " · " + esc(field.enum.join(", ")) : ""}</td><td>${field.required ? "required" : "optional"}</td><td>${esc(field.description)}</td></tr>`).join("")}</tbody></table></section>`;
}

function recordTable(rows, columns) {
  return `<div class="scroll"><table><thead><tr>${columns.map((column) => `<th>${esc(column)}</th>`).join("")}</tr></thead><tbody>
    ${rows.map((row) => `<tr>${columns.map((column) => `<td>${esc(row[column] ?? "")}</td>`).join("")}</tr>`).join("")}
  </tbody></table></div>`;
}

function listCard(title, items) {
  return `<section class="card"><h3>${esc(title)}</h3>${items.length ? `<ul>${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : "<p class='muted'>None</p>"}</section>`;
}

function runList(runs) {
  if (!runs.length) return "";
  return `<p>${runs.map((run) => `<button class="text-button" type="button" data-action="open-run" data-run="${run.id}">${esc(run.id)} · ${run.accepted_count} accepted, ${run.rejected_count} rejected</button>`).join(" · ")}</p>`;
}

function empty(text, view, label) {
  return `<div class="card"><p>${esc(text)}</p><button class="primary" type="button" data-action="view" data-view="${view}">${esc(label)}</button></div>`;
}

function stat(value, label) {
  return `<section class="card stat"><strong>${esc(value)}</strong><span>${esc(label)}</span></section>`;
}

function pill(status) {
  return `<span class="pill ${esc(status)}">${esc(status.replaceAll("_", " "))}</span>`;
}

function latestRun() {
  return [...state.runs].reverse()[0] || null;
}

function latestLabel(run) {
  if (!run) return "Last accepted";
  if (run.mode === "dry_run") return "Latest dry run accepted";
  if (run.is_retry && run.accepted_count === 0) return "Retry inserted";
  return "Last inserted";
}

function approvedPlan() {
  return [...state.plans].reverse().find((plan) => plan.status === "approved") || null;
}

function nextStep() {
  if (!state.plan) return { text: "Draft a mapping for the customer extract.", label: "Open agent", view: "agent" };
  if (state.plan.status === "draft" && state.plan.open_questions > 0) return { text: "Answer the open questions, then approve the plan.", label: "Open plan", view: "plan" };
  if (state.plan.status === "draft") return { text: "The questions are answered. Approve the plan before loading.", label: "Review plan", view: "plan" };
  if (!state.runs.some((run) => run.mode === "dry_run" && run.plan_id === state.plan.id)) return { text: "Dry-run the approved plan and review quarantine.", label: "Dry run", view: "dryrun" };
  if (!state.runs.some((run) => run.mode === "execute" && run.plan_id === state.plan.id && run.status === "completed")) return { text: "Load the approved plan into staging.", label: "Staging", view: "target" };
  return { text: "Compare the approved plan with staging.", label: "Reconcile", view: "reconcile" };
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}
