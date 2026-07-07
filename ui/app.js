const state = {
  operation: "add",
  a: null,
  b: null,
};

const results = document.querySelector("#results");
const output = document.querySelector("#output");
const reviewOutput = document.querySelector("#reviewOutput");

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
    const button = document.createElement("button");
    button.className = "result";
    button.type = "button";
    button.innerHTML = `<strong>${escapeHtml(obj.emoji || "")} ${escapeHtml(obj.name)} #${obj.id}</strong><small>${escapeHtml(obj.type)} · ${escapeHtml(obj.description || "")}</small>`;
    button.addEventListener("click", () => selectObject(obj));
    results.appendChild(button);
  }
}

function selectObject(obj) {
  if (!state.a) {
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

async function craft() {
  if (!state.a || !state.b) {
    output.textContent = "请先选择 A 和 B。";
    return;
  }
  const data = await postJson("/api/craft", {
    a_id: state.a.id,
    b_id: state.b.id,
    operation: state.operation,
    persist: true,
    use_vectors: false,
  });
  output.textContent = JSON.stringify(data, null, 2);
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

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  })[char]);
}
