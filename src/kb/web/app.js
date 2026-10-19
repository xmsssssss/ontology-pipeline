const state = {
  page: "ontologies",
  dashboard: null,
  ontologies: [],
  selectedOntologyId: null,
  selectedVersion: null,
  selectedOntology: null,
  activeTab: "overview",
  querySource: "local",
  graphdb: null,
  runs: [],
  selectedRunId: null,
  selectedRun: null,
  facts: [],
  factStatus: "",
  factSearch: "",
  aiStatus: null,
  activeBuildRunId: window.localStorage.getItem("kb.activeBuildRunId") || null,
  activeBuild: null,
  buildPollTimer: null,
  runPollTimer: null,
};

const viewRoot = document.querySelector("#view-root");
const pageActions = document.querySelector("#page-actions");
const pageTitle = document.querySelector("#page-title");
const pageEyebrow = document.querySelector("#page-eyebrow");
const modalRoot = document.querySelector("#modal-root");
const toastRegion = document.querySelector("#toast-region");

const pageMeta = {
  ontologies: { eyebrow: "知识资产", title: "本体资产" },
  builds: { eyebrow: "文件流水线", title: "文档构建" },
  runs: { eyebrow: "构建与审核", title: "运行记录" },
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderCompactText(value, limit = 180) {
  const text = String(value ?? "").trim();
  if (!text) return "-";
  if (text.length <= limit) return escapeHtml(text);
  return `
    <details class="text-details">
      <summary>${escapeHtml(text.slice(0, limit))}...</summary>
      <div>${escapeHtml(text)}</div>
    </details>
  `;
}

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(Number(value || 0));
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDuration(value) {
  const total = Math.max(0, Math.round(Number(value || 0)));
  if (total < 60) return `${total} 秒`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) return `${minutes} 分 ${seconds} 秒`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

function elapsedSeconds(summary) {
  if (summary?.duration_seconds != null) return summary.duration_seconds;
  const started = summary?.started_at || summary?.created_at;
  if (!started) return 0;
  const start = new Date(started).getTime();
  if (Number.isNaN(start)) return 0;
  return Math.max(0, (Date.now() - start) / 1000);
}

function isActiveStatus(status) {
  return ["queued", "running", "cancelling"].includes(status);
}

function badge(status) {
  const labels = {
    draft: "草稿",
    published: "已发布",
    pending: "待审核",
    approved: "已批准",
    rejected: "已拒绝",
    completed: "已完成",
    completed_with_validation_errors: "校验异常",
    validation_failed: "校验失败",
    validated: "已校验",
    queued: "排队中",
    running: "构建中",
    cancelling: "取消中",
    cancelled: "已取消",
    failed: "失败",
    interrupted: "已中断",
    scanned: "已扫描",
    parsed: "已解析",
    segmented: "已切分",
    extracted: "已抽取",
    reviewed: "已审核",
    ontology_built: "已生成本体",
  };
  return `<span class="badge ${escapeHtml(status)}">${escapeHtml(labels[status] || status || "-")}</span>`;
}

function refreshIcons() {
  if (window.lucide) {
    window.lucide.createIcons({
      attrs: { "stroke-width": 1.8, "aria-hidden": "true" },
    });
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    let message = payload;
    if (typeof payload === "object" && payload !== null) {
      message = Array.isArray(payload.detail)
        ? payload.detail.map((item) => item.msg || JSON.stringify(item)).join("；")
        : payload.detail || JSON.stringify(payload);
    }
    throw new Error(message || `请求失败（${response.status}）`);
  }
  return payload;
}

function toast(message, kind = "success") {
  const item = document.createElement("div");
  item.className = `toast ${kind === "error" ? "error" : ""}`;
  item.innerHTML = `
    <i data-lucide="${kind === "error" ? "circle-alert" : "circle-check"}"></i>
    <span>${escapeHtml(message)}</span>
  `;
  toastRegion.append(item);
  refreshIcons();
  window.setTimeout(() => item.remove(), 4200);
}

function setPageHeader(page) {
  const meta = pageMeta[page];
  pageEyebrow.textContent = meta.eyebrow;
  pageTitle.textContent = meta.title;
  document.querySelectorAll(".nav-item[data-page]").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.page === page);
  });
}

function renderLoading() {
  viewRoot.innerHTML = `<div class="loading">加载中</div>`;
}

async function navigate(page) {
  state.page = page;
  if (page !== "runs") stopRunPolling();
  setPageHeader(page);
  if (page === "ontologies") await loadOntologiesPage();
  if (page === "builds") await loadBuildsPage();
  if (page === "runs") await loadRunsPage();
}

async function loadGraphDB(showResult = false) {
  const target = document.querySelector("#graphdb-status");
  if (target) {
    target.querySelector("small").textContent = "正在检查连接";
    target.querySelector(".status-dot").className = "status-dot is-idle";
  }
  try {
    state.graphdb = await api("/api/graphdb/status");
  } catch (error) {
    state.graphdb = { connected: false, error: error.message };
  }
  const status = state.graphdb;
  if (target) {
    const dot = target.querySelector(".status-dot");
    const detail = target.querySelector("small");
    dot.className = `status-dot ${status.connected ? "is-online" : "is-offline"}`;
    detail.textContent = status.connected
      ? status.repository_exists
        ? `${status.repository} 已就绪`
        : `${status.repository} 仓库不存在`
      : "未连接";
  }
  if (showResult) {
    if (status.connected) {
      toast(`GraphDB 已连接：${status.repository}`);
    } else {
      toast(status.error || "GraphDB 未连接", "error");
    }
  }
}

async function loadDashboard() {
  state.dashboard = await api("/api/dashboard");
}

async function loadOntologies() {
  const payload = await api("/api/ontologies");
  state.ontologies = payload.items || [];
  const selectedExists = state.ontologies.some(
    (item) => item.ontology_id === state.selectedOntologyId,
  );
  if (!selectedExists) {
    state.selectedOntologyId = state.ontologies[0]?.ontology_id || null;
    state.selectedVersion = state.ontologies[0]?.versions?.[0]?.version || null;
  }
  if (state.selectedOntologyId) {
    await loadOntologyDetail(state.selectedOntologyId, state.selectedVersion);
  } else {
    state.selectedOntology = null;
  }
}

async function loadOntologyDetail(ontologyId, version = null) {
  const group = state.ontologies.find((item) => item.ontology_id === ontologyId);
  const selectedVersion = version || group?.versions?.[0]?.version;
  if (!selectedVersion) {
    state.selectedOntology = null;
    return;
  }
  const payload = await api(`/api/ontologies/${encodeURIComponent(ontologyId)}/versions/${encodeURIComponent(selectedVersion)}`);
  state.selectedOntologyId = ontologyId;
  state.selectedVersion = selectedVersion;
  state.selectedOntology = payload;
}

function renderStats() {
  const counts = state.dashboard?.counts || {};
  const stats = [
    ["boxes", "本体", counts.ontologies],
    ["git-branch", "版本", counts.versions],
    ["circle-check-big", "已发布", counts.published],
    ["file-pen-line", "草稿", counts.drafts],
    ["waypoints", "三元组", counts.triples],
  ];
  return `
    <div class="stats-grid">
      ${stats
        .map(
          ([icon, label, value]) => `
            <div class="stat">
              <span class="stat-label"><i data-lucide="${icon}"></i>${label}</span>
              <strong class="stat-value">${formatNumber(value)}</strong>
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderOntologyList() {
  if (!state.ontologies.length) {
    return `
      <div class="empty">
        <div>
          <i data-lucide="library-big"></i>
          <strong>暂无本体版本</strong>
          <span>新建本体或导入 OWL 文件</span>
        </div>
      </div>
    `;
  }
  return state.ontologies
    .map((item) => {
      const latest = item.versions[0];
      const active = item.ontology_id === state.selectedOntologyId ? "is-active" : "";
      return `
        <button
          class="asset-item ${active}"
          data-action="select-ontology"
          data-id="${escapeHtml(item.ontology_id)}"
          data-version="${escapeHtml(latest.version)}"
        >
          <div class="asset-title-row">
            <strong>${escapeHtml(item.title || item.ontology_id)}</strong>
            ${badge(latest.status)}
          </div>
          <div class="asset-meta">
            <span class="mono">${escapeHtml(item.ontology_id)} / ${escapeHtml(latest.version)}</span>
            <span>${item.version_count} 个版本</span>
          </div>
        </button>
      `;
    })
    .join("");
}

function renderDetailOverview() {
  const detail = state.selectedOntology;
  if (!detail) return "";
  const info = detail.metadata;
  const group = state.ontologies.find((item) => item.ontology_id === info.ontology_id);
  const versions = group?.versions || [];
  return `
    <div class="meta-grid">
      <div class="meta-item"><span>本体 ID</span><strong class="mono">${escapeHtml(info.ontology_id)}</strong></div>
      <div class="meta-item"><span>版本</span><strong class="mono">${escapeHtml(info.version)}</strong></div>
      <div class="meta-item"><span>基础 IRI</span><strong class="mono">${escapeHtml(info.base_iri)}</strong></div>
      <div class="meta-item"><span>三元组</span><strong>${formatNumber(info.triple_count)}</strong></div>
      <div class="meta-item"><span>更新时间</span><strong>${escapeHtml(formatDate(info.updated_at))}</strong></div>
      <div class="meta-item"><span>内容 SHA-256</span><strong class="mono">${escapeHtml(info.content_sha256.slice(0, 20))}…</strong></div>
    </div>
    <div class="version-list">
      <div class="version-row header">
        <span>版本</span><span>状态</span><span>三元组</span><span>操作</span>
      </div>
      ${versions
        .map(
          (item) => `
            <div class="version-row">
              <span class="mono">${escapeHtml(item.version)}</span>
              <span>${badge(item.status)}</span>
              <span>${formatNumber(item.triple_count)}</span>
              <button
                class="button ghost small"
                data-action="select-version"
                data-id="${escapeHtml(info.ontology_id)}"
                data-version="${escapeHtml(item.version)}"
              >查看</button>
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderTurtleEditor() {
  const detail = state.selectedOntology;
  if (!detail) return "";
  const published = detail.metadata.status === "published";
  return `
    <div class="code-surface">
      <div class="code-toolbar">
        <span class="mono">${escapeHtml(detail.metadata.ontology_id)}-${escapeHtml(detail.metadata.version)}.ttl</span>
        <div class="toolbar-group">
          <span>${published ? "已发布版本只读" : `${formatNumber(detail.metadata.triple_count)} 个三元组`}</span>
          <button class="button small primary" data-action="save-turtle" ${published ? "disabled" : ""}>
            <i data-lucide="save"></i>保存
          </button>
        </div>
      </div>
      <textarea class="code-editor" id="turtle-editor" spellcheck="false" ${published ? "readonly" : ""}>${escapeHtml(detail.turtle)}</textarea>
    </div>
  `;
}

function renderQuery() {
  const detail = state.selectedOntology;
  if (!detail) return "";
  const baseIri = detail.metadata.base_iri;
  const sample = `PREFIX kb: <${baseIri}>\nSELECT ?s ?p ?o\nWHERE {\n  ?s ?p ?o .\n}\nLIMIT 50`;
  const graphdbDisabled = detail.metadata.status !== "published";
  return `
    <div class="query-layout">
      <div>
        <div class="query-controls">
          <div class="segmented">
            <button class="${state.querySource === "local" ? "is-active" : ""}" data-action="query-source" data-source="local">本地</button>
            <button class="${state.querySource === "graphdb" ? "is-active" : ""}" data-action="query-source" data-source="graphdb" ${graphdbDisabled ? "disabled" : ""}>GraphDB</button>
          </div>
          <button class="button primary small" data-action="run-query">
            <i data-lucide="play"></i>执行
          </button>
        </div>
        <div class="code-surface">
          <textarea class="code-editor query-editor" id="query-editor" spellcheck="false">${escapeHtml(sample)}</textarea>
        </div>
      </div>
      <div class="result-surface" id="query-result">
        <div class="result-placeholder">等待查询</div>
      </div>
    </div>
  `;
}

function renderOntologyDetail() {
  const detail = state.selectedOntology;
  if (!detail) {
    return `
      <section class="panel">
        <div class="empty detail-empty">
          <div>
            <i data-lucide="panel-left"></i>
            <strong>选择一个本体版本</strong>
            <span>查看元数据、Turtle 和 SPARQL</span>
          </div>
        </div>
      </section>
    `;
  }
  const info = detail.metadata;
  const body =
    state.activeTab === "overview"
      ? renderDetailOverview()
      : state.activeTab === "turtle"
        ? renderTurtleEditor()
        : renderQuery();
  return `
    <section class="panel">
      <div class="detail-head">
        <div>
          <div class="detail-title-row">
            <h2>${escapeHtml(info.title || info.ontology_id)}</h2>
            ${badge(info.status)}
          </div>
          <p>${escapeHtml(info.description || "未填写本体说明")}</p>
        </div>
        <div class="detail-actions">
          <a class="button small" href="/api/ontologies/${encodeURIComponent(info.ontology_id)}/versions/${encodeURIComponent(info.version)}/download">
            <i data-lucide="download"></i>导出
          </a>
          <button class="button small primary" data-action="publish">
            <i data-lucide="cloud-upload"></i>${info.status === "published" ? "重新发布" : "发布"}
          </button>
          <button class="icon-button" data-action="delete-version" title="删除版本">
            <i data-lucide="trash-2"></i>
          </button>
        </div>
      </div>
      <div class="tabs">
        <button class="tab ${state.activeTab === "overview" ? "is-active" : ""}" data-action="tab" data-tab="overview">概览</button>
        <button class="tab ${state.activeTab === "turtle" ? "is-active" : ""}" data-action="tab" data-tab="turtle">Turtle</button>
        <button class="tab ${state.activeTab === "query" ? "is-active" : ""}" data-action="tab" data-tab="query">SPARQL</button>
      </div>
      <div class="detail-body">${body}</div>
    </section>
  `;
}

async function loadOntologiesPage() {
  renderLoading();
  pageActions.innerHTML = `
    <button class="button" data-action="open-import"><i data-lucide="file-input"></i>导入 OWL</button>
    <button class="button primary" data-action="open-create"><i data-lucide="plus"></i>新建本体</button>
  `;
  refreshIcons();
  try {
    await Promise.all([loadDashboard(), loadOntologies()]);
    viewRoot.innerHTML = `
      ${renderStats()}
      <div class="workspace-grid">
        <section class="panel">
          <div class="panel-header">
            <div><h2>本体目录</h2><p>${state.ontologies.length} 个本体</p></div>
            <button class="icon-button" data-action="refresh-ontologies" title="刷新"><i data-lucide="refresh-cw"></i></button>
          </div>
          <div class="asset-list">${renderOntologyList()}</div>
        </section>
        ${renderOntologyDetail()}
      </div>
    `;
    refreshIcons();
  } catch (error) {
    renderError(error);
  }
}

function renderAISettingsStatus() {
  const ai = state.aiStatus || {};
  const ready = Boolean(ai.ready);
  const strategy = ai.extraction_strategy === "single_pass" ? "单轮抽取" : "SPIRES 两阶段";
  const detail = ready
    ? `${ai.model} · ${strategy} · ${ai.base_url}${ai.api_key_configured ? " · 密钥已配置" : ""}`
    : ai.message || "AI 抽取尚未配置";
  return `
    <div
      class="inline-status ${ready ? "is-ready" : ai.enabled ? "is-warning" : ""}"
      id="ai-settings-status"
    >
      <span class="status-dot ${ready ? "is-online" : ai.enabled ? "is-offline" : "is-idle"}"></span>
      <span class="inline-status-copy">
        <strong>AI 抽取${ready ? "已就绪" : "不可用"}</strong>
        <small>${escapeHtml(detail)}</small>
      </span>
      <button class="button small" data-action="open-ai-settings">
        <i data-lucide="settings-2"></i>${ready ? "调整配置" : "配置 AI"}
      </button>
    </div>
  `;
}

function renderTaskLogs(logs = []) {
  if (!logs.length) {
    return `<div class="task-log-empty">暂无运行日志</div>`;
  }
  return `
    <div class="task-log-list">
      ${logs
        .map(
          (item) => `
            <div class="task-log-row ${item.level === "error" ? "is-error" : item.level === "warning" ? "is-warning" : ""}">
              <time>${escapeHtml(formatDate(item.timestamp))}</time>
              <span>${escapeHtml(item.event || item.stage_label || "-")}</span>
              <b>${formatNumber(item.progress)}%</b>
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderTaskProgress(summary, logs = [], { runPage = false } = {}) {
  if (!summary) return "";
  const progress = Math.max(0, Math.min(100, Number(summary.progress || 0)));
  const active = isActiveStatus(summary.status);
  const count =
    summary.current != null && summary.total != null
      ? `${formatNumber(summary.current)} / ${formatNumber(summary.total)}`
      : "";
  const errors = summary.errors || [];
  const action = runPage ? "cancel-run" : "cancel-build";
  return `
    <div class="task-progress is-${escapeHtml(summary.status || "queued")}" data-run-id="${escapeHtml(summary.run_id)}">
      <div class="task-progress-head">
        <div>
          <div class="task-progress-title">
            <strong>${escapeHtml(summary.stage_label || "等待执行")}</strong>
            ${badge(summary.status)}
          </div>
          <span>${escapeHtml(summary.run_id)} · 已用 ${formatDuration(elapsedSeconds(summary))}${count ? ` · ${count}` : ""}</span>
        </div>
        <div class="task-progress-value">${progress}%</div>
      </div>
      <div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}">
        <span style="width: ${progress}%"></span>
      </div>
      <div class="task-progress-foot">
        <span>${summary.validation?.status ? `校验：${escapeHtml(summary.validation.status)}` : "等待生成校验结果"}</span>
        <div class="toolbar-group">
          ${
            active
              ? `<button class="button small danger" data-action="${action}" ${summary.status === "cancelling" ? "disabled" : ""}>
                  <i data-lucide="square"></i>${summary.status === "cancelling" ? "正在取消" : "取消任务"}
                </button>`
              : ""
          }
          <button class="button small" data-action="open-run" data-run-id="${escapeHtml(summary.run_id)}">
            <i data-lucide="arrow-up-right"></i>查看运行
          </button>
        </div>
      </div>
      ${
        errors.length
          ? `<div class="task-errors">${errors.map((error) => `<span>${escapeHtml(error)}</span>`).join("")}</div>`
          : ""
      }
      <details class="task-logs" ${active ? "open" : ""}>
        <summary>运行日志 <span>${formatNumber(logs.length)} 条</span></summary>
        ${renderTaskLogs(logs)}
      </details>
    </div>
  `;
}

function renderBuildTask() {
  if (!state.activeBuild?.summary) return "";
  return `
    <section class="panel build-task-panel" data-build-task>
      <div class="panel-header">
        <div><h2>当前构建任务</h2><p>后台执行，离开本页不会中断</p></div>
        <button class="icon-button" data-action="dismiss-build-task" title="隐藏任务卡片">
          <i data-lucide="x"></i>
        </button>
      </div>
      <div class="panel-body">${renderTaskProgress(state.activeBuild.summary, state.activeBuild.logs || [])}</div>
    </section>
  `;
}

function stopBuildPolling() {
  if (state.buildPollTimer) {
    window.clearTimeout(state.buildPollTimer);
    state.buildPollTimer = null;
  }
}

function setActiveBuild(runId, payload = null) {
  state.activeBuildRunId = runId;
  if (payload) state.activeBuild = payload;
  if (runId) {
    window.localStorage.setItem("kb.activeBuildRunId", runId);
  } else {
    window.localStorage.removeItem("kb.activeBuildRunId");
  }
}

function updateBuildTaskSurface() {
  const slot = document.querySelector("#build-task-slot");
  if (!slot) return;
  slot.innerHTML = renderBuildTask();
  refreshIcons();
}

function scheduleBuildPoll(delay = 1000) {
  stopBuildPolling();
  if (!state.activeBuildRunId) return;
  state.buildPollTimer = window.setTimeout(pollActiveBuild, delay);
}

async function pollActiveBuild() {
  const runId = state.activeBuildRunId;
  if (!runId) return;
  try {
    const payload = await api(
      `/api/runs/${encodeURIComponent(runId)}/progress?log_limit=100`,
    );
    if (runId !== state.activeBuildRunId) return;
    state.activeBuild = payload;
    updateBuildTaskSurface();
    if (isActiveStatus(payload.summary?.status)) {
      scheduleBuildPoll();
    } else {
      stopBuildPolling();
    }
  } catch (error) {
    if (String(error.message).includes("运行不存在")) {
      setActiveBuild(null);
      state.activeBuild = null;
      updateBuildTaskSurface();
      toast("当前构建任务记录已被删除", "error");
      return;
    }
    scheduleBuildPoll(2500);
  }
}

function startBuildPolling(runId) {
  setActiveBuild(runId);
  scheduleBuildPoll(100);
}

async function loadBuildsPage() {
  renderLoading();
  pageActions.innerHTML = "";
  try {
    state.aiStatus = await api("/api/ai/status");
  } catch (error) {
    state.aiStatus = {
      enabled: false,
      ready: false,
      model: "",
      message: error.message,
    };
  }
  const ai = state.aiStatus;
  const aiReady = Boolean(ai.ready);
  viewRoot.innerHTML = `
    <div id="build-task-slot">${renderBuildTask()}</div>
    <section class="panel build-panel">
      <div class="panel-header">
        <div><h2>构建输入</h2><p>规章文档与结构化数据</p></div>
      </div>
      <form class="panel-body form-grid" id="build-form">
        ${renderAISettingsStatus()}
        <label class="upload-zone" for="build-files">
          <div>
            <i data-lucide="file-up"></i>
            <strong>选择规章文件</strong>
            <small>TXT、DOCX、PDF、CSV、XLSX、JSON、XML</small>
          </div>
          <input id="build-files" name="files" type="file" multiple />
        </label>
        <div class="file-list" id="build-file-list"></div>
        <div class="field-row">
          <div class="field">
            <label for="build-ontology-id">本体 ID</label>
            <input class="input mono" id="build-ontology-id" name="ontology_id" value="regulation" required />
          </div>
          <div class="field">
            <label for="build-version">版本</label>
            <input class="input mono" id="build-version" name="version" value="0.1.0" required />
          </div>
        </div>
        <div class="field">
          <label for="build-base-iri">基础 IRI</label>
          <input class="input mono" id="build-base-iri" name="base_iri" value="https://example.org/regulation/" required />
        </div>
        <div class="field">
          <label for="build-run-id">运行 ID</label>
          <input class="input mono" id="build-run-id" name="run_id" placeholder="自动生成" />
        </div>
        <div class="field">
          <label for="build-extraction-mode">抽取模式</label>
          <select class="select" id="build-extraction-mode" name="extraction_mode">
            <option value="rule">规则抽取</option>
            <option value="hybrid" ${aiReady ? "" : "disabled"}>规则 + AI 混合</option>
            <option value="ai" ${aiReady ? "" : "disabled"}>仅 AI 抽取</option>
          </select>
          <small class="field-hint">混合模式保留规则结果，并补充 AI 候选事实。</small>
        </div>
        <label class="checkbox">
          <input id="build-auto-approve" name="auto_approve" type="checkbox" />
          自动批准全部候选事实
        </label>
        <div class="toolbar-group">
          <button class="button primary" id="build-submit" type="submit">
            <i data-lucide="wand-sparkles"></i>开始构建
          </button>
        </div>
      </form>
    </section>
  `;
  refreshIcons();
  if (state.activeBuildRunId) scheduleBuildPoll(100);
}

async function loadRuns() {
  const payload = await api("/api/runs");
  state.runs = payload.items || [];
  if (!state.runs.some((item) => item.run_id === state.selectedRunId)) {
    state.selectedRunId = state.runs[0]?.run_id || null;
  }
  if (state.selectedRunId) {
    await loadRunDetail(state.selectedRunId);
  } else {
    state.selectedRun = null;
    state.facts = [];
  }
}

async function loadRunDetail(runId) {
  state.selectedRunId = runId;
  const params = new URLSearchParams({ limit: "500" });
  if (state.factStatus) params.set("status", state.factStatus);
  if (state.factSearch) params.set("search", state.factSearch);
  const [detail, facts] = await Promise.all([
    api(`/api/runs/${encodeURIComponent(runId)}`),
    api(`/api/runs/${encodeURIComponent(runId)}/facts?${params}`),
  ]);
  state.selectedRun = detail;
  state.facts = facts.items || [];
}

function renderRunStats(summary) {
  const counts = summary.counts || {};
  const stats = [
    ["files", "文件", counts.files],
    ["file-text", "文档", counts.documents],
    ["list-tree", "条款", counts.clauses],
    ["waypoints", "事实", counts.facts_total],
    ["circle-check", "已批准", counts.facts_approved],
    ["clock-3", "待审核", counts.facts_pending],
    ["sparkles", "AI 事实", counts.facts_ai],
    ["binary", "Token", counts.ai_tokens],
    ["boxes", "三元组", counts.ontology_triples],
  ];
  return `
    <div class="stats-grid run-stats-grid">
      ${stats
        .map(
          ([icon, label, value]) => `
            <div class="stat">
              <span class="stat-label"><i data-lucide="${icon}"></i>${label}</span>
              <strong class="stat-value">${formatNumber(value)}</strong>
            </div>
          `,
        )
        .join("")}
    </div>
  `;
}

function renderRunMeta(summary) {
  const config = summary.task_config || {};
  const extraction = summary.extraction || {};
  const counts = summary.counts || {};
  const ontology = summary.ontology || {};
  const mode = config.extraction_mode || extraction.mode || "-";
  const strategy = extractionStrategyLabel(
    counts.ai_strategy || extraction.strategy || extraction.ai?.strategy,
  );
  const parent = summary.parent_run_id
    ? `<button class="button ghost small" data-action="open-run" data-run-id="${escapeHtml(summary.parent_run_id)}">${escapeHtml(summary.parent_run_id)}</button>`
    : "-";
  return `
    <div class="meta-grid run-meta-grid">
      <div class="meta-item"><span>当前阶段</span><strong>${escapeHtml(summary.stage_label || "-")}</strong></div>
      <div class="meta-item"><span>总耗时</span><strong>${escapeHtml(formatDuration(elapsedSeconds(summary)))}</strong></div>
      <div class="meta-item"><span>开始时间</span><strong>${escapeHtml(formatDate(summary.started_at || summary.created_at))}</strong></div>
      <div class="meta-item"><span>结束时间</span><strong>${escapeHtml(formatDate(summary.finished_at))}</strong></div>
      <div class="meta-item"><span>抽取模式</span><strong class="mono">${escapeHtml(mode)}</strong></div>
      <div class="meta-item"><span>抽取策略</span><strong>${escapeHtml(strategy)}</strong></div>
      <div class="meta-item"><span>本体版本</span><strong class="mono">${escapeHtml(ontology.id || "-")} / ${escapeHtml(ontology.version || "-")}</strong></div>
      <div class="meta-item"><span>校验状态</span><strong>${escapeHtml(summary.validation?.status || "not_run")}</strong></div>
      <div class="meta-item"><span>父运行</span><strong>${parent}</strong></div>
    </div>
  `;
}

function extractionStrategyLabel(strategy) {
  if (strategy === "spires") return "SPIRES 两阶段";
  if (strategy === "single_pass") return "单轮 JSON";
  if (strategy === "rule") return "规则抽取";
  return strategy || "-";
}

function renderRunExtractionStats(summary) {
  const counts = summary.counts || {};
  const ai = summary.extraction?.ai || {};
  const strategy = counts.ai_strategy || summary.extraction?.strategy || ai.strategy;
  if (!strategy || strategy === "rule") return "";

  const batches = Number(counts.ai_batches || ai.batches || 0);
  const totalTokens = Number(counts.ai_tokens || ai.total_tokens || 0);
  const promptTokens = Number(counts.ai_prompt_tokens || ai.prompt_tokens || 0);
  const completionTokens = Number(
    counts.ai_completion_tokens || ai.completion_tokens || 0,
  );
  const durationMs = Number(counts.ai_duration_ms || ai.duration_ms || 0);
  const stageMarkup =
    strategy === "spires"
      ? `
        <div class="extraction-stage">
          <span>实体阶段</span>
          <strong>${formatNumber(counts.ai_entity_stage_successful_batches)} / ${formatNumber(batches)} 批次成功</strong>
          <small>
            候选 ${formatNumber(counts.ai_entity_candidates)}
            · 接受 ${formatNumber(counts.ai_entity_accepted)}
            · 拒绝 ${formatNumber(counts.ai_entity_rejected)}
            · 失败 ${formatNumber(counts.ai_entity_stage_failed_batches)}
          </small>
        </div>
        <div class="extraction-stage">
          <span>关系阶段</span>
          <strong>${formatNumber(counts.ai_relation_stage_successful_batches)} / ${formatNumber(batches)} 批次成功</strong>
          <small>
            候选 ${formatNumber(counts.ai_relation_candidates)}
            · 接受 ${formatNumber(counts.ai_relation_accepted)}
            · 拒绝 ${formatNumber(counts.ai_relation_rejected)}
            · 失败 ${formatNumber(counts.ai_relation_stage_failed_batches)}
            · 跳过 ${formatNumber(counts.ai_relation_skipped_batches)}
          </small>
        </div>
      `
      : `
        <div class="extraction-stage">
          <span>单轮抽取</span>
          <strong>${formatNumber(counts.ai_single_pass_successful_batches)} / ${formatNumber(batches)} 批次成功</strong>
          <small>
            候选 ${formatNumber(counts.ai_relation_candidates)}
            · 接受 ${formatNumber(counts.ai_relation_accepted)}
            · 拒绝 ${formatNumber(counts.ai_relation_rejected)}
            · 失败 ${formatNumber(counts.ai_single_pass_failed_batches)}
          </small>
        </div>
      `;

  return `
    <div class="run-extraction">
      <div class="run-extraction-head">
        <div>
          <strong>AI 抽取统计</strong>
          <span>${escapeHtml(extractionStrategyLabel(strategy))} · ${formatNumber(batches)} 个批次 · ${formatDuration(durationMs / 1000)}</span>
        </div>
        <b>${formatNumber(totalTokens)} Token</b>
      </div>
      <div class="extraction-stage-grid">${stageMarkup}</div>
      <div class="run-extraction-foot">
        输入 Token ${formatNumber(promptTokens)} · 输出 Token ${formatNumber(completionTokens)}
      </div>
    </div>
  `;
}

function renderRunWarnings(summary) {
  const warnings = summary.warnings || [];
  if (!warnings.length) return "";
  return `
    <details class="run-warnings">
      <summary>构建警告 <span>${formatNumber(warnings.length)} 条</span></summary>
      <div>${warnings.map((warning) => `<div class="warning-text">${renderCompactText(warning, 260)}</div>`).join("")}</div>
    </details>
  `;
}

function renderRunList() {
  if (!state.runs.length) {
    return `
      <div class="empty">
        <div>
          <i data-lucide="history"></i>
          <strong>暂无运行记录</strong>
          <span>从文档构建开始</span>
        </div>
      </div>
    `;
  }
  return state.runs
    .map((item) => {
      const active = item.run_id === state.selectedRunId ? "is-active" : "";
      const progress = Math.max(0, Math.min(100, Number(item.progress || 0)));
      return `
        <button class="run-item ${active}" data-action="select-run" data-run-id="${escapeHtml(item.run_id)}">
          <code>${escapeHtml(item.run_id)}</code>
          <small class="run-item-status">${escapeHtml(formatDate(item.created_at))} · ${badge(item.status)}</small>
          <div class="run-mini-progress" aria-hidden="true"><span style="width: ${progress}%"></span></div>
          <small class="run-item-stage">
            ${escapeHtml(item.stage_label || "等待执行")} · ${progress}% · ${formatDuration(elapsedSeconds(item))}
          </small>
          <small>${formatNumber(item.counts?.facts_approved)} 已批准 · ${formatNumber(item.counts?.facts_pending)} 待审核</small>
        </button>
      `;
    })
    .join("");
}

function renderFactsTable() {
  if (!state.facts.length) {
    return `
      <div class="empty">
        <div>
          <i data-lucide="list-filter"></i>
          <strong>没有符合条件的事实</strong>
          <span>调整状态或搜索条件</span>
        </div>
      </div>
    `;
  }
  return `
    <div class="table-wrap">
      <table class="data-table facts-table">
        <thead>
          <tr>
            <th></th><th>状态</th><th>主体</th><th>关系</th><th>对象</th><th>抽取</th><th>证据</th>
          </tr>
        </thead>
        <tbody>
          ${state.facts
            .map(
              (fact) => `
                <tr>
                  <td><input class="fact-check" type="checkbox" value="${escapeHtml(fact.fact_id)}" /></td>
                  <td>${badge(fact.review_status)}</td>
                  <td>${escapeHtml(fact.subject)}</td>
                  <td class="mono">${escapeHtml(fact.predicate)}</td>
                  <td>${renderCompactText(`${fact.object ?? ""}${fact.unit ? ` ${fact.unit}` : ""}`, 120)}</td>
                  <td class="mono">${escapeHtml(fact.extraction_method || "-")}</td>
                  <td class="muted">${renderCompactText(fact.evidence_text || fact.condition || "-", 220)}</td>
                </tr>
              `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderRunDetail() {
  const detail = state.selectedRun;
  if (!detail) {
    return `
      <section class="panel">
        <div class="empty detail-empty">
          <div><i data-lucide="file-clock"></i><strong>选择一次运行</strong><span>查看事实和校验结果</span></div>
        </div>
      </section>
    `;
  }
  const summary = detail.summary;
  const counts = summary.counts || {};
  const active = isActiveStatus(summary.status);
  const canRegister =
    !active &&
    ["completed", "completed_with_validation_errors"].includes(summary.status) &&
    summary.validation?.status === "valid";
  return `
    <section class="panel">
      <div class="run-detail-head">
        <div>
          <div class="run-title-row">
            <h2 class="mono">${escapeHtml(summary.run_id)}</h2>
            ${badge(summary.status)}
          </div>
          <p>${escapeHtml(formatDate(summary.created_at))} · ${formatNumber(counts.facts_total)} 条事实 · ${formatNumber(counts.ontology_triples)} 个三元组</p>
        </div>
        <div class="detail-actions">
          <a class="button small" href="/api/runs/${encodeURIComponent(summary.run_id)}/export">
            <i data-lucide="archive"></i>导出
          </a>
          ${
            active
              ? `<button class="button small danger" data-action="cancel-run" ${summary.status === "cancelling" ? "disabled" : ""}>
                  <i data-lucide="square"></i>${summary.status === "cancelling" ? "正在取消" : "取消任务"}
                </button>`
              : `<button class="button small" data-action="retry-run">
                  <i data-lucide="rotate-ccw"></i>重跑
                </button>`
          }
          ${
            canRegister
              ? `<button class="button small primary" data-action="register-run">
                  <i data-lucide="library-big"></i>登记版本
                </button>`
              : ""
          }
          ${
            active
              ? ""
              : `<button class="icon-button" data-action="delete-run" title="删除运行记录">
                  <i data-lucide="trash-2"></i>
                </button>`
          }
        </div>
      </div>
      <div class="run-detail-body">
        <div data-run-progress-host>
          ${renderTaskProgress(summary, detail.logs || [], { runPage: true })}
        </div>
        ${renderRunStats(summary)}
        ${renderRunMeta(summary)}
        ${renderRunExtractionStats(summary)}
        ${renderRunWarnings(summary)}
      </div>
      ${
        active
          ? `<div class="run-processing-note">
              <i data-lucide="loader-circle"></i>
              <span>任务执行中，完成后可审核事实并登记本体版本。</span>
            </div>`
          : `<div class="toolbar">
              <div class="toolbar-group">
                <button class="button small" data-action="approve-selected"><i data-lucide="check"></i>批准所选</button>
                <button class="button small danger" data-action="reject-selected"><i data-lucide="x"></i>拒绝所选</button>
                <button class="button small" data-action="approve-all"><i data-lucide="check-check"></i>全部批准</button>
              </div>
              <div class="toolbar-group">
                <select class="select" id="fact-status" aria-label="事实状态">
                  <option value="">全部状态</option>
                  <option value="pending" ${state.factStatus === "pending" ? "selected" : ""}>待审核</option>
                  <option value="approved" ${state.factStatus === "approved" ? "selected" : ""}>已批准</option>
                  <option value="rejected" ${state.factStatus === "rejected" ? "selected" : ""}>已拒绝</option>
                </select>
                <input class="input" id="fact-search" value="${escapeHtml(state.factSearch)}" placeholder="搜索事实" aria-label="搜索事实" />
              </div>
            </div>
            ${renderFactsTable()}`
      }
    </section>
  `;
}

function renderRunsLayout() {
  if (state.page !== "runs") return;
  viewRoot.innerHTML = `
    <div class="run-layout">
      <section class="panel">
        <div class="panel-header">
          <div><h2>运行队列</h2><p>${state.runs.length} 次构建</p></div>
        </div>
        <div data-run-list>${renderRunList()}</div>
      </section>
      ${renderRunDetail()}
    </div>
  `;
  refreshIcons();
}

function stopRunPolling() {
  if (state.runPollTimer) {
    window.clearTimeout(state.runPollTimer);
    state.runPollTimer = null;
  }
}

function scheduleRunPoll(delay = 1000) {
  stopRunPolling();
  if (state.page !== "runs" || !state.selectedRunId) return;
  state.runPollTimer = window.setTimeout(pollSelectedRun, delay);
}

function startRunPolling() {
  if (state.page !== "runs" || !state.selectedRun?.summary) {
    stopRunPolling();
    return;
  }
  if (isActiveStatus(state.selectedRun.summary.status)) scheduleRunPoll(100);
  else stopRunPolling();
}

async function pollSelectedRun() {
  if (state.page !== "runs" || !state.selectedRunId) {
    stopRunPolling();
    return;
  }
  const runId = state.selectedRunId;
  try {
    const payload = await api(
      `/api/runs/${encodeURIComponent(runId)}/progress?log_limit=200`,
    );
    if (state.page !== "runs" || runId !== state.selectedRunId) return;
    const previousStatus = state.selectedRun?.summary?.status;
    const nextSummary = payload.summary;
    state.runs = state.runs.map((item) =>
      item.run_id === runId ? nextSummary : item,
    );
    state.selectedRun = {
      ...(state.selectedRun || {}),
      summary: nextSummary,
      logs: payload.logs || [],
    };
    const statusChanged = previousStatus !== nextSummary.status;
    if (statusChanged || !isActiveStatus(nextSummary.status)) {
      await loadRuns();
      renderRunsLayout();
      startRunPolling();
      return;
    }
    const list = document.querySelector("[data-run-list]");
    const progress = document.querySelector("[data-run-progress-host]");
    if (list) list.innerHTML = renderRunList();
    if (progress) {
      progress.innerHTML = renderTaskProgress(
        nextSummary,
        payload.logs || [],
        { runPage: true },
      );
    }
    refreshIcons();
    scheduleRunPoll();
  } catch (error) {
    if (String(error.message).includes("运行不存在")) {
      stopRunPolling();
      toast("运行记录已被删除", "error");
      await loadRunsPage();
      return;
    }
    scheduleRunPoll(2500);
  }
}

async function loadRunsPage() {
  renderLoading();
  stopRunPolling();
  pageActions.innerHTML = `
    <button class="button" data-action="refresh-runs"><i data-lucide="refresh-cw"></i>刷新</button>
    <button class="button primary" data-action="go-build"><i data-lucide="file-up"></i>构建</button>
  `;
  refreshIcons();
  try {
    await loadRuns();
    renderRunsLayout();
    startRunPolling();
  } catch (error) {
    renderError(error);
  }
}

function renderError(error) {
  viewRoot.innerHTML = `
    <section class="panel">
      <div class="empty detail-empty">
        <div>
          <i data-lucide="circle-alert"></i>
          <strong>页面加载失败</strong>
          <span>${escapeHtml(error.message)}</span>
        </div>
      </div>
    </section>
  `;
  refreshIcons();
  toast(error.message, "error");
}

function openModal(title, body, footer) {
  modalRoot.innerHTML = `
    <div class="modal-backdrop" data-action="close-modal">
      <section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" data-stop-modal>
        <div class="modal-header">
          <h2 id="modal-title">${escapeHtml(title)}</h2>
          <button class="icon-button" data-action="close-modal" aria-label="关闭"><i data-lucide="x"></i></button>
        </div>
        <div class="modal-body">${body}</div>
        <div class="modal-footer">${footer}</div>
      </section>
    </div>
  `;
  refreshIcons();
  modalRoot.querySelector('input:not([type="hidden"]), select, textarea, button')?.focus();
}

function closeModal() {
  modalRoot.innerHTML = "";
}

function openCreateModal() {
  openModal(
    "新建本体",
    `
      <form class="form-grid" id="create-form">
        <div class="field-row">
          <div class="field">
            <label for="create-id">本体 ID</label>
            <input class="input mono" id="create-id" name="ontology_id" value="regulation" required />
          </div>
          <div class="field">
            <label for="create-version">版本</label>
            <input class="input mono" id="create-version" name="version" value="0.1.0" required />
          </div>
        </div>
        <div class="field">
          <label for="create-title">名称</label>
          <input class="input" id="create-title" name="title" placeholder="规章知识本体" required />
        </div>
        <div class="field">
          <label for="create-base-iri">基础 IRI</label>
          <input class="input mono" id="create-base-iri" name="base_iri" value="https://example.org/regulation/" required />
        </div>
        <div class="field">
          <label for="create-description">说明</label>
          <textarea class="textarea" id="create-description" name="description"></textarea>
        </div>
      </form>
    `,
    `
      <button class="button" data-action="close-modal">取消</button>
      <button class="button primary" type="submit" form="create-form"><i data-lucide="plus"></i>创建</button>
    `,
  );
}

function openImportModal() {
  openModal(
    "导入 OWL",
    `
      <form class="form-grid" id="import-form">
        <label class="upload-zone" for="import-file">
          <div>
            <i data-lucide="file-input"></i>
            <strong>选择 OWL / RDF 文件</strong>
            <small>Turtle、RDF/XML、JSON-LD、N-Triples</small>
          </div>
          <input id="import-file" name="file" type="file" accept=".ttl,.owl,.rdf,.xml,.jsonld,.json,.nt" required />
        </label>
        <div class="field-row">
          <div class="field">
            <label for="import-id">本体 ID</label>
            <input class="input mono" id="import-id" name="ontology_id" value="regulation" required />
          </div>
          <div class="field">
            <label for="import-version">版本</label>
            <input class="input mono" id="import-version" name="version" value="0.1.0" required />
          </div>
        </div>
        <div class="field">
          <label for="import-title">名称</label>
          <input class="input" id="import-title" name="title" placeholder="默认使用文件名" />
        </div>
        <div class="field">
          <label for="import-base-iri">基础 IRI</label>
          <input class="input mono" id="import-base-iri" name="base_iri" value="https://example.org/regulation/" required />
        </div>
        <div class="field">
          <label for="import-description">说明</label>
          <textarea class="textarea" id="import-description" name="description"></textarea>
        </div>
      </form>
    `,
    `
      <button class="button" data-action="close-modal">取消</button>
      <button class="button primary" type="submit" form="import-form"><i data-lucide="file-input"></i>导入</button>
    `,
  );
}

function openAISettingsModal() {
  const ai = state.aiStatus || {};
  const apiKeyConfigured = Boolean(ai.api_key_configured);
  openModal(
    "AI 抽取配置",
    `
      <form class="form-grid" id="ai-settings-form">
        <input type="hidden" name="provider" value="openai-compatible" />
        <label class="checkbox ai-enable-toggle">
          <input id="ai-enabled" name="enabled" type="checkbox" ${ai.enabled ? "checked" : ""} />
          启用 AI 抽取
        </label>
        <div class="field">
          <label for="ai-base-url">服务地址</label>
          <input
            class="input mono"
            id="ai-base-url"
            name="base_url"
            type="url"
            value="${escapeHtml(ai.base_url || "")}"
            placeholder="http://localhost:11434/v1"
          />
          <small class="field-hint">兼容 OpenAI /chat/completions 的服务地址，可填写基础地址或完整接口地址。</small>
        </div>
        <div class="field">
          <label for="ai-model">模型</label>
          <input
            class="input mono"
            id="ai-model"
            name="model"
            value="${escapeHtml(ai.model || "")}"
            placeholder="模型名称"
          />
        </div>
        <div class="field">
          <label for="ai-extraction-strategy">抽取策略</label>
          <select class="select" id="ai-extraction-strategy" name="extraction_strategy">
            <option value="spires" ${ai.extraction_strategy !== "single_pass" ? "selected" : ""}>SPIRES 两阶段（推荐）</option>
            <option value="single_pass" ${ai.extraction_strategy === "single_pass" ? "selected" : ""}>单轮 JSON 抽取</option>
          </select>
          <small class="field-hint">两阶段先识别并对齐实体，再抽取关系；单轮模式兼容原有接口和提示词。</small>
        </div>
        <div class="field">
          <label for="ai-api-key">API Key</label>
          <input
            class="input"
            id="ai-api-key"
            name="api_key"
            type="password"
            autocomplete="new-password"
            placeholder="${apiKeyConfigured ? "已配置，留空保持不变" : "无鉴权服务可留空"}"
          />
          <small class="field-hint">密钥仅保存在当前服务进程，不写入工作区文件，服务重启后需要重新填写。</small>
        </div>
        ${
          apiKeyConfigured
            ? `
              <label class="checkbox">
                <input id="ai-clear-api-key" name="clear_api_key" type="checkbox" />
                清除当前进程中的密钥
              </label>
            `
            : ""
        }
        <div
          class="connection-test"
          id="ai-connection-test"
          role="status"
          aria-live="polite"
          hidden
        ></div>
        <div class="field-row">
          <div class="field">
            <label for="ai-batch-size">批大小</label>
            <input class="input" id="ai-batch-size" name="batch_size" type="number" min="1" max="50" step="1" value="${escapeHtml(ai.batch_size ?? 8)}" />
          </div>
          <div class="field">
            <label for="ai-concurrency">并发数</label>
            <input class="input" id="ai-concurrency" name="concurrency" type="number" min="1" max="8" step="1" value="${escapeHtml(ai.concurrency ?? 2)}" />
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="ai-timeout">超时（秒）</label>
            <input class="input" id="ai-timeout" name="timeout_seconds" type="number" min="1" max="600" step="1" value="${escapeHtml(ai.timeout_seconds ?? 60)}" />
          </div>
          <div class="field">
            <label for="ai-max-retries">最大重试</label>
            <input class="input" id="ai-max-retries" name="max_retries" type="number" min="0" max="5" step="1" value="${escapeHtml(ai.max_retries ?? 2)}" />
          </div>
        </div>
      </form>
    `,
    `
      <button class="button ai-test-button" type="button" data-action="test-ai-settings">
        <i data-lucide="plug-zap"></i>测试连接
      </button>
      <button class="button" data-action="close-modal">取消</button>
      <button class="button primary" type="submit" form="ai-settings-form">
        <i data-lucide="save"></i>保存配置
      </button>
    `,
  );
}

function openGraphDBModal() {
  const status = state.graphdb || {};
  const repositories = status.repositories || [];
  openModal(
    "GraphDB 连接",
    `
      <div class="meta-grid">
        <div class="meta-item"><span>状态</span><strong>${status.connected ? "已连接" : "未连接"}</strong></div>
        <div class="meta-item"><span>地址</span><strong class="mono">${escapeHtml(status.base_url || "-")}</strong></div>
        <div class="meta-item"><span>目标仓库</span><strong class="mono">${escapeHtml(status.repository || "-")}</strong></div>
        <div class="meta-item"><span>仓库状态</span><strong>${status.repository_exists ? "已存在" : "不存在"}</strong></div>
      </div>
      ${
        repositories.length
          ? `<div class="version-list"><div class="version-row header"><span>仓库</span><span>标题</span><span>类型</span><span></span></div>${repositories
              .map(
                (item) => `<div class="version-row"><span class="mono">${escapeHtml(item.id)}</span><span>${escapeHtml(item.title || "-")}</span><span>${escapeHtml(item.type || "-")}</span><span></span></div>`,
              )
              .join("")}</div>`
          : `<div class="empty"><div><i data-lucide="database-zap"></i><strong>没有可用仓库</strong><span>${escapeHtml(status.error || "连接后显示仓库列表")}</span></div></div>`
      }
    `,
    `
      <button class="button" data-action="close-modal">关闭</button>
      <button class="button primary" data-action="refresh-graphdb"><i data-lucide="refresh-cw"></i>重新检查</button>
    `,
  );
}

function selectedFactIds() {
  return [...document.querySelectorAll(".fact-check:checked")].map((item) => item.value);
}

async function reviewFacts(payload) {
  if (!state.selectedRunId) return;
  const button = document.querySelector('[data-action="approve-selected"], [data-action="reject-selected"], [data-action="approve-all"]');
  if (button) button.disabled = true;
  try {
    const result = await api(`/api/runs/${encodeURIComponent(state.selectedRunId)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast(`审核完成：${result.review.approved} 条批准，${result.review.rejected} 条拒绝`);
    await Promise.all([loadDashboard(), loadRuns()]);
    renderRunsLayout();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

async function runQuery() {
  if (!state.selectedOntology) return;
  const editor = document.querySelector("#query-editor");
  const target = document.querySelector("#query-result");
  if (!editor || !target) return;
  target.innerHTML = `<div class="loading">执行查询</div>`;
  try {
    const result = await api("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ontology_id: state.selectedOntology.metadata.ontology_id,
        version: state.selectedOntology.metadata.version,
        query: editor.value,
        source: state.querySource,
      }),
    });
    if (result.type === "SELECT") {
      target.innerHTML = `
        <div class="result-summary"><span>${formatNumber(result.count)} 行</span><span>${escapeHtml(state.querySource)}</span></div>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr>${result.columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("")}</tr></thead>
            <tbody>${result.rows
              .map(
                (row) => `<tr>${result.columns.map((column) => `<td class="mono">${escapeHtml(row[column])}</td>`).join("")}</tr>`,
              )
              .join("")}</tbody>
          </table>
        </div>
      `;
    } else if (result.type === "ASK") {
      target.innerHTML = `<div class="result-summary"><span>ASK</span><span>${result.boolean}</span></div><pre class="result-pre">${escapeHtml(result.boolean)}</pre>`;
    } else {
      const data = result.data || JSON.stringify(result, null, 2);
      target.innerHTML = `<div class="result-summary"><span>${escapeHtml(result.type || "RDF")}</span><span>${formatNumber(result.count)}</span></div><pre class="result-pre">${escapeHtml(data)}</pre>`;
    }
    refreshIcons();
  } catch (error) {
    target.innerHTML = `<div class="result-placeholder">${escapeHtml(error.message)}</div>`;
  }
}

async function submitCreate(form) {
  const data = new FormData(form);
  try {
    const info = await api("/api/ontologies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(data.entries())),
    });
    closeModal();
    state.selectedOntologyId = info.ontology_id;
    state.selectedVersion = info.version;
    toast(`已创建 ${info.ontology_id}/${info.version}`);
    await loadOntologiesPage();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function submitImport(form) {
  try {
    const info = await api("/api/ontologies/import", {
      method: "POST",
      body: new FormData(form),
    });
    closeModal();
    state.selectedOntologyId = info.ontology_id;
    state.selectedVersion = info.version;
    toast(`已导入 ${info.ontology_id}/${info.version}`);
    await loadOntologiesPage();
  } catch (error) {
    toast(error.message, "error");
  }
}

function updateBuildAIPresentation() {
  const status = document.querySelector("#ai-settings-status");
  if (status) status.outerHTML = renderAISettingsStatus();
  const aiReady = Boolean(state.aiStatus?.ready);
  document
    .querySelectorAll('#build-extraction-mode option[value="ai"], #build-extraction-mode option[value="hybrid"]')
    .forEach((option) => {
      option.disabled = !aiReady;
    });
  const mode = document.querySelector("#build-extraction-mode");
  const autoApprove = document.querySelector("#build-auto-approve");
  if (mode && !aiReady && mode.value !== "rule") mode.value = "rule";
  if (autoApprove) {
    autoApprove.disabled = mode?.value !== "rule";
    if (autoApprove.disabled) autoApprove.checked = false;
  }
  refreshIcons();
}

function readAISettingsForm(form) {
  const data = new FormData(form);
  return {
    enabled: form.querySelector("#ai-enabled").checked,
    provider: "openai-compatible",
    base_url: String(data.get("base_url") || "").trim(),
    model: String(data.get("model") || "").trim(),
    api_key: String(data.get("api_key") || "").trim() || null,
    clear_api_key: Boolean(form.querySelector("#ai-clear-api-key")?.checked),
    batch_size: Number(data.get("batch_size")),
    concurrency: Number(data.get("concurrency")),
    timeout_seconds: Number(data.get("timeout_seconds")),
    max_retries: Number(data.get("max_retries")),
    extraction_strategy: String(data.get("extraction_strategy") || "spires"),
  };
}

function setAIConnectionFeedback(kind, title = "", detail = "") {
  const container = document.querySelector("#ai-connection-test");
  if (!container) return;
  if (!kind) {
    container.hidden = true;
    container.innerHTML = "";
    return;
  }
  const dotClass =
    kind === "success" ? "is-online" : kind === "error" ? "is-offline" : "is-idle";
  container.hidden = false;
  container.className = `connection-test is-${kind}`;
  container.innerHTML = `
    <span class="status-dot ${dotClass}"></span>
    <span class="connection-test-copy">
      <strong>${escapeHtml(title)}</strong>
      <small>${escapeHtml(detail)}</small>
    </span>
  `;
}

async function testAISettings(button) {
  const form = document.querySelector("#ai-settings-form");
  if (!form) return;
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `<i data-lucide="loader-circle"></i>测试中`;
  setAIConnectionFeedback("testing", "正在测试连接", "向模型发送最小请求，请稍候。");
  refreshIcons();
  try {
    const settings = readAISettingsForm(form);
    const result = await api("/api/ai/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: settings.provider,
        base_url: settings.base_url,
        model: settings.model,
        api_key: settings.api_key,
        clear_api_key: settings.clear_api_key,
        timeout_seconds: settings.timeout_seconds,
      }),
    });
    if (result.ok) {
      const detail = [
        result.model,
        `${formatNumber(result.elapsed_ms)} ms`,
        result.response,
      ]
        .filter(Boolean)
        .join(" · ");
      setAIConnectionFeedback("success", result.message, detail);
    } else {
      setAIConnectionFeedback("error", "连接失败", result.message);
    }
  } catch (error) {
    setAIConnectionFeedback("error", "连接失败", error.message);
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.innerHTML = original;
      refreshIcons();
    }
  }
}

async function submitAISettings(form) {
  const submit = form.closest(".modal")?.querySelector('button[type="submit"]');
  if (submit) submit.disabled = true;
  try {
    const result = await api("/api/ai/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readAISettingsForm(form)),
    });
    state.aiStatus = result;
    closeModal();
    updateBuildAIPresentation();
    toast(result.ready ? "AI 配置已保存并生效" : "AI 配置已保存，但尚未就绪");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (submit) submit.disabled = false;
  }
}

async function submitBuild(form) {
  const fileInput = form.querySelector("#build-files");
  if (!fileInput.files.length) {
    toast("请选择至少一个文件", "error");
    return;
  }
  const submit = form.querySelector("#build-submit");
  submit.disabled = true;
  submit.innerHTML = `<i data-lucide="loader-circle"></i>提交中`;
  refreshIcons();
  try {
    const result = await api("/api/builds", {
      method: "POST",
      body: new FormData(form),
    });
    setActiveBuild(result.run_id, {
      summary: result.summary,
      logs: [],
    });
    updateBuildTaskSurface();
    startBuildPolling(result.run_id);
    toast(`任务已提交：${result.run_id}`);
    state.selectedRunId = result.run_id;
  } catch (error) {
    toast(error.message, "error");
  } finally {
    submit.disabled = false;
    submit.innerHTML = `<i data-lucide="wand-sparkles"></i>开始构建`;
    refreshIcons();
  }
}

async function handleAction(action, target) {
  if (action === "open-create") openCreateModal();
  if (action === "open-import") openImportModal();
  if (action === "open-ai-settings") openAISettingsModal();
  if (action === "test-ai-settings") await testAISettings(target);
  if (action === "close-modal") closeModal();
  if (action === "refresh-graphdb") {
    await loadGraphDB(true);
    openGraphDBModal();
  }
  if (action === "refresh-ontologies") await loadOntologiesPage();
  if (action === "refresh-runs") await loadRunsPage();
  if (action === "go-build") await navigate("builds");
  if (action === "dismiss-build-task") {
    if (isActiveStatus(state.activeBuild?.summary?.status)) {
      if (!window.confirm("任务仍在后台执行。隐藏后可在“运行记录”继续查看，确定隐藏吗？")) {
        return;
      }
    }
    stopBuildPolling();
    setActiveBuild(null);
    state.activeBuild = null;
    updateBuildTaskSurface();
  }
  if (action === "cancel-build") {
    if (!state.activeBuildRunId) return;
    try {
      const payload = await api(
        `/api/runs/${encodeURIComponent(state.activeBuildRunId)}/cancel`,
        { method: "POST" },
      );
      state.activeBuild = payload;
      updateBuildTaskSurface();
      startBuildPolling(state.activeBuildRunId);
      toast("已请求取消构建任务");
    } catch (error) {
      toast(error.message, "error");
    }
  }
  if (action === "open-run") {
    state.selectedRunId = target.dataset.runId;
    await navigate("runs");
  }
  if (action === "select-ontology" || action === "select-version") {
    state.selectedOntologyId = target.dataset.id;
    state.selectedVersion = target.dataset.version;
    state.activeTab = "overview";
    await loadOntologiesPage();
  }
  if (action === "tab") {
    state.activeTab = target.dataset.tab;
    viewRoot.innerHTML = `
      ${renderStats()}
      <div class="workspace-grid">
        <section class="panel">
          <div class="panel-header">
            <div><h2>本体目录</h2><p>${state.ontologies.length} 个本体</p></div>
            <button class="icon-button" data-action="refresh-ontologies" title="刷新"><i data-lucide="refresh-cw"></i></button>
          </div>
          <div class="asset-list">${renderOntologyList()}</div>
        </section>
        ${renderOntologyDetail()}
      </div>
    `;
    refreshIcons();
  }
  if (action === "save-turtle") {
    const editor = document.querySelector("#turtle-editor");
    try {
      const info = await api(
        `/api/ontologies/${encodeURIComponent(state.selectedOntologyId)}/versions/${encodeURIComponent(state.selectedVersion)}/turtle`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: editor.value }),
        },
      );
      state.selectedOntology.metadata = info;
      toast(`已保存，${formatNumber(info.triple_count)} 个三元组`);
      await loadOntologiesPage();
    } catch (error) {
      toast(error.message, "error");
    }
  }
  if (action === "publish") {
    if (!state.selectedOntology) return;
    target.disabled = true;
    try {
      const result = await api(
        `/api/ontologies/${encodeURIComponent(state.selectedOntology.metadata.ontology_id)}/versions/${encodeURIComponent(state.selectedOntology.metadata.version)}/publish`,
        { method: "POST" },
      );
      toast(`已发布到 ${result.repository}`);
      await Promise.all([loadDashboard(), loadOntologies()]);
      viewRoot.innerHTML = `
        ${renderStats()}
        <div class="workspace-grid">
          <section class="panel"><div class="panel-header"><div><h2>本体目录</h2><p>${state.ontologies.length} 个本体</p></div></div><div class="asset-list">${renderOntologyList()}</div></section>
          ${renderOntologyDetail()}
        </div>
      `;
      refreshIcons();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      target.disabled = false;
    }
  }
  if (action === "delete-version") {
    if (!state.selectedOntology) return;
    if (!window.confirm("删除当前草稿版本？")) return;
    try {
      await api(
        `/api/ontologies/${encodeURIComponent(state.selectedOntology.metadata.ontology_id)}/versions/${encodeURIComponent(state.selectedOntology.metadata.version)}`,
        { method: "DELETE" },
      );
      toast("版本已删除");
      state.selectedOntology = null;
      await loadOntologiesPage();
    } catch (error) {
      toast(error.message, "error");
    }
  }
  if (action === "query-source") {
    state.querySource = target.dataset.source;
    const editor = document.querySelector("#query-editor");
    const query = editor?.value;
    state.activeTab = "query";
    const body = renderQuery();
    document.querySelector(".detail-body").innerHTML = body;
    if (query) document.querySelector("#query-editor").value = query;
    refreshIcons();
  }
  if (action === "run-query") await runQuery();
  if (action === "select-run") {
    try {
      await loadRunDetail(target.dataset.runId);
      renderRunsLayout();
      startRunPolling();
    } catch (error) {
      toast(error.message, "error");
    }
  }
  if (action === "cancel-run") {
    if (!state.selectedRunId) return;
    try {
      const payload = await api(
        `/api/runs/${encodeURIComponent(state.selectedRunId)}/cancel`,
        { method: "POST" },
      );
      state.selectedRun = {
        ...(state.selectedRun || {}),
        summary: payload.summary,
        logs: payload.logs || [],
      };
      renderRunsLayout();
      startRunPolling();
      toast("已请求取消运行任务");
    } catch (error) {
      toast(error.message, "error");
    }
  }
  if (action === "retry-run") {
    if (!state.selectedRunId) return;
    try {
      const result = await api(
        `/api/runs/${encodeURIComponent(state.selectedRunId)}/retry`,
        { method: "POST" },
      );
      state.selectedRunId = result.run_id;
      setActiveBuild(result.run_id, {
        summary: result.summary,
        logs: [],
      });
      toast(`已创建重跑任务：${result.run_id}`);
      await loadRunsPage();
    } catch (error) {
      toast(error.message, "error");
    }
  }
  if (action === "delete-run") {
    if (!state.selectedRunId) return;
    const runId = state.selectedRunId;
    if (!window.confirm(`删除运行记录 ${runId}？此操作不会删除已发布的本体版本或 GraphDB 数据。`)) {
      return;
    }
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}`, { method: "DELETE" });
      if (state.activeBuildRunId === runId) {
        stopBuildPolling();
        setActiveBuild(null);
        state.activeBuild = null;
      }
      state.selectedRunId = null;
      state.selectedRun = null;
      state.facts = [];
      toast("运行记录已删除");
      await loadRunsPage();
    } catch (error) {
      toast(error.message, "error");
    }
  }
  if (action === "approve-selected" || action === "reject-selected") {
    const ids = selectedFactIds();
    if (!ids.length) {
      toast("请选择待处理事实", "error");
      return;
    }
    await reviewFacts(
      action === "approve-selected" ? { approve: ids, reject: [] } : { approve: [], reject: ids },
    );
  }
  if (action === "approve-all") {
    if (!window.confirm("批准当前运行的全部事实？")) return;
    await reviewFacts({ approve_all: true, approve: [], reject: [] });
  }
  if (action === "register-run") {
    if (!state.selectedRunId) return;
    try {
      const info = await api(`/api/runs/${encodeURIComponent(state.selectedRunId)}/register`, { method: "POST" });
      toast(`已登记 ${info.ontology_id}/${info.version}`);
      state.selectedOntologyId = info.ontology_id;
      state.selectedVersion = info.version;
      await navigate("ontologies");
    } catch (error) {
      toast(error.message, "error");
    }
  }
}

document.addEventListener("click", async (event) => {
  const nav = event.target.closest(".nav-item[data-page]");
  if (nav) {
    event.preventDefault();
    await navigate(nav.dataset.page);
    return;
  }
  const modalAction = event.target.closest("[data-stop-modal] [data-action]");
  if (event.target.closest("[data-stop-modal]") && !modalAction) {
    return;
  }
  const target = modalAction || event.target.closest("[data-action]");
  if (!target) return;
  event.preventDefault();
  try {
    await handleAction(target.dataset.action, target);
  } catch (error) {
    toast(error.message, "error");
  }
});

document.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  if (form.id === "create-form") await submitCreate(form);
  if (form.id === "import-form") await submitImport(form);
  if (form.id === "ai-settings-form") await submitAISettings(form);
  if (form.id === "build-form") await submitBuild(form);
});

document.addEventListener("change", (event) => {
  if (event.target.id === "build-files") {
    const list = document.querySelector("#build-file-list");
    list.innerHTML = [...event.target.files]
      .map(
        (file) => `<span class="file-chip"><i data-lucide="file"></i><span>${escapeHtml(file.name)}</span></span>`,
      )
      .join("");
    refreshIcons();
  }
  if (event.target.id === "fact-status") {
    state.factStatus = event.target.value;
    loadRunDetail(state.selectedRunId).then(() => {
      renderRunsLayout();
      startRunPolling();
    });
  }
  if (event.target.id === "build-extraction-mode") {
    const autoApprove = document.querySelector("#build-auto-approve");
    const ruleMode = event.target.value === "rule";
    if (autoApprove) {
      autoApprove.disabled = !ruleMode;
      if (!ruleMode) autoApprove.checked = false;
    }
  }
});

let searchTimer = null;
document.addEventListener("input", (event) => {
  if (event.target.closest("#ai-settings-form")) {
    setAIConnectionFeedback(null);
  }
  if (event.target.id === "fact-search") {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(async () => {
      state.factSearch = event.target.value;
      await loadRunDetail(state.selectedRunId);
      renderRunsLayout();
      startRunPolling();
    }, 280);
  }
});

document.querySelector("#graphdb-status").addEventListener("click", async () => {
  await loadGraphDB();
  openGraphDBModal();
});

async function initialize() {
  setPageHeader("ontologies");
  await Promise.all([loadGraphDB(), navigate("ontologies")]);
}

initialize();
