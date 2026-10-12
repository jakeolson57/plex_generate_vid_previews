const $ = (id) => document.getElementById(id);
const esc = (x) =>
  String(x ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const number = (x) => Number(x || 0).toLocaleString();
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (response.redirected && response.url.includes("/login")) {
    location.href = "/login";
    throw Error("Sign in to continue.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw Error(
      typeof data.detail === "string"
        ? data.detail
        : data.message || "Request failed. Please try again.",
    );
  return data;
}
function demo(message) {
  $("toast").textContent = message;
  $("toast").style.display = "block";
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(
    () => ($("toast").style.display = "none"),
    5000,
  );
}
function elapsed(value) {
  if (!value) return "never";
  const seconds = Math.max(
    0,
    (Date.now() -
      new Date(
        value.endsWith("Z") || value.includes("+")
          ? value
          : value.replace(" ", "T") + "Z",
      )) /
      1000,
  );
  return seconds < 60
    ? "just now"
    : seconds < 3600
      ? Math.floor(seconds / 60) + " min ago"
      : seconds < 86400
        ? Math.floor(seconds / 3600) + " hr ago"
        : Math.floor(seconds / 86400) + " days ago";
}
function confirmWork(title, message, work) {
  return new Promise((resolve) => {
    const dialog = $("app-dialog");
    $("dialog-body").innerHTML =
      `<div class="eyebrow">Confirm action</div><h2>${esc(title)}</h2><p>${esc(message)}</p><div class="dialog-actions"><button id="cancel-action">Cancel</button><button class="primary" id="confirm-action">Confirm</button></div>`;
    dialog.showModal();
    $("cancel-action").onclick = () => {
      dialog.close();
      resolve(false);
    };
    dialog.oncancel = () => resolve(false);
    $("confirm-action").onclick = async () => {
      const btn = $("confirm-action");
      btn.disabled = true;
      try {
        await work();
        dialog.close();
        resolve(true);
      } catch (error) {
        demo(error.message);
        btn.disabled = false;
      }
    };
  });
}
