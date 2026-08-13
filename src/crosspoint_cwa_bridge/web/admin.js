"use strict";

const byId = (id) => document.getElementById(id);
let csrfToken = "";

function bytes(value) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function announce(text, error = false) {
  const target = byId("admin-message") || byId("login-message");
  if (!target) return;
  target.textContent = text;
  target.classList.toggle("error", error);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body) headers.set("Content-Type", "application/json");
  if (csrfToken && !["GET", "HEAD"].includes(options.method || "GET")) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(path, {...options, headers, cache: "no-store"});
  if (response.status === 401 && path !== "/admin/api/login") {
    window.location.assign("/admin/login");
    throw new Error("authentication required");
  }
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) throw new Error(payload.error || `request failed (${response.status})`);
  return payload;
}

const loginForm = byId("login-form");
if (loginForm) {
  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    announce("Signing in…");
    const form = new FormData(loginForm);
    try {
      const result = await api("/admin/api/login", {
        method: "POST",
        body: JSON.stringify({username: form.get("username"), password: form.get("password")}),
      });
      window.location.assign(result.redirect);
    } catch (error) {
      const messages = {
        admin_not_configured: "Administration has not been configured. Run the SSH password setup first.",
        invalid_credentials: "The username or password is incorrect.",
        try_again_later: "Too many attempts. Please wait 15 minutes.",
      };
      announce(messages[error.message] || "Sign-in failed.", true);
    }
  });
}

function setSettings(values) {
  const form = byId("settings-form");
  if (!form) return;
  Object.entries(values).forEach(([name, value]) => {
    const input = form.elements.namedItem(name);
    if (input) input.value = value;
  });
}

function renderDiff(active, pending) {
  const target = byId("settings-diff");
  const badge = byId("pending-badge");
  if (!target || !badge) return;
  if (!pending) {
    target.textContent = "Saved values become active only after an explicit bridge restart.";
    badge.hidden = true;
    return;
  }
  const changes = Object.keys(active).filter((key) => active[key] !== pending[key]);
  badge.hidden = changes.length === 0;
  target.textContent = changes.length ? `Pending: ${changes.map((key) => `${key.replaceAll("_", " ")} ${active[key]} → ${pending[key]}`).join(" · ")}` : "Pending values match the active configuration.";
}

function renderActivity(events) {
  const list = byId("activity-list");
  list.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("p");
    empty.textContent = "No sanitized activity has been recorded yet.";
    empty.className = "activity-item";
    list.append(empty);
    return;
  }
  events.forEach((event) => {
    const row = document.createElement("div");
    row.className = "activity-item";
    const when = document.createElement("time");
    when.dateTime = event.occurred_at;
    when.textContent = new Date(event.occurred_at).toLocaleString();
    const name = document.createElement("strong");
    name.textContent = event.event.replaceAll("_", " ");
    const detail = document.createElement("span");
    const parts = [];
    if (event.profile) parts.push(String(event.profile).toUpperCase());
    if (event.duration_ms !== undefined) parts.push(`${event.duration_ms} ms`);
    if (event.output_bytes !== undefined) parts.push(`${bytes(event.output_bytes)} output`);
    if (event.savings_percent !== undefined) parts.push(`${event.savings_percent}% saved`);
    if (event.image_count !== undefined) parts.push(`${event.image_count} images`);
    detail.textContent = parts.join(" · ") || `HTTP ${event.status || "event"}`;
    row.append(when, name, detail);
    list.append(row);
  });
}

async function loadAdmin() {
  if (!byId("admin-main")) return;
  announce("Refreshing bridge status…");
  try {
    const [status, activity] = await Promise.all([
      api("/admin/api/status"),
      api("/admin/api/activity?limit=100"),
    ]);
    csrfToken = status.csrf_token;
    byId("admin-bridge-state").textContent = "Online";
    byId("admin-version").textContent = `v${status.version} · ${status.uptime_seconds}s uptime`;
    byId("admin-cwa-state").textContent = status.upstream.state === "reachable" ? "Reachable" : status.upstream.state === "unreachable" ? "Unavailable" : "Not checked";
    byId("admin-cwa-detail").textContent = status.upstream.latency_ms === null ? "Awaiting contact" : `${status.upstream.status || "No response"} · ${status.upstream.latency_ms} ms`;
    byId("conversion-state").textContent = status.conversion.active ? `${status.conversion.profile.toUpperCase()} active` : "Idle";
    byId("conversion-detail").textContent = status.conversion.active ? `${status.conversion.duration_seconds}s elapsed` : "One conversion at a time";
    byId("storage-free").textContent = bytes(status.storage.free_bytes);
    byId("storage-total").textContent = `${bytes(status.storage.total_bytes)} total`;
    ["x3", "x4"].forEach((profile) => {
      const value = status.cache[profile];
      byId(`admin-${profile}-cache`).textContent = `${value.entries} edition${value.entries === 1 ? "" : "s"} · ${bytes(value.output_bytes)}${value.invalid_entries ? ` · ${value.invalid_entries} invalid` : ""}`;
    });
    setSettings(status.pending_settings || status.active_settings);
    renderDiff(status.active_settings, status.pending_settings);
    renderActivity(activity.events);
    announce(status.settings_error ? "The bridge is online, but a saved settings file needs attention." : "Console is current.", Boolean(status.settings_error));
  } catch (error) {
    if (error.message !== "authentication required") announce("The console could not refresh.", true);
  }
}

byId("refresh-admin")?.addEventListener("click", loadAdmin);
byId("logout")?.addEventListener("click", async () => {
  try { await api("/admin/api/logout", {method: "POST"}); } finally { window.location.assign("/admin/login"); }
});
byId("settings-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const values = {
    optimizer_jpeg_quality: Number(form.get("optimizer_jpeg_quality")),
    feed_max_bytes: Number(form.get("feed_max_bytes")),
    optimizer_max_image_pixels: Number(form.get("optimizer_max_image_pixels")),
    optimizer_max_epub_bytes: Number(form.get("optimizer_max_epub_bytes")),
    connect_timeout_seconds: Number(form.get("connect_timeout_seconds")),
    read_timeout_seconds: Number(form.get("read_timeout_seconds")),
  };
  try {
    const result = await api("/admin/api/settings", {method: "PUT", body: JSON.stringify(values)});
    const count = Object.keys(result.changes).length;
    announce(count ? `${count} pending change${count === 1 ? "" : "s"} saved. Review, then restart when ready.` : "Settings saved; no restart-changing differences found.");
    await loadAdmin();
  } catch (_) { announce("Settings were not saved. Check every allowed range.", true); }
});
document.querySelectorAll(".purge-cache").forEach((button) => {
  button.addEventListener("click", async () => {
    const scope = button.dataset.scope;
    const label = scope === "all" ? "all X3 and X4 derivatives" : `all ${scope.toUpperCase()} derivatives`;
    if (!window.confirm(`Clear ${label}? Source books will not be touched.`)) return;
    try {
      const result = await api("/admin/api/cache/purge", {method: "POST", body: JSON.stringify({scope, confirmation: `clear-${scope}`})});
      announce(`Cleared ${result.entries} cached edition${result.entries === 1 ? "" : "s"}.`);
      await loadAdmin();
    } catch (_) { announce("Cache cleanup could not be completed.", true); }
  });
});
byId("run-diagnostics")?.addEventListener("click", async () => {
  announce("Running read-only diagnostics…");
  try {
    const result = await api("/admin/api/diagnostics", {method: "POST", body: "{}"});
    const invalid = result.cache.x3.invalid_entries + result.cache.x4.invalid_entries;
    announce(`Diagnostics complete: CWA ${result.upstream.state}; cache ${invalid ? `${invalid} invalid entries` : "valid"}; TLS ${result.tls.configured ? "ready" : "unavailable"}.`, Boolean(invalid));
  } catch (_) { announce("Diagnostics could not complete.", true); }
});
byId("restart-bridge")?.addEventListener("click", async () => {
  if (!window.confirm("Restart only the CrossPoint bridge now? Active administration sessions will end.")) return;
  try {
    await api("/admin/api/restart", {method: "POST", body: "{}"});
    announce("Bridge is restarting. This page will reconnect when HTTPS returns.");
    const retry = window.setInterval(async () => {
      try {
        const response = await fetch("/admin/api/health", {cache: "no-store"});
        if (response.ok) { window.clearInterval(retry); window.location.assign("/admin/login"); }
      } catch (_) { /* restart still in progress */ }
    }, 1500);
  } catch (_) { announce("The bridge restart could not be scheduled.", true); }
});
byId("password-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await api("/admin/api/password", {method: "POST", body: JSON.stringify({current_password: form.get("current_password"), new_password: form.get("new_password")})});
    window.location.assign("/admin/login");
  } catch (error) {
    announce(error.message === "current_password_incorrect" ? "The current password is incorrect." : "The new password must contain at least 12 characters.", true);
  }
});

loadAdmin();
