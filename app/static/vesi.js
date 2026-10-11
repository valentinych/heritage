(function () {
  const query = document.getElementById("find-query");
  const findStatus = document.getElementById("find-status");
  const findResults = document.getElementById("find-results");
  const artifactStatus = document.getElementById("artifact-status");
  const artifactList = document.getElementById("artifacts");
  const artifactHeading = document.getElementById("artifact-heading");
  const LIMIT = 20;
  let settlements = [];
  let markers = {};
  let byArtifact = {};
  let map = null;
  let cluster = null;

  function fold(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/ё/g, "е")
      .replace(/і/g, "и")
      .replace(/ў/g, "у");
  }

  function hasPoint(settlement) {
    return Number.isFinite(settlement.lat) && Number.isFinite(settlement.lon);
  }

  const MARKER_LEVEL = {
    "vesi-dot-red": "нет сведений",
    "vesi-dot-yellow": "мало сведений",
    "vesi-dot-green": "есть сведения",
  };

  function markerLevel(settlement) {
    const className = Object.prototype.hasOwnProperty.call(MARKER_LEVEL, settlement.marker_class)
      ? settlement.marker_class
      : "vesi-dot-red";
    return { className: className, words: MARKER_LEVEL[className] };
  }

  function markerIcon(className) {
    return L.divIcon({
      className: "leaflet-div-icon vesi-dot-wrap",
      html: '<span class="vesi-dot ' + className + '"></span>',
      iconSize: [32, 32],
      iconAnchor: [16, 16],
    });
  }

  function kindLabel(artifact) {
    const kind = document.createElement("span");
    kind.className = "vesi-kind";
    const mark = document.createElement("span");
    mark.setAttribute("aria-hidden", "true");
    const word = document.createElement("span");
    if (artifact.kind === "parish") {
      mark.textContent = "⛪";
      word.textContent = "парафия";
    } else {
      mark.textContent = "🪦";
      word.textContent = "кладбище";
    }
    kind.append(mark, word);
    return kind;
  }

  function appendLines(item, rows, write) {
    if (!rows.length) return;
    const list = document.createElement("ul");
    list.className = "vesi-coverage";
    rows.forEach(function (row) {
      const line = document.createElement("li");
      write(line, row);
      list.append(line);
    });
    item.append(list);
  }

  function appendCoverage(item, artifact) {
    appendLines(item, artifact.coverage || [], function (line, act) {
      let text = act.label || "";
      if (act.years) text += ", " + act.years;
      if (act.records !== null && act.records !== undefined && act.records !== "") {
        text += ", записей " + act.records;
      }
      line.textContent = text;
    });
  }

  function appendNote(item, text) {
    if (!text) return;
    const note = document.createElement("p");
    note.className = "vesi-note";
    note.textContent = text;
    item.append(note);
  }

  function appendDocuments(item, artifact) {
    appendLines(item, artifact.documents || [], function (line, doc) {
      const parts = [];
      if (doc.year) parts.push(doc.year);
      if (doc.type_label || doc.type) parts.push(doc.type_label || doc.type);
      const archive = [doc.archive, doc.fod].filter(Boolean).join(" ");
      if (archive) parts.push(archive);
      if (doc.archive_name) parts.push(doc.archive_name);
      if (doc.digitized) parts.push("оцифровано");
      if (doc.rejected) parts.push("отказано в выдаче");
      line.append(document.createTextNode(parts.join(", ")));
      if (typeof doc.url === "string" && doc.url.indexOf("https://") === 0) {
        const link = document.createElement("a");
        link.href = doc.url;
        link.rel = "noopener noreferrer";
        link.textContent = "снимки";
        line.append(document.createTextNode(" "), link);
      }
    });
  }

  function appendParish(item, artifact) {
    if (artifact.church) appendNote(item, artifact.church);
    const place = [artifact.place_type, artifact.place].filter(Boolean).join(" ");
    if (place) appendNote(item, place);
    if (artifact.atd && artifact.atd.length) appendNote(item, artifact.atd.join(", "));
    if (artifact.served && artifact.served.length) {
      appendNote(item, "Населённые пункты");
      appendLines(item, artifact.served, function (line, name) {
        line.textContent = name;
      });
    }
    appendCoverage(item, artifact);
    appendDocuments(item, artifact);
  }

  function showArtifacts(settlement, moveFocus) {
    artifactList.replaceChildren();
    artifactHeading.textContent = "Артефакты, " + settlement.name;
    settlement.artifacts.forEach(function (artifact) {
      const item = document.createElement("li");
      item.className = "vesi-artifact";
      let nameNode;
      if (artifact.kind === "parish" && typeof artifact.url === "string" && artifact.url.indexOf("https://") === 0) {
        nameNode = document.createElement("a");
        nameNode.href = artifact.url;
        nameNode.rel = "noopener noreferrer";
      } else if (artifact.indexed) {
        nameNode = document.createElement("a");
        nameNode.href = "/vesi/" + encodeURIComponent(artifact.id);
      } else {
        nameNode = document.createElement("span");
      }
      nameNode.textContent = artifact.name;
      const main = document.createElement("div");
      main.className = "vesi-artifact-main";
      main.append(nameNode, kindLabel(artifact));
      if (artifact.kind === "parish" && artifact.rite) {
        const rite = document.createElement("span");
        rite.className = "vesi-rite";
        rite.textContent = "обряд " + artifact.rite;
        main.append(rite);
      }
      const status = document.createElement("span");
      status.className = "status";
      const mark = document.createElement("span");
      mark.className = "status-mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = artifact.emoji;
      const words = document.createElement("span");
      words.textContent = artifact.label;
      status.append(mark, words);
      item.append(main, status);
      if (artifact.kind === "parish") appendParish(item, artifact);
      artifactList.append(item);
    });
    const count = settlement.artifacts.length;
    artifactStatus.textContent = settlement.name + ". Артефактов: " + count + ".";
    if (moveFocus) artifactHeading.focus();
  }

  function openSettlement(settlement, moveFocus) {
    showArtifacts(settlement, moveFocus);
    const marker = markers[settlement.id];
    if (marker && cluster) {
      cluster.zoomToShowLayer(marker);
    }
  }

  function showMatches(items) {
    findResults.replaceChildren();
    items.forEach(function (settlement) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button-secondary vesi-hit";
      button.textContent = settlement.name + ", " + settlement.artifacts.length;
      button.addEventListener("click", function () {
        openSettlement(settlement, true);
      });
      item.append(button);
      findResults.append(item);
    });
  }

  function search() {
    const needle = fold(query.value).trim();
    if (needle.length < 2) {
      findResults.replaceChildren();
      findStatus.textContent = needle ? "Введите ещё букву." : "";
      return;
    }
    const found = settlements.filter(function (settlement) {
      return fold(settlement.name).indexOf(needle) !== -1;
    });
    showMatches(found.slice(0, LIMIT));
    if (!found.length) {
      findStatus.textContent = "Ничего не найдено.";
      return;
    }
    findStatus.textContent = found.length > LIMIT
      ? "Показано " + LIMIT + " из " + found.length + "."
      : "Найдено: " + found.length + ".";
  }

  function draw(data) {
    settlements = data.settlements;
    settlements.forEach(function (settlement) {
      settlement.artifacts.forEach(function (artifact) {
        byArtifact[artifact.id] = settlement;
      });
    });
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
        return L.divIcon({
          className: "leaflet-div-icon vesi-dot-wrap",
          html: '<span class="vesi-cluster">' + group.getChildCount() + "</span>",
          iconSize: [44, 44],
          iconAnchor: [22, 22],
        });
      },
    });
    const bounds = [];
    settlements.forEach(function (settlement) {
      if (!hasPoint(settlement)) return;
      const level = markerLevel(settlement);
      const accessible = settlement.name + ", " + level.words;
      const marker = L.marker([settlement.lat, settlement.lon], {
        icon: markerIcon(level.className),
        title: accessible,
        alt: accessible,
        keyboard: true,
      });
      marker.on("add", function () {
        const el = marker.getElement();
        if (el) el.setAttribute("aria-label", accessible);
      });
      marker.on("click", function () {
        showArtifacts(settlement, false);
      });
      markers[settlement.id] = marker;
      cluster.addLayer(marker);
      bounds.push([settlement.lat, settlement.lon]);
    });
    map.addLayer(cluster);
    if (bounds.length) map.fitBounds(bounds, { padding: [24, 24] });
    const params = new URLSearchParams(window.location.search);
    const selected = byArtifact[params.get("at") || ""];
    if (selected) openSettlement(selected, false);
  }

  query.addEventListener("input", search);
  document.getElementById("find").addEventListener("submit", function (event) {
    event.preventDefault();
    search();
    const first = findResults.querySelector("button");
    if (first) first.click();
  });

  fetch("/vesi/data").then(function (response) {
    if (!response.ok) throw new Error("data");
    return response.json();
  }).then(draw).catch(function () {
    artifactStatus.textContent = "Не удалось загрузить карту.";
  });
})();
