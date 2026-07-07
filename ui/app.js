const state = {
  operation: "add",
  a: null,
  b: null,
};

const results = document.querySelector("#results");
const output = document.querySelector("#output");
const reviewOutput = document.querySelector("#reviewOutput");
const resetOutput = document.querySelector("#resetOutput");
const objectDetail = document.querySelector("#objectDetail");
const craftSummary = document.querySelector("#craftSummary");
const topMatches = document.querySelector("#topMatches");

document.querySelector("#opAdd").addEventListener("click", () => setOperation("add"));
document.querySelector("#opSubtract").addEventListener("click", () => setOperation("subtract"));
document.querySelector("#searchButton").addEventListener("click", search);
document.querySelector("#searchInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") search();
});
document.querySelector("#craftButton").addEventListener("click", craft);
document.querySelector("#promoteButton").addEventListener("click", () => review("promote"));
document.querySelector("#rejectButton").addEventListener("click", () => review("reject"));
document.querySelector("#mergeButton").addEventListener("click", () => review("merge"));
document.querySelector("#resetButton").addEventListener("click", resetToInitial);

search();

function setOperation(operation) {
  state.operation = operation;
  document.querySelector("#opAdd").classList.toggle("active", operation === "add");
  document.querySelector("#opSubtract").classList.toggle("active", operation === "subtract");
}

async function search() {
  const q = document.querySelector("#searchInput").value.trim();
  const data = await getJson(`/api/search?q=${encodeURIComponent(q)}&limit=30`);
  results.innerHTML = "";
  for (const obj of data.objects) {
    const item = document.createElement("article");
    item.className = "result";
    item.innerHTML = `
      <button class="result-main" type="button">
        <strong>${escapeHtml(obj.emoji || "")} ${escapeHtml(obj.name)} #${obj.id}</strong>
        <small>${escapeHtml(obj.type)} · ${escapeHtml(obj.description || "")}</small>
      </button>
      <div class="result-actions">
        <button type="button" data-slot="A">A</button>
        <button type="button" data-slot="B">B</button>
        <button type="button" data-detail="true">详情</button>
      </div>
    `;
    item.querySelector(".result-main").addEventListener("click", () => showDetail(obj.id));
    item.querySelector('[data-slot="A"]').addEventListener("click", () => selectObject(obj, "A"));
    item.querySelector('[data-slot="B"]').addEventListener("click", () => selectObject(obj, "B"));
    item.querySelector("[data-detail]").addEventListener("click", () => showDetail(obj.id));
    results.appendChild(item);
  }
}

function selectObject(obj, slot = null) {
  if (slot === "A") {
    state.a = obj;
  } else if (slot === "B") {
    state.b = obj;
  } else if (!state.a) {
    state.a = obj;
  } else if (!state.b) {
    state.b = obj;
  } else {
    state.a = obj;
    state.b = null;
  }
  renderSlots();
}

function renderSlots() {
  renderSlot("A", state.a);
  renderSlot("B", state.b);
}

function renderSlot(slot, obj) {
  document.querySelector(`#slot${slot}Name`).textContent = obj ? `${obj.emoji || ""} ${obj.name} #${obj.id}` : "未选择";
  document.querySelector(`#slot${slot}Desc`).textContent = obj ? `${obj.type} · ${obj.description || ""}` : "";
}

async function showDetail(id) {
  objectDetail.className = "detail";
  objectDetail.innerHTML = '<small>加载中...</small>';
  const data = await getJson(`/api/objects/${id}`);
  if (data.error) {
    objectDetail.innerHTML = `<strong>详情读取失败</strong><small>${escapeHtml(data.error)}</small>`;
    return;
  }
  renderDetail(data.object);
}

function renderDetail(obj) {
  objectDetail.className = "detail";
  objectDetail.innerHTML = `
    <div class="detail-row">
      <strong>${escapeHtml(obj.emoji || "")} ${escapeHtml(obj.name)} #${obj.id}</strong>
      <small>${escapeHtml(obj.type)} · ${escapeHtml(obj.status)}</small>
    </div>
    ${fieldRow("描述", obj.description)}
    ${fieldRow("角色摘要", obj.character_summary)}
    ${fieldRow("背景", obj.background)}
    ${fieldRow("特长", obj.specialty)}
    ${fieldRow("弱点", obj.weakness)}
    ${fieldRow("来源", obj.source)}
    ${fieldRow("发现方式", obj.discovery_method)}
  `;
}

async function craft() {
  if (!state.a || !state.b) {
    craftSummary.className = "summary empty";
    craftSummary.textContent = "请先选择 A 和 B。";
    return;
  }
  const data = await postJson("/api/craft", {
    a_id: state.a.id,
    b_id: state.b.id,
    operation: state.operation,
    persist: true,
    use_vectors: false,
  });
  renderCraft(data);
  output.textContent = JSON.stringify(data, null, 2);
}

function renderCraft(data) {
  if (data.error || !data.success) {
    craftSummary.className = "summary";
    craftSummary.innerHTML = `
      <div class="summary-row">
        <strong>合成失败</strong>
        <small>${escapeHtml(data.error || data.failure_reason || "unknown")}</small>
      </div>
    `;
    topMatches.innerHTML = "";
    return;
  }
  const result = data.result || {};
  const candidate = data.candidate;
  craftSummary.className = "summary";
  craftSummary.innerHTML = `
    <div class="summary-row">
      <strong>${escapeHtml(result.emoji || "")} ${escapeHtml(result.name || "")} #${escapeHtml(result.id ?? "transient")}</strong>
      <small>${escapeHtml(result.type || "")} · ${escapeHtml(result.status || "")}</small>
    </div>
    <div class="badge-row">
      ${badge(data.decision)}
      ${badge(data.cached ? "cached" : "not cached", data.cached ? "" : "warn")}
      ${badge(data.score_breakdown ? `score ${Number(data.score_breakdown.total).toFixed(2)}` : "no score", data.score_breakdown ? "" : "warn")}
    </div>
    ${fieldRow("说明", data.explanation)}
    ${fieldRow("结果描述", result.description)}
    ${candidate ? fieldRow("候选", `${candidate.name} · ${candidate.type} · ${candidate.source_reason}`) : ""}
  `;
  renderMatches(data.top_matches || []);
}

function renderMatches(matches) {
  if (matches.length === 0) {
    topMatches.innerHTML = '<section class="match"><strong>Top matches</strong><small>无召回匹配。</small></section>';
    return;
  }
  topMatches.innerHTML = `
    <strong>Top matches</strong>
    ${matches.map((match) => `
      <section class="match">
        <strong>${escapeHtml(match.name)} #${escapeHtml(match.object_id)}</strong>
        <small>${escapeHtml(match.type)} · score ${Number(match.score.total).toFixed(2)}</small>
        <small>${escapeHtml((match.reasons || []).join("；"))}</small>
      </section>
    `).join("")}
  `;
}

async function review(action) {
  const id = Number(document.querySelector("#reviewId").value);
  if (!id) {
    reviewOutput.textContent = "请填写 pending id。";
    return;
  }
  const payload = { id };
  if (action === "reject") payload.reason = document.querySelector("#rejectReason").value.trim();
  if (action === "merge") payload.canonical_id = Number(document.querySelector("#canonicalId").value);
  const data = await postJson(`/api/review/${action}`, payload);
  reviewOutput.textContent = JSON.stringify(data, null, 2);
}

async function resetToInitial() {
  if (!window.confirm("还原会删除除水、火、土、风以外的所有对象和路线，确定继续？")) {
    return;
  }
  const data = await postJson("/api/reset", {});
  if (!data.error && data.ok) {
    state.a = null;
    state.b = null;
    renderSlots();
    objectDetail.className = "detail empty";
    objectDetail.textContent = "";
    craftSummary.className = "summary empty";
    craftSummary.textContent = "";
    topMatches.innerHTML = "";
    output.textContent = "";
    await search();
  }
  resetOutput.textContent = JSON.stringify(data, null, 2);
}

async function getJson(url) {
  const response = await fetch(url);
  return response.json();
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return response.json();
}

function fieldRow(label, value) {
  if (value === null || value === undefined || value === "") return "";
  return `<div class="detail-row"><small>${escapeHtml(label)}</small><span>${escapeHtml(value)}</span></div>`;
}

function badge(value, tone = "") {
  return `<span class="badge ${tone}">${escapeHtml(value)}</span>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}
