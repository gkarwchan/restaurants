"use strict";

const TYPES = ["Food", "Personal Services", "Aquatics", "Social Care"];
const STATUS = {
  critical:   { color: "#d93025", label: "Critical violation at last inspection" },
  violations: { color: "#e37400", label: "Violation at last inspection" },
  clean:      { color: "#188038", label: "No violations at last inspection" },
  none:       { color: "#80868b", label: "No inspection data" },
};
const CALGARY = { lat: 51.0447, lng: -114.0719 };

const state = { facilities: [], markers: [], types: new Set(TYPES), status: "all", query: "" };
let map, clusterer, selectedId = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function statusOf(f) {
  if (!f.inspections) return "none";
  if (f.latestCritical > 0) return "critical";
  if (f.latestViolations > 0) return "violations";
  return "clean";
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso + "T00:00:00");
  return isNaN(d) ? iso : d.toLocaleDateString("en-CA", { year: "numeric", month: "short", day: "numeric" });
}

// ---------------------------------------------------------------- boot
function loadGoogleMaps() {
  const { mapsKey } = window.APP_CONFIG || {};
  if (!mapsKey) {
    $("map").innerHTML = '<div class="notice">Set the <code>GOOGLE_MAPS_BROWSER_KEY</code> environment variable and restart the server to show the map.</div>';
    return;
  }
  const params = new URLSearchParams({
    key: mapsKey, v: "weekly", libraries: "marker", loading: "async", callback: "initMap",
  });
  const s = document.createElement("script");
  s.src = "https://maps.googleapis.com/maps/api/js?" + params;
  s.async = true;
  s.onerror = () => { $("map").innerHTML = '<div class="notice">Google Maps failed to load.</div>'; };
  document.head.appendChild(s);
}

window.initMap = async function () {
  const { Map } = await google.maps.importLibrary("maps");
  const { AdvancedMarkerElement } = await google.maps.importLibrary("marker");
  map = new Map($("map"), {
    center: CALGARY, zoom: 10, mapId: window.APP_CONFIG.mapId,
    mapTypeControl: false, streetViewControl: false, clickableIcons: false,
  });

  const data = await fetch("/api/facilities").then((r) => r.json());
  state.facilities = data.rows.map((row) => Object.fromEntries(data.fields.map((k, i) => [k, row[i]])));
  state.unmapped = data.unmapped;

  const dotTemplate = document.createElement("div");
  dotTemplate.className = "dot";
  state.markers = state.facilities.map((f) => {
    f.lat = Number(f.lat);
    f.lng = Number(f.lng);
    f.status = statusOf(f);
    if (!Number.isFinite(f.lat) || !Number.isFinite(f.lng)) return null;
    const dot = dotTemplate.cloneNode();
    dot.style.background = STATUS[f.status].color;
    const m = new AdvancedMarkerElement({ position: { lat: f.lat, lng: f.lng }, content: dot, title: f.name });
    m.addListener("click", () => openFacility(f, m));
    m.facility = f;
    return m;
  }).filter(Boolean);

  clusterer = new markerClusterer.MarkerClusterer({ map, markers: [] });
  applyFilters();
};

// ---------------------------------------------------------------- filters
function buildControls() {
  $("types").innerHTML = TYPES.map((t) =>
    `<button class="chip" type="button" aria-pressed="true" data-type="${esc(t)}">${esc(t)}</button>`).join("");
  $("types").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    const on = b.getAttribute("aria-pressed") !== "true";
    b.setAttribute("aria-pressed", on);
    on ? state.types.add(b.dataset.type) : state.types.delete(b.dataset.type);
    applyFilters();
  });
  $("status").addEventListener("change", (e) => { state.status = e.target.value; applyFilters(); });
  let t;
  $("search").addEventListener("input", (e) => {
    clearTimeout(t);
    t = setTimeout(() => { state.query = e.target.value.trim().toLowerCase(); applyFilters(); }, 250);
  });
  $("close").addEventListener("click", closePanel);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
  $("legend").innerHTML = Object.values(STATUS).map((s) =>
    `<div><span class="dot" style="background:${s.color}"></span>${esc(s.label)}</div>`).join("");
}

function matches(f) {
  if (!state.types.has(f.type)) return false;
  if (state.status === "critical" && f.status !== "critical") return false;
  if (state.status === "any" && !(f.status === "critical" || f.status === "violations")) return false;
  if (state.status === "clean" && f.status !== "clean") return false;
  if (state.query) {
    const hay = `${f.name} ${f.city || ""}`.toLowerCase();
    if (!hay.includes(state.query)) return false;
  }
  return true;
}

function applyFilters() {
  if (!clusterer) return;
  const visible = state.markers.filter((m) => matches(m.facility));
  clusterer.clearMarkers(true);
  clusterer.addMarkers(visible);
  let text = `${visible.length.toLocaleString()} of ${state.markers.length.toLocaleString()} facilities`;
  if (state.unmapped) text += ` · ${state.unmapped.toLocaleString()} without coordinates`;
  $("count").textContent = text;
}

// ---------------------------------------------------------------- details panel
async function openFacility(f, marker) {
  selectedId = f.id;
  $("panel").hidden = false;
  $("panel-body").innerHTML = `<h2>${esc(f.name)}</h2><div class="meta">Loading…</div>`;
  map.panTo(marker.position);
  const res = await fetch(`/api/facilities/${encodeURIComponent(f.id)}`);
  if (selectedId !== f.id) return; // another facility was clicked meanwhile
  if (!res.ok) {
    $("panel-body").innerHTML = `<h2>${esc(f.name)}</h2><div class="meta">Could not load details.</div>`;
    return;
  }
  $("panel-body").innerHTML = renderFacility(await res.json());
}

function closePanel() {
  selectedId = null;
  $("panel").hidden = true;
}

function renderFacility(d) {
  const totalV = d.inspections.reduce((n, i) => n + i.violations.length, 0);
  const totalC = d.inspections.reduce((n, i) => n + i.violations.filter((v) => v.critical === "Yes").length, 0);
  let html = `
    <h2>${esc(d.name)}</h2>
    <div class="meta">${esc(d.facility_group)} · ${esc(d.address)}${d.community ? " · " + esc(d.community) : ""}</div>
    <a href="${esc(d.detail_url)}" target="_blank" rel="noopener">View on the AHS website ↗</a>`;

  if (!d.inspections_fetched_at) {
    return html + `<p class="empty">Inspection details for this facility haven't been downloaded yet.</p>`;
  }
  html += `
    <div class="summary">
      <div><b>${d.inspections.length}</b><span>inspections</span></div>
      <div><b>${totalV}</b><span>violations</span></div>
      <div><b>${totalC}</b><span>critical</span></div>
    </div>`;
  if (!d.inspections.length) {
    return html + `<p class="empty">No inspections in the last 3 years.</p>`;
  }
  html += d.inspections.map((insp, idx) => {
    const crit = insp.violations.filter((v) => v.critical === "Yes").length;
    const badge = crit ? `<span class="badge critical">${crit} critical</span>`
      : insp.violations.length ? `<span class="badge violation">${insp.violations.length} violation${insp.violations.length > 1 ? "s" : ""}</span>`
      : `<span class="badge clean">No violations</span>`;
    const viols = insp.violations.length
      ? insp.violations.map(renderViolation).join("")
      : `<div class="empty">No violations noted.</div>`;
    return `
      <details class="insp" ${idx === 0 ? "open" : ""}>
        <summary>
          <span class="date">${esc(fmtDate(insp.inspection_date))}</span> ${badge}
          <div class="sub">${esc(insp.inspection_type)} · ${esc((insp.action_taken || "").replace(/\n/g, ", "))}</div>
        </summary>
        ${viols}
      </details>`;
  }).join("");
  return html;
}

function renderViolation(v) {
  return `
    <div class="viol">
      <div><span class="cat">${esc(v.category)}</span>
        ${v.critical === "Yes" ? '<span class="badge critical">Critical</span>' : ""}
        ${v.status ? `<span class="badge status">${esc(v.status)}</span>` : ""}</div>
      ${v.inspector_comment ? `<div class="text">${esc(v.inspector_comment)}</div>` : ""}
      ${v.comments ? `<details><summary>Regulation</summary>${esc(v.comments)}</details>` : ""}
    </div>`;
}

buildControls();
loadGoogleMaps();
