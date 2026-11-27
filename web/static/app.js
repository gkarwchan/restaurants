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
let placeRequestId = 0;

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
    $("loading").hidden = true;
    return;
  }
  const params = new URLSearchParams({
    key: mapsKey, v: "weekly", libraries: "marker", loading: "async", callback: "initMap",
  });
  const s = document.createElement("script");
  s.src = "https://maps.googleapis.com/maps/api/js?" + params;
  s.async = true;
  s.onerror = () => {
    $("map").innerHTML = '<div class="notice">Google Maps failed to load.</div>';
    $("loading").hidden = true;
  };
  document.head.appendChild(s);
}

window.initMap = async function () {
  const { Map } = await google.maps.importLibrary("maps");
  const { AdvancedMarkerElement } = await google.maps.importLibrary("marker");
  map = new Map($("map"), {
    center: CALGARY, zoom: 10, mapId: window.APP_CONFIG.mapId,
    mapTypeControl: false, streetViewControl: false, clickableIcons: false,
    zoomControl: true,
  });
  const locateButton = document.createElement("button");
  locateButton.type = "button";
  locateButton.className = "locate-control";
  locateButton.setAttribute("aria-label", "Find my location");
  locateButton.title = "Find my location";
  locateButton.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><circle cx="12" cy="12" r="7" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="12" r="2" fill="currentColor"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3" stroke="currentColor" stroke-width="2"/></svg>';
  locateButton.addEventListener("click", () => {
    const message = $("map-message");
    message.hidden = true;
    if (!navigator.geolocation) {
      message.textContent = "Location is not available in this browser.";
      message.hidden = false;
      return;
    }

    locateButton.disabled = true;
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        map.panTo({ lat: coords.latitude, lng: coords.longitude });
        map.setZoom(15);
        locateButton.disabled = false;
      },
      (error) => {
        message.textContent = error.code === error.PERMISSION_DENIED
          ? "Location access was denied. Allow it in your browser and try again."
          : error.code === error.TIMEOUT
            ? "Location request timed out. Please try again."
            : "Could not determine your location. Please try again.";
        message.hidden = false;
        locateButton.disabled = false;
      },
      { enableHighAccuracy: true, timeout: 10000 },
    );
  });
  map.controls[google.maps.ControlPosition.RIGHT_TOP].push(locateButton);

  let data;
  try {
    const response = await fetch("/api/facilities");
    if (!response.ok) throw new Error(`Facilities request failed (${response.status})`);
    data = await response.json();
  } catch (error) {
    console.error("Could not load facility data:", error);
    $("map").innerHTML = '<div class="notice">Facility data could not be loaded. Please reload the page.</div>';
    $("loading").hidden = true;
    return;
  }
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
    if (f.hasPlaceInfo) dot.classList.add("has-place-info");
    const title = f.hasPlaceInfo ? `${f.name} — Google Places details available` : f.name;
    const m = new AdvancedMarkerElement({ position: { lat: f.lat, lng: f.lng }, content: dot, title });
    m.addListener("click", () => openFacility(f, m));
    m.facility = f;
    return m;
  }).filter(Boolean);

  const clusterRenderer = {
    render({ count, position }, _stats, rendererMap) {
      const svg = `<svg fill="#0000ff" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240" width="50" height="50">
<circle cx="120" cy="120" opacity=".6" r="70" />
<circle cx="120" cy="120" opacity=".3" r="90" />
<circle cx="120" cy="120" opacity=".2" r="110" />
<text x="50%" y="50%" style="fill:#fff" text-anchor="middle" font-size="50" dominant-baseline="middle" font-family="roboto,arial,sans-serif">${count}</text>
</svg>`;
      const content = new DOMParser().parseFromString(svg, "image/svg+xml").documentElement;
      content.setAttribute("transform", "translate(0 25)");
      return new AdvancedMarkerElement({
        map: rendererMap,
        position,
        content,
        title: `Cluster of ${count} facilities`,
        zIndex: Number(google.maps.Marker.MAX_ZINDEX) + count,
      });
    },
  };
  clusterer = new markerClusterer.MarkerClusterer({ map, markers: [], renderer: clusterRenderer });
  applyFilters();
  $("loading").hidden = true;
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
  $("place-dialog-close").addEventListener("click", () => $("place-dialog").close());
  $("place-dialog").addEventListener("close", () => { placeRequestId++; });
  $("panel-body").addEventListener("click", (e) => {
    const link = e.target.closest(".place-info-link");
    if (!link) return;
    e.preventDefault();
    openPlaceInfo(link.dataset.facilityId);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("place-dialog").open) closePanel();
  });
  $("legend").innerHTML = Object.values(STATUS).map((s) =>
    `<div><span class="dot" style="background:${s.color}"></span>${esc(s.label)}</div>`).join("")
    + '<div><span class="dot places-legend-dot has-place-info"></span>Google Places details available</div>';
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
  if ($("place-dialog").open) $("place-dialog").close();
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

async function openPlaceInfo(facilityId) {
  const dialog = $("place-dialog");
  const body = $("place-dialog-body");
  const requestId = ++placeRequestId;
  body.innerHTML = '<h2 id="place-dialog-title">Google Places</h2><div class="meta">Loading details…</div>';
  dialog.showModal();
  try {
    const response = await fetch(`/api/facilities/${encodeURIComponent(facilityId)}/place`);
    const place = await response.json();
    if (requestId !== placeRequestId || !dialog.open) return;
    if (!response.ok) throw new Error(place.detail || "Could not load Google Places details.");
    body.innerHTML = renderPlace(place);
  } catch (error) {
    if (requestId === placeRequestId && dialog.open) {
      body.innerHTML = `<h2 id="place-dialog-title">Google Places</h2><p class="empty">${esc(error.message)}</p>`;
    }
  }
}

function closePanel() {
  selectedId = null;
  $("panel").hidden = true;
  if ($("place-dialog").open) $("place-dialog").close();
}

function renderFacility(d) {
  const totalV = d.inspections.reduce((n, i) => n + i.violations.length, 0);
  const totalC = d.inspections.reduce((n, i) => n + i.violations.filter((v) => v.critical === "Yes").length, 0);
  let html = `
    <h2>${esc(d.name)}</h2>
    <div class="meta">${esc(d.facility_group)} · ${esc(d.address)}${d.community ? " · " + esc(d.community) : ""}</div>
    <a href="${esc(d.detail_url)}" target="_blank" rel="noopener">View on the AHS website ↗</a>
    ${d.place_id ? ` · <a class="place-info-link" href="#" data-facility-id="${esc(d.id)}">More info</a>` : ""}`;

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

function renderPlace(place) {
  const name = place.displayName?.text || "Google Place";
  const rating = place.rating
    ? `<div class="place-rating"><b>${esc(place.rating)} ★</b>${place.userRatingCount ? ` · ${Number(place.userRatingCount).toLocaleString()} reviews` : ""}</div>`
    : "";
  const photoAttributions = (place.photo_attributions || []).map((attribution) =>
    attribution.uri
      ? `<a href="${esc(attribution.uri)}" target="_blank" rel="noopener">${esc(attribution.displayName)}</a>`
      : esc(attribution.displayName)).filter(Boolean).join(", ");
  const photo = place.photo_url
    ? `<img class="place-photo" src="${esc(place.photo_url)}" alt="Photo of ${esc(name)}">
       ${photoAttributions ? `<div class="photo-attribution">Photo: ${photoAttributions}</div>` : ""}`
    : "";
  const types = (place.types || []).map((type) => esc(type.replaceAll("_", " "))).join(", ");
  const website = place.websiteUri
    ? `<div><a href="${esc(place.websiteUri)}" target="_blank" rel="noopener">Website ↗</a></div>`
    : "";
  const phone = place.nationalPhoneNumber ? `<div>${esc(place.nationalPhoneNumber)}</div>` : "";
  const mapsLink = place.googleMapsUri
    ? `<a href="${esc(place.googleMapsUri)}" target="_blank" rel="noopener">Open in Google Maps ↗</a>`
    : "";
  return `
    <h2 id="place-dialog-title">${esc(name)}</h2>
    ${photo}
    <div class="place-details">
      ${place.formattedAddress ? `<div>${esc(place.formattedAddress)}</div>` : ""}
      ${rating}${phone}${website}
      ${types ? `<div class="meta">${types}</div>` : ""}
      ${mapsLink}
    </div>`;
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
