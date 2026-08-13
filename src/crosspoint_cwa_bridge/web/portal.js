"use strict";

const byId = (id) => document.getElementById(id);

function bytes(value) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function duration(seconds) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

async function loadStatus() {
  const message = byId("status-message");
  message.textContent = "Refreshing local status…";
  message.classList.remove("error");
  try {
    const response = await fetch("/api/status", {cache: "no-store"});
    if (!response.ok) throw new Error("status unavailable");
    const status = await response.json();
    byId("bridge-state").textContent = "Online";
    byId("bridge-version").textContent = `Version ${status.version}`;
    byId("footer-version").textContent = `v${status.version}`;
    byId("cwa-state").textContent = status.cwa.state === "reachable" ? "Reachable" : status.cwa.state === "unreachable" ? "Unavailable" : "Not checked";
    byId("cwa-checked").textContent = status.cwa.checked_at ? `Checked ${new Date(status.cwa.checked_at).toLocaleTimeString()}` : "Awaiting first check";
    byId("uptime").textContent = duration(status.uptime_seconds);
    const x3 = status.cache.x3;
    const x4 = status.cache.x4;
    const entries = x3.entries + x4.entries;
    const output = x3.output_bytes + x4.output_bytes;
    byId("cache-total").textContent = `${entries} edition${entries === 1 ? "" : "s"}`;
    byId("cache-size").textContent = `${bytes(output)} stored outside Calibre`;
    byId("x3-cache").textContent = `${x3.entries} cached · ${bytes(x3.output_bytes)}`;
    byId("x4-cache").textContent = `${x4.entries} cached · ${bytes(x4.output_bytes)}`;
    byId("opds-url").textContent = status.opds_url;
    byId("admin-link").href = status.admin_url;
    message.textContent = status.admin_available ? "Bridge and administration are ready." : "Bridge is ready; administration still needs its separate password setup.";
  } catch (_) {
    message.textContent = "The status snapshot could not be loaded. OPDS may still be available.";
    message.classList.add("error");
    byId("bridge-state").textContent = "Unknown";
  }
}

byId("refresh-status")?.addEventListener("click", loadStatus);
byId("copy-opds")?.addEventListener("click", async (event) => {
  const value = byId("opds-url").textContent;
  try {
    await navigator.clipboard.writeText(value);
    event.currentTarget.textContent = "Copied";
    window.setTimeout(() => { event.currentTarget.textContent = "Copy address"; }, 1600);
  } catch (_) {
    window.prompt("Copy this OPDS address", value);
  }
});

loadStatus();
