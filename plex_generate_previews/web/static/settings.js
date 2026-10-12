let original = null,
  draftBaseline = null,
  loaded = false;
const form = $("settings-form");
const names = [
  "gpu_threads",
  "cpu_threads",
  "sync_hours",
  "use_database_sync",
  "data_limit_gb_per_hour",
  "rate_limit_exempt_paths",
  "enable_multi_user_priority",
  "priority_history_limit",
  "mount_check_enabled",
  "mount_check_paths",
  "mount_failure_threshold",
];
const sections = [
  "connection",
  "processing",
  "priorities",
  "protection",
  "maintenance",
  "security",
];
const descriptions = {
  connection: [
    "Your library connection",
    "Connection details come from your existing server configuration. Credentials stay on the server.",
  ],
  processing: [
    "Tune your workload",
    "Start with one GPU worker. More concurrent jobs may increase media reads without improving throughput.",
  ],
  priorities: [
    "Process what matters first",
    "Watching next and recent watch history keep the preview queue aligned with what your viewers are likely to play.",
  ],
  protection: [
    "Keep storage checks on",
    "Mount validation helps distinguish unavailable storage from media that has been removed.",
  ],
  maintenance: [
    "Inspect before cleanup",
    "Use discovery and verification to review existing previews. Run a dry run before deleting orphaned bundles.",
  ],
  security: [
    "Administrator access",
    "Set a password for the preview generator. Connection tokens and passwords are never displayed in this preview.",
  ],
};
function setTab(tab, focus = false) {
  if (!sections.includes(tab)) tab = "processing";
  for (const name of sections) {
    $("panel-" + name).hidden = name !== tab;
    $("tab-" + name).setAttribute("aria-selected", String(name === tab));
    $("tab-" + name).tabIndex = name === tab ? 0 : -1;
  }
  $("help-title").textContent = descriptions[tab][0];
  $("help-text").textContent = descriptions[tab][1];
  history.replaceState(null, "", "/settings?tab=" + tab);
  if (focus) $("tab-" + tab).focus();
}
$("settings-form").addEventListener("input", markDirty);
$("settings-form").addEventListener("change", markDirty);
function draft() {
  const result = {};
  for (const name of names) {
    const field = $(name);
    result[name] = field.type === "checkbox" ? field.checked : field.value;
  }
  result.scope = form.elements.scope.value;
  result.new_password = $("new_password").value;
  result.confirm_password = $("confirm_password").value;
  return result;
}
function populate(values) {
  for (const name of names) {
    const field = $(name);
    if (field.type === "checkbox") field.checked = Boolean(values[name]);
    else field.value = values[name] ?? "";
  }
  form.elements.scope.value = values.scope;
  $("new_password").value = "";
  $("confirm_password").value = "";
}
function markDirty() {
  if (!loaded) return;
  const v = draft();
  const dirty = JSON.stringify(v) !== JSON.stringify(draftBaseline);
  $("save-state").textContent = dirty ? "Unsaved changes" : "All changes saved";
  $("save-dot").style.background = dirty ? "var(--accent)" : "var(--green)";
  $("save").disabled = !dirty;
  $("discard").disabled = !dirty;
  $("context-workers").textContent =
    `${v.gpu_threads} GPU · ${v.cpu_threads} CPU`;
  $("context-scope").textContent =
    v.scope === "priority" ? "Priority only" : "Full backlog";
  $("context-budget").textContent =
    Number(v.data_limit_gb_per_hour) > 0
      ? v.data_limit_gb_per_hour + " GB / hr"
      : "Unlimited";
  $("budget-label").textContent =
    Number(v.data_limit_gb_per_hour) > 0 ? "Limited" : "Unlimited";
}
function discardChanges() {
  populate(original);
  draftBaseline = draft();
  markDirty();
  demo("Draft discarded. Saved settings restored.");
}
async function saveSettings(e) {
  e.preventDefault();
  for (const input of form.querySelectorAll("input[type=number]")) {
    input.classList.remove("invalid");
    if (!input.checkValidity()) {
      setTab(input.closest(".tab-panel").id.replace("panel-", ""));
      input.classList.add("invalid");
      input.focus();
      demo(
        "Enter a valid value for " +
          input.labels[0].textContent.toLowerCase() +
          ".",
      );
      return;
    }
  }
  const password = $("new_password").value,
    confirm = $("confirm_password").value;
  if (password !== confirm || new TextEncoder().encode(password).length > 72) {
    setTab("security");
    $("new_password").focus();
    demo(
      password !== confirm
        ? "The two passwords must match."
        : "The password must be at most 72 bytes.",
    );
    return;
  }
  const value = draft();
  const payload = {
    gpu_threads: Number(value.gpu_threads),
    cpu_threads: Number(value.cpu_threads),
    sync_interval: Number(value.sync_hours) * 3600,
    use_database_sync: value.use_database_sync,
    full_backlog: value.scope === "all",
    data_limit_gb_per_hour: Number(value.data_limit_gb_per_hour),
    rate_limit_exempt_paths: value.rate_limit_exempt_paths,
    enable_multi_user_priority: value.enable_multi_user_priority,
    priority_history_limit: Number(value.priority_history_limit),
    mount_check_enabled: value.mount_check_enabled,
    mount_check_paths: value.mount_check_paths,
    mount_failure_threshold: Number(value.mount_failure_threshold),
    new_password: value.new_password,
  };
  const controls = [...form.querySelectorAll("input,textarea,button")];
  controls.forEach((c) => (c.disabled = true));
  try {
    const result = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    value.new_password = "";
    value.confirm_password = "";
    original = structuredClone(value);
    $("new_password").value = "";
    $("confirm_password").value = "";
    draftBaseline = draft();
    demo(result.message);
  } catch (error) {
    demo(error.message);
  } finally {
    controls.forEach((c) => (c.disabled = false));
    markDirty();
  }
}

const maintenanceMap = {
  "Discover existing previews": ["discover-existing-bifs", {}],
  "Scan all preview bundles": ["scan-all-bifs", {}],
  "Debug completed previews": ["verify-debug", null],
  "Verify completed items": ["verify-completed", {}],
  "Retry failed items": ["reset-failed", {}],
  "Preview orphan cleanup": ["cleanup-orphaned-bundles", { dry_run: true }],
};
let maintenanceBusy = false;
async function runMaintenance(endpoint, payload = {}) {
  if (maintenanceBusy) return;
  maintenanceBusy = true;
  const buttons = [...$("panel-maintenance").querySelectorAll("button")];
  buttons.forEach((b) => (b.disabled = true));
  $("maintenance-result").textContent = "Working…";
  try {
    const result = await api(
      "/api/settings/" + endpoint,
      payload === null ? {} : { method: "POST", body: JSON.stringify(payload) },
    );
    if (result.requires_confirmation) {
      $("maintenance-result").textContent = result.message;
      maintenanceBusy = false;
      await confirmWork("Confirm library changes", result.message, async () =>
        runMaintenance(endpoint, { ...payload, confirm: true }),
      );
      return;
    }
    $("maintenance-result").textContent =
      result.message || JSON.stringify(result, null, 2);
    if (result.sample_paths?.length)
      $("maintenance-result").textContent +=
        "\n\n" + result.sample_paths.join("\n");
  } catch (error) {
    $("maintenance-result").textContent = error.message;
    throw error;
  } finally {
    maintenanceBusy = false;
    buttons.forEach((b) => (b.disabled = false));
  }
}
async function maintenance(title) {
  try {
    const [endpoint, payload] = maintenanceMap[title];
    await runMaintenance(endpoint, payload);
  } catch (error) {
    demo(error.message);
  }
}
function confirmAction(title, message) {
  confirmWork(title, message, async () => {
    if (title === "Delete orphaned bundles")
      await runMaintenance("cleanup-orphaned-bundles", { dry_run: false });
    else await runMaintenance("reset-library", {});
  });
}
async function checkConnection() {
  const node = $("connection-result");
  node.textContent = "Checking library connection…";
  try {
    const data = await api("/api/settings/library-health");
    $("db-backend").textContent = data.backend || "Configured";
    node.textContent = data.healthy
      ? "Connected · " + data.latency_ms + " ms"
      : "Library connection unavailable.";
  } catch (error) {
    node.textContent = error.message;
  }
}
async function testMounts() {
  $("mount-result").textContent = "Validating mounts…";
  try {
    const data = await api("/api/settings/validate-mounts", { method: "POST" });
    $("mount-result").textContent =
      data.message +
      (data.missing_paths?.length ? "\n" + data.missing_paths.join("\n") : "");
  } catch (error) {
    $("mount-result").textContent = error.message;
  }
}
document.querySelector("[role=tablist]").addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  e.preventDefault();
  const current = sections.findIndex(
    (n) => $("tab-" + n).getAttribute("aria-selected") === "true",
  );
  const next =
    e.key === "Home"
      ? 0
      : e.key === "End"
        ? sections.length - 1
        : (current + (e.key === "ArrowRight" ? 1 : -1) + sections.length) %
          sections.length;
  setTab(sections[next], true);
});
setTab(new URLSearchParams(location.search).get("tab") || "processing");
async function load() {
  try {
    const [s, stats] = await Promise.all([
      api("/api/settings"),
      api("/api/stats"),
    ]);
    original = {
      ...s,
      sync_hours: s.sync_interval / 3600,
      scope: s.full_backlog ? "all" : "priority",
    };
    populate(original);
    draftBaseline = draft();
    loaded = true;
    $("server-name").textContent = s.plex_server_name || "Plex Media Server";
    $("server-url").textContent = s.plex_url || "Configured on server";
    $("context-server").textContent = s.plex_server_name || "Plex";
    $("last-sync").textContent = stats.last_sync_time
      ? new Date(stats.last_sync_time).toLocaleString([], {
          month: "short",
          day: "numeric",
          hour: "2-digit",
          minute: "2-digit",
        })
      : "Not yet synced";
    $("failed-count").textContent = stats.failed;
    $("loaded-at").textContent =
      "Current settings loaded · " +
      new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    markDirty();
  } catch (e) {
    $("save-state").textContent = "Could not load current settings";
    demo(e.message);
  }
}
load();
window.addEventListener("beforeunload", (event) => {
  if (loaded && JSON.stringify(draft()) !== JSON.stringify(draftBaseline)) {
    event.preventDefault();
    event.returnValue = "";
  }
});
