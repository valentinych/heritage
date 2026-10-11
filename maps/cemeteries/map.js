(function () {
  const data = window.CEMETERY_MAP;
  const summary = document.getElementById("summary");
  const status = document.getElementById("status");
  const layerList = document.getElementById("layer-list");
  const resultList = document.getElementById("result-list");
  const resultsHint = document.getElementById("results-hint");
  const queryInput = document.getElementById("q");
  const panel = document.getElementById("panel");
  const panelToggle = document.getElementById("panel-toggle");

  if (!data || !data.points) {
    status.textContent = "Не ўдалося прачытаць кропкі мапы.";
    return;
  }

  document.getElementById("map-title").textContent = "Могілкі";
  summary.textContent = data.points.length.toLocaleString("be") + " месцаў";

  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const map = L.map("map", {
    zoomAnimation: !reduced,
    fadeAnimation: !reduced,
    markerZoomAnimation: !reduced,
    zoomControl: true,
  });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  map.zoomControl.setPosition("topright");

  const enabled = new Set(data.layers.filter((layer) => layer.on !== false).map((layer) => layer.id));
  const records = [];
  let lastTrigger = null;

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  function linkify(value) {
    const re = /https?:\/\/[^\s]+/g;
    let html = "";
    let last = 0;
    for (const match of String(value).matchAll(re)) {
      html += escapeHtml(value.slice(last, match.index));
      let url = match[0].replace(/[),.;]+$/, "");
      const trail = match[0].slice(url.length);
      html += '<a href="' + escapeHtml(url) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(url) + "</a>";
      html += escapeHtml(trail);
      last = match.index + match[0].length;
    }
    html += escapeHtml(value.slice(last));
    return html;
  }

  function fold(value) {
    return String(value || "")
      .toLocaleLowerCase("ru")
      .replace(/ё/g, "е")
      .replace(/і/g, "и")
      .replace(/ў/g, "у")
      .replace(/\s+/g, " ")
      .trim();
  }

  function popupHtml(record) {
    const title = record.name || "Без назвы";
    let html = '<p class="popup-title">' + escapeHtml(title) + "</p>";
    html += '<p class="popup-layer">' + escapeHtml(record.layers.map((layer) => layer.name).join(" · ")) + "</p>";
    if (record.note) {
      html += '<p class="popup-note">' + linkify(record.note) + "</p>";
    }
    html += '<p class="popup-coords">Каардынаты ' + record.lat.toFixed(6) + ", " + record.lon.toFixed(6) + "</p>";
    return html;
  }

  function pinIcon(color) {
    return L.divIcon({
      className: "pin-wrap",
      html: '<span class="pin" style="background:' + color + '"></span>',
      iconSize: [28, 28],
      iconAnchor: [14, 26],
      popupAnchor: [0, -24],
    });
  }

  function markerIcon(layer) {
    const color = layer ? layer.color : "#37474F";
    if (layer && layer.marker === "pin") {
      return pinIcon(color);
    }
    return dotIcon(color);
  }

  function dotIcon(color) {
    return L.divIcon({
      className: "dot-wrap",
      html: '<span class="dot" style="background:' + color + '"></span>',
      iconSize: [32, 32],
      iconAnchor: [16, 16],
      popupAnchor: [0, -12],
    });
  }

  function clusterIcon(color) {
    return function (cluster) {
      return L.divIcon({
        className: "cluster-wrap",
        html: '<span class="cluster-blob" style="background:' + color + '">' + cluster.getChildCount() + "</span>",
        iconSize: [44, 44],
        iconAnchor: [22, 22],
      });
    };
  }

  const cluster = L.markerClusterGroup({
    maxClusterRadius: 40,
    disableClusteringAtZoom: 16,
    spiderfyOnMaxZoom: true,
    showCoverageOnHover: false,
    removeOutsideVisibleBounds: true,
    iconCreateFunction: (group) => {
      const colors = new Set(group.getAllChildMarkers().map((marker) => marker.options.color));
      const color = colors.size === 1 ? colors.values().next().value : "#37474F";
      return clusterIcon(color)(group);
    },
  });
  map.addLayer(cluster);

  const uyezdRing = (data.uyezd && data.uyezd.ring ? data.uyezd.ring : []).map(([lon, lat]) => [lat, lon]);
  const povet = data.layers.find((layer) => layer.id === "navahrudak");
  map.createPane("uyezd");
  map.getPane("uyezd").style.zIndex = 350;
  const outline = L.polygon(uyezdRing, {
    pane: "uyezd",
    color: povet ? povet.color : "#9A3412",
    weight: 2,
    fillColor: povet ? povet.color : "#9A3412",
    fillOpacity: 0.08,
    interactive: false,
  });

  data.points.forEach((row) => {
    const indexes = Array.isArray(row[0]) ? row[0] : [row[0]];
    const layers = indexes.map((index) => data.layers[index]).filter(Boolean);
    const record = {
      layers: layers,
      name: row[1],
      lon: row[2],
      lat: row[3],
      note: row[4] || "",
      search: fold(layers.map((layer) => layer.name).join(" ") + " " + (row[1] || "") + " " + (row[4] || "")),
    };
    const marker = L.marker([record.lat, record.lon], {
      icon: markerIcon(layers[0]),
      keyboard: false,
      title: record.name || (layers[0] ? layers[0].name : ""),
      color: layers[0] ? layers[0].color : "#37474F",
    });
    marker.bindPopup(popupHtml(record), { maxWidth: 320 });
    record.marker = marker;
    records.push(record);
  });

  const layerOrder = data.layers.slice().sort((a, b) => {
    const rank = (layer) => (layer.id === "navahrudak" ? 0 : layer.id === "indexed" ? 1 : 2);
    return rank(a) - rank(b) || data.layers.indexOf(a) - data.layers.indexOf(b);
  });
  layerOrder.forEach((layer) => {
    const count = records.filter((record) => record.layers.some((item) => item.id === layer.id)).length;
    const label = document.createElement("label");
    label.className = "layer-row";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = enabled.has(layer.id);
    input.dataset.layer = layer.id;
    const swatch = document.createElement("span");
    swatch.className = layer.marker === "pin" ? "swatch swatch-pin" : "swatch";
    swatch.style.background = layer.color;
    swatch.setAttribute("aria-hidden", "true");
    const name = document.createElement("span");
    name.className = "layer-name";
    name.textContent = layer.name;
    const countNode = document.createElement("span");
    countNode.className = "layer-count";
    countNode.textContent = String(count);
    label.append(input, swatch, name, countNode);
    layerList.append(label);
    input.addEventListener("change", () => setLayer(layer.id, input.checked));
  });

  function preferredLayer(record) {
    const active = record.layers.filter((layer) => enabled.has(layer.id));
    return active.find((layer) => layer.marker === "pin")
      || active.find((layer) => layer.id === "navahrudak")
      || active[0]
      || null;
  }

  function applyVisibility() {
    const markers = [];
    records.forEach((record) => {
      const layer = preferredLayer(record);
      if (!layer) {
        return;
      }
      record.marker.options.color = layer.color;
      record.marker.setIcon(markerIcon(layer));
      markers.push(record.marker);
    });
    cluster.clearLayers();
    cluster.addLayers(markers);
    if (enabled.has("navahrudak") && uyezdRing.length) {
      if (!map.hasLayer(outline)) {
        outline.addTo(map);
      }
    } else if (map.hasLayer(outline)) {
      map.removeLayer(outline);
    }
    reportVisible();
  }

  function setLayer(id, on) {
    const input = layerList.querySelector('input[data-layer="' + id + '"]');
    if (input) {
      input.checked = on;
    }
    if (on) {
      enabled.add(id);
    } else {
      enabled.delete(id);
    }
    applyVisibility();
  }

  function setAll(on) {
    data.layers.forEach((layer) => {
      const input = layerList.querySelector('input[data-layer="' + layer.id + '"]');
      if (input) {
        input.checked = on;
      }
      if (on) {
        enabled.add(layer.id);
      } else {
        enabled.delete(layer.id);
      }
    });
    applyVisibility();
  }

  function reportVisible() {
    const visible = records.reduce((sum, record) => sum + (preferredLayer(record) ? 1 : 0), 0);
    if (visible === 0) {
      status.textContent = "Усе слаі схаваныя.";
      return;
    }
    status.textContent = "На мапе " + visible.toLocaleString("be") + " месцаў.";
  }

  applyVisibility();
  const onlyHome = [...enabled].every((id) => id === "navahrudak" || id === "indexed");
  if (uyezdRing.length && enabled.has("navahrudak") && onlyHome) {
    map.fitBounds(outline.getBounds(), { padding: [24, 24] });
  } else {
    const visible = records.filter((record) => preferredLayer(record));
    if (visible.length) {
      map.fitBounds(L.latLngBounds(visible.map((record) => [record.lat, record.lon])), { padding: [24, 24] });
    }
  }

  document.getElementById("show-all").addEventListener("click", () => setAll(true));
  document.getElementById("hide-all").addEventListener("click", () => setAll(false));

  function openRecord(record, trigger) {
    lastTrigger = trigger || null;
    if (!preferredLayer(record)) {
      const layer = record.layers.find((item) => item.id === "indexed")
        || record.layers.find((item) => item.id === "navahrudak")
        || record.layers[0];
      if (layer) {
        setLayer(layer.id, true);
      }
    }
    cluster.zoomToShowLayer(record.marker, () => {
      record.marker.openPopup();
    });
    if (window.matchMedia("(max-width: 800px)").matches) {
      setPanelOpen(false);
    }
  }

  map.on("popupopen", (event) => {
    const close = event.popup.getElement()?.querySelector(".leaflet-popup-close-button");
    if (close) {
      close.focus();
    }
  });
  map.on("popupclose", () => {
    if (lastTrigger && document.contains(lastTrigger)) {
      lastTrigger.focus();
    }
    lastTrigger = null;
  });

  let searchTimer = 0;
  function runSearch() {
    const query = fold(queryInput.value);
    resultList.replaceChildren();
    if (query.length < 2) {
      resultsHint.hidden = false;
      resultsHint.textContent = "Увядзіце прынамсі 2 літары назвы.";
      reportVisible();
      return;
    }
    const hits = [];
    for (const record of records) {
      if (record.search.includes(query)) {
        hits.push(record);
      }
    }
    resultsHint.hidden = false;
    if (!hits.length) {
      resultsHint.textContent = "Нічога не знойдзена.";
      status.textContent = "Нічога не знойдзена.";
      return;
    }
    const shown = hits.slice(0, 30);
    resultsHint.textContent = hits.length > shown.length
      ? "Паказаны першыя 30 з " + hits.length.toLocaleString("be") + ". Удакладніце запыт."
      : "Знойдзена " + hits.length.toLocaleString("be") + ".";
    status.textContent = resultsHint.textContent;
    for (const record of shown) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "result";
      const name = document.createElement("span");
      name.className = "result-name";
      name.textContent = record.name || "Без назвы";
      const layer = document.createElement("span");
      layer.className = "result-layer";
      layer.textContent = record.layers.map((item) => item.name).join(" · ");
      button.append(name, layer);
      button.addEventListener("click", () => openRecord(record, button));
      item.append(button);
      resultList.append(item);
    }
  }

  queryInput.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(runSearch, 150);
  });
  document.getElementById("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    window.clearTimeout(searchTimer);
    runSearch();
    const first = resultList.querySelector("button");
    if (first) {
      first.focus();
    }
  });

  function setPanelOpen(open) {
    panel.classList.toggle("is-open", open);
    panelToggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  panelToggle.addEventListener("click", () => {
    setPanelOpen(!panel.classList.contains("is-open"));
  });

  reportVisible();
})();
