const taskDialog = document.querySelector("#task-dialog");
const taskForm = document.querySelector("#task-form");
const tokenKey = "zju-seat-access-token";
const directSubmitLabel = "发现空位后直接预约";
let tasks = [];
let submissionEnabled = false;
const LOCATION_OPTIONS = {
  "主馆": {
    "一层": [],
    "二层": ["二层南", "二层北"],
    "三层": ["三层东", "三层南", "三层北"],
    "四层": ["四层东", "四层南", "四层西", "四层北"],
    "五层": ["五层东"],
  },
  "基础馆": {
    "负一层": ["负一层书库"],
    "一层": ["一层书库"],
    "二层": ["二层书库"],
    "三层": ["301信息共享空间"],
  },
  "农医馆": {
    "一层": ["112李摩西阅览室"],
    "二层": ["207中文图书阅览室", "209中文图书阅览室", "211中文图书阅览室"],
    "三层": ["320外文图书阅览室", "322中外文现刊阅览室"],
  },
  "玉泉馆": {
    "三层": [],
    "四层": [],
  },
};
const locationSelection = {
  venues: new Set(),
  floors: new Set(),
  areas: new Set(),
};

function locationKey(...parts) {
  return JSON.stringify(parts);
}

function pickerMode(level) {
  return document.querySelector(`#${level}-mode`).value;
}

function trimToSingle(selection) {
  const first = selection.values().next().value;
  selection.clear();
  if (first !== undefined) selection.add(first);
}

function renderOptionChips(containerId, options, selected, level) {
  const container = document.querySelector(containerId);
  if (!options.length) {
    container.innerHTML = "<p class='empty picker-empty'>暂无已收集分区</p>";
    return;
  }
  container.innerHTML = options.map(option => `
    <label class="option-chip">
      <input type="checkbox" data-level="${level}" data-key="${escapeHtml(option.key)}"
        ${selected.has(option.key) ? "checked" : ""}>
      <span>${escapeHtml(option.label)}</span>
    </label>
  `).join("");
  container.querySelectorAll("input").forEach(input => {
    input.onchange = () => updateLocationSelection(level, input.dataset.key, input.checked);
  });
}

function renderLocationPickers() {
  const venueOptions = Object.keys(LOCATION_OPTIONS).map(venue => ({
    key: venue,
    label: venue,
  }));
  const floorOptions = [];
  for (const venue of locationSelection.venues) {
    for (const floor of Object.keys(LOCATION_OPTIONS[venue] || {})) {
      floorOptions.push({
        key: locationKey(venue, floor),
        label: locationSelection.venues.size > 1 ? `${venue} / ${floor}` : floor,
      });
    }
  }
  const validFloors = new Set(floorOptions.map(option => option.key));
  locationSelection.floors = new Set(
    [...locationSelection.floors].filter(key => validFloors.has(key))
  );

  const areaOptions = [];
  for (const floorKey of locationSelection.floors) {
    const [venue, floor] = JSON.parse(floorKey);
    for (const area of LOCATION_OPTIONS[venue][floor]) {
      areaOptions.push({
        key: locationKey(venue, floor, area),
        label: locationSelection.floors.size > 1 ? `${venue} / ${floor} / ${area}` : area,
      });
    }
  }
  const validAreas = new Set(areaOptions.map(option => option.key));
  locationSelection.areas = new Set(
    [...locationSelection.areas].filter(key => validAreas.has(key))
  );

  renderOptionChips("#venue-options", venueOptions, locationSelection.venues, "venue");
  renderOptionChips("#floor-options", floorOptions, locationSelection.floors, "floor");
  renderOptionChips("#area-options", areaOptions, locationSelection.areas, "area");
}

function updateLocationSelection(level, key, checked) {
  const selection = {
    venue: locationSelection.venues,
    floor: locationSelection.floors,
    area: locationSelection.areas,
  }[level];
  if (checked && pickerMode(level) === "single") selection.clear();
  if (checked) selection.add(key);
  else selection.delete(key);
  renderLocationPickers();
}

function resetLocationSelection() {
  document.querySelector("#venue-mode").value = "single";
  document.querySelector("#floor-mode").value = "single";
  document.querySelector("#area-mode").value = "single";
  locationSelection.venues = new Set(["主馆"]);
  locationSelection.floors = new Set([locationKey("主馆", "三层")]);
  locationSelection.areas = new Set([
    locationKey("主馆", "三层", "三层东"),
  ]);
  renderLocationPickers();
}

function setLocationSelection(venue, floor, area) {
  document.querySelector("#venue-mode").value = "single";
  document.querySelector("#floor-mode").value = "single";
  document.querySelector("#area-mode").value = "single";
  locationSelection.venues = new Set([venue]);
  locationSelection.floors = new Set([locationKey(venue, floor)]);
  locationSelection.areas = new Set([locationKey(venue, floor, area)]);
  renderLocationPickers();
}

function locationTargets() {
  return [...locationSelection.areas].map(key => {
    const [venue, floor, area] = JSON.parse(key);
    return {venue, floor, area};
  });
}

function headers(json = false) {
  const token = localStorage.getItem(tokenKey) || "";
  const result = {};
  if (token) result.Authorization = `Bearer ${token}`;
  if (json) result["Content-Type"] = "application/json";
  return result;
}

async function api(path, options = {}) {
  const request = () => fetch(path, {
    ...options,
    headers: {...headers(Boolean(options.body)), ...(options.headers || {})},
  });
  let response = await request();
  if (response.status === 401) {
    const token = prompt("请输入控制台访问令牌");
    if (token) {
      localStorage.setItem(tokenKey, token);
      response = await request();
    }
  }
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2800);
}

async function refreshDashboard() {
  try {
    const data = await api("/api/dashboard");
    tasks = data.tasks;
    submissionEnabled = data.submission_enabled;
    document.querySelector("#account-status").textContent = statusText(data.account_status);
    document.querySelector("#submission-status").textContent = data.submission_enabled ? "已启用" : "关闭";
    document.querySelector("#submission-status").classList.toggle("warning-text", data.submission_enabled);
    document.querySelector("#submission-enabled").checked = data.submission_enabled;
    document.querySelector("#task-count").textContent = data.tasks.length;
    document.querySelector("#reservation-count").textContent = data.reservations.length;
    document.querySelector("#tasks").innerHTML = data.tasks.length
      ? data.tasks.map(taskCard).join("")
      : "<p class='empty'>尚未创建任务，点击“新建任务”开始。</p>";
  } catch (error) {
    toast(error.message);
  }
}

function formatDetectionTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? escapeHtml(String(value))
    : date.toLocaleString("zh-CN", {hour12: false});
}

function detectionResultText(result) {
  return {
    success: "预约成功",
    succeeded: "预约成功",
    no_seat: "未找到符合规则的空闲座位",
    candidate_found: "已找到候选座位",
    already_reserved: "账号已有预约",
    login_required: "需要重新连接浙大账号",
    ambiguous: "预约结果待人工核验",
    failure: "检测失败",
  }[result] || result || "检测中";
}

function progressStageText(stage) {
  return ({
    run_started: "开始检测",
    checking_login: "检查登录状态",
    login_required: "需要重新连接账号",
    scanning: "读取座位页面",
    scan_complete: "座位扫描完成",
    no_matching_seat: "没有符合规则的座位",
    candidate_found: "找到候选座位",
    submission_skipped: "观察模式，不提交预约",
    submitting: "正在提交预约",
    verifying: "正在核验预约结果",
    finished: "检测完成",
    failed: "检测异常",
  })[stage] || stage || "等待检测";
}

function eventDetailsText(details = {}) {
  const parts = [];
  if (details.available_count !== undefined) parts.push(`空余 ${details.available_count}`);
  if (details.seat !== undefined && details.seat !== null) parts.push(`座位 ${String(details.seat).padStart(3, "0")}`);
  if (details.observation_mode) parts.push("观察模式");
  if (details.submission_enabled === false) parts.push("总开关关闭");
  if (details.error) parts.push(details.error);
  return parts.join(" · ");
}

function seatListText(seats = []) {
  if (!seats.length) return "无";
  const shown = seats.slice(0, 12).map(seat => String(seat).padStart(3, "0"));
  const suffix = seats.length > shown.length ? ` 等 ${seats.length} 个` : "";
  return `${shown.join("、")}${suffix}`;
}

function taskCard(task) {
  const config = task.config;
  const mode = config.observation_mode ? "观察模式" : "允许提交";
  const policy = task.submission_policy || {};
  const detection = task.detection || {};
  const progress = task.progress || {};
  const seatStatus = progress.seat_status || {};
  const events = progress.events || [];
  const lastRun = detection.last_run;
  const activeStates = ["running", "submitting", "verifying"];
  const checking = activeStates.includes(task.state)
    && Boolean(lastRun && !lastRun.finished_at);
  const blockers = policy.blockers ? [...policy.blockers] : [];
  if (!policy.blockers && !submissionEnabled) blockers.push("自动提交总开关未开启");
  if (!policy.blockers && config.observation_mode) blockers.push("任务仍处于观察模式");
  if (new Date(config.stops_at) <= new Date()) blockers.push("自动检查停止时间已过");
  const submitNotice = policy.will_submit
    ? "本任务会点击立即预约"
    : "本任务不会点击立即预约";
  const detectionDetails = lastRun ? `
    <div class="detection-grid">
      <span>最近检测</span><strong>${formatDetectionTime(lastRun.started_at)}</strong>
      <span>检测结果</span><strong>${escapeHtml(detectionResultText(lastRun.result))}</strong>
      ${lastRun.seat_number !== null && lastRun.seat_number !== undefined
        ? `<span>候选座位</span><strong>${escapeHtml(String(lastRun.seat_number).padStart(3, "0"))}</strong>`
        : ""}
      ${lastRun.last_error
        ? `<span>检测错误</span><strong class="error">${escapeHtml(lastRun.last_error)}</strong>`
        : ""}
    </div>` : `<p class="empty">尚未执行检测</p>`;
  const currentProgress = progress.current_stage ? `
    <div class="progress-summary">
      <span>当前步骤</span>
      <strong>${escapeHtml(progressStageText(progress.current_stage))}</strong>
      <small>${escapeHtml(progress.current_message || "")}</small>
      <span>检测轮次</span>
      <strong>${Number(progress.scan_count || 0)}</strong>
      <span>当前空余</span>
      <strong>${seatStatus.available_count ?? "--"}</strong>
      <span>可用座位</span>
      <strong>${escapeHtml(seatListText(seatStatus.available_seats || []))}</strong>
      <span>候选座位</span>
      <strong>${seatStatus.candidate_seat ? escapeHtml(String(seatStatus.candidate_seat).padStart(3, "0")) : "--"}</strong>
    </div>` : "";
  const eventLog = events.length ? `
    <details class="event-log">
      <summary>查看最近过程</summary>
      <ol>
        ${events.slice(0, 8).map(event => {
          const details = eventDetailsText(event.details || {});
          return `<li>
            <time>${formatDetectionTime(event.created_at)}</time>
            <strong>${escapeHtml(progressStageText(event.stage))}</strong>
            <span>${escapeHtml(details || event.message || "")}</span>
          </li>`;
        }).join("")}
      </ol>
    </details>` : "";
  return `<article class="task">
    <div class="task-main">
      <div class="task-badges"><span class="badge">${statusText(task.state)}</span><span class="badge neutral">${mode}</span>${checking ? '<span class="badge checking"><i></i>检测中</span>' : ""}</div>
      <h3>${escapeHtml(config.name)}</h3>
      <p>${escapeHtml(config.venue)} / ${escapeHtml(config.floor)} / ${escapeHtml(config.area)}</p>
      <p>${config.reservation_date} · ${escapeHtml(config.time_slot)} · ${config.seat_rules.length} 条座位规则</p>
      <p class="${policy.will_submit ? "warning-text" : "hint"}">${submitNotice}</p>
      ${blockers.length ? `<p class="error">不会自动预约：${blockers.map(escapeHtml).join("；")}</p>` : ""}
      ${task.last_error ? `<p class="error">${escapeHtml(task.last_error)}</p>` : ""}
      <section class="detection-status">
        ${currentProgress}
        ${detectionDetails}
        <p class="next-check">下次检测：${formatDetectionTime(detection.next_check_at)}</p>
        ${eventLog}
      </section>
    </div>
    <div class="actions">
      <button onclick="taskAction('${task.id}','start')">启动</button>
      <button onclick="taskAction('${task.id}','stop')" class="secondary">停止</button>
      <button onclick="taskAction('${task.id}','run-once')" class="secondary">检查一次</button>
      <button onclick="editTask('${task.id}')" class="secondary">编辑</button>
      <button onclick="removeTask('${task.id}')" class="secondary">删除</button>
    </div>
  </article>`;
}

function addRule(rule = {}) {
  const fragment = document.querySelector("#seat-rule-template").content.cloneNode(true);
  const card = fragment.querySelector(".rule-card");
  for (const [field, value] of Object.entries(rule)) {
    const input = card.querySelector(`[data-field="${field}"]`);
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else if (Array.isArray(value)) input.value = value.join(", ");
    else if (value !== null && value !== undefined) input.value = value;
  }
  card.querySelector(".remove-rule").onclick = () => {
    card.remove();
    renumberRules();
  };
  document.querySelector("#seat-rules").append(card);
  renumberRules();
}

function renumberRules() {
  document.querySelectorAll(".rule-card").forEach((card, index) => {
    card.querySelector(".rule-number").textContent = index + 1;
  });
}

function collectRules() {
  return [...document.querySelectorAll(".rule-card")].map((card, index) => {
    const value = field => card.querySelector(`[data-field="${field}"]`).value;
    const any = card.querySelector('[data-field="accept_any"]').checked;
    return {
      priority: Number(value("priority") || index + 1),
      start: any ? null : optionalNumber(value("start")),
      end: any ? null : optionalNumber(value("end")),
      included: parseNumberList(value("included")),
      excluded: parseNumberList(value("excluded")),
      accept_any: any,
      order: value("order"),
    };
  });
}

function parseNumberList(value) {
  if (!value.trim()) return [];
  return [...new Set(value.split(/[\s,，;；]+/).filter(Boolean).map(item => {
    const number = Number(item);
    if (!Number.isInteger(number) || number <= 0) throw new Error(`无效座位号：${item}`);
    return number;
  }))];
}

function optionalNumber(value) {
  return value === "" ? null : Number(value);
}

function resetTaskForm() {
  taskForm.reset();
  taskForm.elements.id.value = "";
  resetLocationSelection();
  document.querySelector("#seat-rules").innerHTML = "";
  document.querySelector("#form-error").textContent = "";
  const now = new Date();
  taskForm.elements.reservation_date.value = localDate(now);
  taskForm.elements.starts_at.value = localDateTime(now);
  taskForm.elements.stops_at.value = localDateTime(new Date(now.getTime() + 15 * 60000));
  addRule({priority: 1, start: 1, end: 999, order: "asc"});
}

window.editTask = id => {
  const task = tasks.find(item => item.id === id);
  if (!task) return;
  resetTaskForm();
  const config = task.config;
  taskForm.elements.id.value = id;
  for (const field of ["name", "reservation_date", "refresh_min_seconds", "refresh_max_seconds", "max_consecutive_errors"]) {
    taskForm.elements[field].value = config[field];
  }
  setLocationSelection(config.venue, config.floor, config.area);
  const [useStart, useEnd] = config.time_slot.split("-");
  taskForm.elements.use_start.value = useStart;
  taskForm.elements.use_end.value = useEnd;
  taskForm.elements.starts_at.value = config.starts_at.slice(0, 16);
  taskForm.elements.stops_at.value = config.stops_at.slice(0, 16);
  for (const field of ["notify_success", "notify_timeout", "notify_error"]) {
    taskForm.elements[field].checked = Boolean(config[field]);
  }
  taskForm.elements.direct_submit.checked = !config.observation_mode;
  document.querySelector("#seat-rules").innerHTML = "";
  config.seat_rules.forEach(addRule);
  taskDialog.showModal();
};

window.taskAction = async (id, action) => {
  try {
    await api(`/api/tasks/${id}/${action}`, {method: "POST"});
    toast(action === "run-once" ? "已开始检查" : "任务状态已更新");
    await refreshDashboard();
  } catch (error) {
    toast(error.message);
  }
};

window.removeTask = async id => {
  if (!confirm("确定删除这个任务吗？")) return;
  try {
    await api(`/api/tasks/${id}`, {method: "DELETE"});
    await refreshDashboard();
  } catch (error) {
    toast(error.message);
  }
};

document.querySelector("#new-task").onclick = () => {
  resetTaskForm();
  taskDialog.showModal();
};
document.querySelector("#close-dialog").onclick = () => taskDialog.close();
document.querySelector("#add-rule").onclick = () => addRule({priority: document.querySelectorAll(".rule-card").length + 1});
for (const level of ["venue", "floor", "area"]) {
  document.querySelector(`#${level}-mode`).onchange = () => {
    const selection = {
      venue: locationSelection.venues,
      floor: locationSelection.floors,
      area: locationSelection.areas,
    }[level];
    if (pickerMode(level) === "single" && selection.size > 1) {
      trimToSingle(selection);
    }
    renderLocationPickers();
  };
}
document.querySelector("#login-button").onclick = async () => {
  try {
    await api("/api/account/login", {method: "POST"});
    toast("登录窗口正在打开，请手动完成认证");
  } catch (error) {
    toast(error.message);
  }
};
document.querySelector("#verify-login-button").onclick = async () => {
  try {
    await api("/api/account/verify", {method: "POST"});
    toast("已请求立即验证登录状态");
    setTimeout(refreshDashboard, 1200);
  } catch (error) {
    toast(error.message);
  }
};

taskForm.onsubmit = async event => {
  event.preventDefault();
  const errorNode = document.querySelector("#form-error");
  errorNode.textContent = "";
  try {
    const values = Object.fromEntries(new FormData(taskForm));
    const id = values.id;
    const targets = locationTargets();
    if (!targets.length) throw new Error("请至少选择一个已有分区");
    if (id && targets.length !== 1) {
      throw new Error("编辑已有任务时只能选择一个分区");
    }
    const commonPayload = {
      name: values.name,
      reservation_date: values.reservation_date,
      time_slot: `${values.use_start}-${values.use_end}`,
      starts_at: values.starts_at,
      stops_at: values.stops_at,
      refresh_min_seconds: Number(values.refresh_min_seconds),
      refresh_max_seconds: Number(values.refresh_max_seconds),
      max_consecutive_errors: Number(values.max_consecutive_errors),
      observation_mode: !taskForm.elements.direct_submit.checked,
      notify_success: taskForm.elements.notify_success.checked,
      notify_timeout: taskForm.elements.notify_timeout.checked,
      notify_error: taskForm.elements.notify_error.checked,
      seat_rules: collectRules(),
    };
    if (!commonPayload.seat_rules.length) throw new Error("至少添加一条座位规则");
    for (const target of targets) {
      const suffix = targets.length > 1
        ? `（${target.venue}/${target.floor}/${target.area}）`
        : "";
      const payload = {
        ...commonPayload,
        ...target,
        name: `${commonPayload.name}${suffix}`,
      };
      await api(id ? `/api/tasks/${id}` : "/api/tasks", {
        method: id ? "PUT" : "POST",
        body: JSON.stringify(payload),
      });
    }
    taskDialog.close();
    toast(targets.length > 1 ? `已创建 ${targets.length} 个任务` : "任务已保存");
    await refreshDashboard();
  } catch (error) {
    errorNode.textContent = error.message;
  }
};

document.querySelector("#save-system").onclick = async () => {
  const enabled = document.querySelector("#submission-enabled").checked;
  const confirmation = document.querySelector("#submission-confirmation").value;
  const errorNode = document.querySelector("#system-error");
  errorNode.textContent = "";
  try {
    await api("/api/settings/system", {
      method: "PUT",
      body: JSON.stringify({submission_enabled: enabled, confirmation}),
    });
    document.querySelector("#submission-confirmation").value = "";
    toast(enabled ? "自动提交总开关已开启" : "自动提交总开关已关闭");
    await refreshDashboard();
  } catch (error) {
    errorNode.textContent = error.message;
    await refreshDashboard();
  }
};

function localDate(date) {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

function localDateTime(date) {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function statusText(value) {
  return ({
    not_connected: "未连接",
    connected: "已连接",
    login_timeout: "登录超时",
    draft: "草稿",
    scheduled: "等待执行",
    waiting_login: "等待登录",
    running: "正在检查",
    submitting: "正在提交",
    verifying: "正在核验",
    succeeded: "预约成功",
    timed_out: "已超时",
    stopped: "已停止",
    failed: "失败",
  })[value] || value;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
}

refreshDashboard();
setInterval(refreshDashboard, 2000);
