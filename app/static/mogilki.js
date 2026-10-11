(function () {
  const form = document.getElementById("search");
  const status = document.getElementById("status");
  const results = document.getElementById("results");
  const names = document.getElementById("cemetery-names");
  const LIMIT = 100;
  let bundle = null;
  let markers = {};
  let map = null;

  function fold(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/ё/g, "е")
      .replace(/і/g, "и")
      .replace(/ў/g, "у")
      .replace(/[^0-9a-zа-я]+/g, "");
  }

  function cemeteryById(id) {
    return bundle.cemeteries.find(function (item) {
      return item.id === id;
    });
  }

  function say(text) {
    status.textContent = text;
  }

  function clearResults() {
    results.replaceChildren();
  }

  function personText(row, cemetery) {
    const name = [row.s, row.g, row.p].filter(Boolean).join(" ") || "без імя";
    const dates = [];
    if (row.b) dates.push("нар. " + row.b);
    if (row.d) dates.push("пам. " + row.d);
    const place = [];
    if (row.sec) place.push("сектар " + row.sec);
    if (row.rw) place.push("рад " + row.rw);
    if (row.pl) place.push("месца " + row.pl);
    const bits = [name];
    if (dates.length) bits.push(dates.join(", "));
    bits.push(cemetery.name);
    if (place.length) bits.push(place.join(", "));
    if (row.note) bits.push(row.note);
    return bits.join(", ");
  }

  function showCemeteries() {
    clearResults();
    bundle.cemeteries.forEach(function (cemetery) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button-secondary mogilki-place";
      button.textContent = cemetery.name + ", " + cemetery.count;
      button.addEventListener("click", function () {
        form.elements.cemetery.value = cemetery.name;
        form.elements.surname.value = "";
        form.elements.year.value = "";
        apply();
        const marker = markers[cemetery.id];
        if (marker && cluster) {
          cluster.zoomToShowLayer(marker, function () { marker.openPopup(); });
        }
      });
      item.append(button);
      results.append(item);
    });
    say("Могілак: " + bundle.cemeteries.length + ". Пахаванняў: " + bundle.burials.length + ".");
  }

  function showPeople(rows) {
    clearResults();
    const shown = rows.slice(0, LIMIT);
    shown.forEach(function (row) {
      const cemetery = cemeteryById(row.c);
      const item = document.createElement("li");
      item.append(document.createTextNode(personText(row, cemetery)));
      if (row.url) {
        item.append(document.createTextNode(" "));
        const link = document.createElement("a");
        link.href = row.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "Кропка на мапе";
        item.append(link);
      }
      results.append(item);
    });
    if (!rows.length) {
      say("Нічога не знойдзена.");
      return;
    }
    if (rows.length > LIMIT) {
      say("Паказана " + LIMIT + " з " + rows.length + ".");
      return;
    }
    say("Знойдзена: " + rows.length + ".");
  }

  function cemeteryFromQuery() {
    const value = new URLSearchParams(window.location.search).get("cemetery");
    return value ? value.trim() : "";
  }

  function showInitial() {
    const cemetery = cemeteryFromQuery();
    if (!cemetery) {
      showCemeteries();
      return;
    }
    form.elements.cemetery.value = cemetery;
    apply();
  }

  function apply() {
    const cemeteryQuery = fold(form.elements.cemetery.value);
    const surnameQuery = fold(form.elements.surname.value);
    const yearText = form.elements.year.value.trim();
    if (yearText && !/^\d{4}$/.test(yearText)) {
      form.elements.year.setAttribute("aria-invalid", "true");
      say("Год патрэбны чатырма лічбамі.");
      form.elements.year.focus();
      return;
    }
    form.elements.year.removeAttribute("aria-invalid");
    const year = yearText ? Number(yearText) : null;
    if (!cemeteryQuery && !surnameQuery && year === null) {
      showCemeteries();
      return;
    }
    const rows = bundle.burials.filter(function (row) {
      if (cemeteryQuery && !fold(cemeteryById(row.c).name).includes(cemeteryQuery)) return false;
      const surnameText = fold([row.s, row.note].filter(Boolean).join(" "));
      if (surnameQuery && !surnameText.includes(surnameQuery)) return false;
      if (year !== null && row.by !== year && row.dy !== year) return false;
      return true;
    });
    showPeople(rows);
  }

  const mapStatus = document.getElementById("map-status");
  const layerList = document.getElementById("layer-list");
  let places = null;
  let enabled = new Set();
  let records = [];
  let cluster = null;
  let outline = null;

  function tone(color) {
    return "tone-" + String(color || "#37474F").replace("#", "").toLowerCase();
  }

  function dotIcon(color) {
    return L.divIcon({
      className: "leaflet-div-icon dot-wrap",
      html: '<span class="dot ' + tone(color) + '"></span>',
      iconSize: [32, 32],
      iconAnchor: [16, 16],
      popupAnchor: [0, -12],
    });
  }

  function pinIcon(color) {
    return L.divIcon({
      className: "leaflet-div-icon pin-wrap",
      html: '<span class="pin ' + tone(color) + '"></span>',
      iconSize: [28, 28],
      iconAnchor: [14, 26],
      popupAnchor: [0, -24],
    });
  }

  function markerIcon(layer) {
    const color = layer ? layer.color : "#37474F";
    return layer && layer.marker === "pin" ? pinIcon(color) : dotIcon(color);
  }

  function preferredLayer(record) {
    const active = record.layers.filter(function (layer) {
      return enabled.has(layer.id);
    });
    return active.find(function (layer) { return layer.marker === "pin"; })
      || active.find(function (layer) { return layer.id === "navahrudak"; })
      || active[0]
      || null;
  }

  function popup(record) {
    const node = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = record.name || "Без назвы";
    node.append(title);
    const layers = document.createElement("div");
    layers.textContent = record.layers.map(function (layer) { return layer.name; }).join(" · ");
    node.append(layers);
    if (record.note) {
      const note = document.createElement("div");
      note.className = "mogilki-note";
      note.textContent = record.note;
      node.append(note);
    }
    if (record.burial) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button-secondary";
      button.textContent = "Паказаць спіс";
      button.addEventListener("click", function () {
        form.elements.cemetery.value = record.burial.name;
        apply();
        document.getElementById("results").focus();
      });
      node.append(button);
    }
    return node;
  }

  function reportMap() {
    const visible = records.reduce(function (sum, record) {
      return sum + (preferredLayer(record) ? 1 : 0);
    }, 0);
    mapStatus.textContent = visible
      ? "На мапе " + visible.toLocaleString("be") + " месцаў."
      : "Усе слаі схаваныя.";
  }

  function applyVisibility() {
    const shown = [];
    records.forEach(function (record) {
      const layer = preferredLayer(record);
      if (!layer) return;
      record.marker.options.color = layer.color;
      record.marker.setIcon(markerIcon(layer));
      shown.push(record.marker);
    });
    cluster.clearLayers();
    cluster.addLayers(shown);
    if (outline) {
      if (enabled.has("navahrudak")) {
        if (!map.hasLayer(outline)) outline.addTo(map);
      } else if (map.hasLayer(outline)) {
        map.removeLayer(outline);
      }
    }
    reportMap();
  }

  function setLayer(id, on) {
    const input = layerList.querySelector('input[data-layer="' + id + '"]');
    if (input) input.checked = on;
    if (on) enabled.add(id);
    else enabled.delete(id);
    applyVisibility();
  }

  function draw(mapData) {
    places = mapData;
    enabled = new Set(mapData.layers.filter(function (layer) {
      return layer.on !== false;
    }).map(function (layer) { return layer.id; }));
    map = L.map("map", { zoomControl: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map);
    cluster = L.markerClusterGroup({
      maxClusterRadius: 40,
      disableClusteringAtZoom: 16,
      spiderfyOnMaxZoom: true,
      showCoverageOnHover: false,
      iconCreateFunction: function (group) {
        const colors = new Set(group.getAllChildMarkers().map(function (marker) {
          return marker.options.color;
        }));
        const color = colors.size === 1 ? colors.values().next().value : "#37474F";
        return L.divIcon({
          className: "leaflet-div-icon cluster-wrap",
          html: '<span class="cluster-blob ' + tone(color) + '">' + group.getChildCount() + "</span>",
          iconSize: [44, 44],
          iconAnchor: [22, 22],
        });
      },
    });
    map.addLayer(cluster);
    const ring = (mapData.uyezd && mapData.uyezd.ring ? mapData.uyezd.ring : []).map(function (pair) {
      return [pair[1], pair[0]];
    });
    const povet = mapData.layers.find(function (layer) { return layer.id === "navahrudak"; });
    if (ring.length) {
      map.createPane("uyezd");
      map.getPane("uyezd").style.zIndex = 350;
      outline = L.polygon(ring, {
        pane: "uyezd",
        color: povet ? povet.color : "#9A3412",
        weight: 2,
        fillColor: povet ? povet.color : "#9A3412",
        fillOpacity: 0.08,
        interactive: false,
      });
    }
    mapData.points.forEach(function (row) {
      const indexes = Array.isArray(row[0]) ? row[0] : [row[0]];
      const layers = indexes.map(function (index) { return mapData.layers[index]; }).filter(Boolean);
      const record = {
        layers: layers,
        name: row[1],
        lon: row[2],
        lat: row[3],
        note: row[4] || "",
        burial: bundle.cemeteries.find(function (cemetery) {
          return cemetery.lat != null && Math.abs(cemetery.lat - row[3]) < 0.0002 && Math.abs(cemetery.lon - row[2]) < 0.0002;
        }) || null,
      };
      record.marker = L.marker([record.lat, record.lon], {
        icon: markerIcon(layers[0]),
        keyboard: false,
        title: record.name || "",
        alt: record.name || "",
        color: layers[0] ? layers[0].color : "#37474F",
      });
      record.marker.bindPopup(popup(record));
      if (record.burial) markers[record.burial.id] = record.marker;
      records.push(record);
    });
    const ordered = mapData.layers.slice().sort(function (a, b) {
      const rank = function (layer) {
        return layer.id === "navahrudak" ? 0 : layer.id === "indexed" ? 1 : 2;
      };
      return rank(a) - rank(b);
    });
    ordered.forEach(function (layer) {
      const count = records.filter(function (record) {
        return record.layers.some(function (item) { return item.id === layer.id; });
      }).length;
      const label = document.createElement("label");
      label.className = "layer-row";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = enabled.has(layer.id);
      input.dataset.layer = layer.id;
      const swatch = document.createElement("span");
      swatch.className = (layer.marker === "pin" ? "swatch swatch-pin " : "swatch ") + tone(layer.color);
      swatch.setAttribute("aria-hidden", "true");
      const name = document.createElement("span");
      name.textContent = layer.name;
      const countNode = document.createElement("span");
      countNode.className = "layer-count";
      countNode.textContent = String(count);
      label.append(input, swatch, name, countNode);
      layerList.append(label);
      input.addEventListener("change", function () { setLayer(layer.id, input.checked); });
    });
    function setAll(on) {
      mapData.layers.forEach(function (layer) {
        const input = layerList.querySelector('input[data-layer="' + layer.id + '"]');
        if (input) input.checked = on;
        if (on) enabled.add(layer.id);
        else enabled.delete(layer.id);
      });
      applyVisibility();
    }
    document.getElementById("show-all").addEventListener("click", function () { setAll(true); });
    document.getElementById("hide-all").addEventListener("click", function () { setAll(false); });
    applyVisibility();
    const onlyHome = [...enabled].every(function (id) { return id === "navahrudak" || id === "indexed"; });
    if (outline && enabled.has("navahrudak") && onlyHome) {
      map.fitBounds(outline.getBounds(), { padding: [24, 24] });
    }
    bundle.cemeteries.forEach(function (cemetery) {
      const option = document.createElement("option");
      option.value = cemetery.name;
      names.append(option);
    });
    showInitial();
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    apply();
  });
  form.addEventListener("reset", function () {
    window.setTimeout(showCemeteries, 0);
  });

  Promise.all([
    fetch("/mogilki/data").then(function (response) {
      if (!response.ok) throw new Error("data");
      return response.json();
    }),
    fetch("/mogilki/map").then(function (response) {
      if (!response.ok) throw new Error("map");
      return response.json();
    }),
  ]).then(function (loaded) {
    bundle = loaded[0];
    try {
      draw(loaded[1]);
    } catch (error) {
      if (cemeteryFromQuery()) {
        showInitial();
        mapStatus.textContent = "Мапа не адкрылася. Спіс ніжэй працуе.";
      } else {
        showCemeteries();
        say("Мапа не адкрылася. Спіс ніжэй працуе.");
      }
    }
  }).catch(function () {
    say("Спіс не загрузіўся. Абнавіце старонку.");
  });
})();
