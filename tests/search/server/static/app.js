const queryForm = document.getElementById("query-form");
const queryInput = document.getElementById("query-input");
const queryError = document.getElementById("query-error");
const querySql = document.getElementById("query-sql");
const queryResults = document.getElementById("query-results");

const actorForm = document.getElementById("actor-form");
const actorPrefix = document.getElementById("actor-prefix");
const actorSuggestions = document.getElementById("actor-suggestions");
const actorSelectedJson = document.getElementById("actor-selected-json");
const actorResults = document.getElementById("actor-results");

let autocompleteTimer = null;
let suggestionValues = [];
let highlightedSuggestionIndex = -1;

const jsonModal = createJsonModal();

queryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  queryError.textContent = "";
  querySql.innerHTML = "";
  queryResults.innerHTML = "";

  const query = queryInput.value.trim();
  if (!query) {
    queryError.textContent = "Enter a search query.";
    return;
  }

  try {
    const response = await fetch("/query", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ query })
    });

    const payload = await response.json();
    if (!response.ok) {
      queryError.textContent = payload.error || "Query failed.";
      return;
    }

    renderGeneratedSql(payload.sql || "", payload.params || []);
    renderQueryTable(payload.columns || [], payload.rows || []);
  } catch (error) {
    queryError.textContent = `Request failed: ${error}`;
  }
});

function renderGeneratedSql(sql, params) {
  if (!sql) {
    querySql.innerHTML = "";
    return;
  }

  const sqlHeading = document.createElement("strong");
  sqlHeading.textContent = "Generated SQL";

  const sqlPre = document.createElement("pre");
  sqlPre.textContent = sql;

  const paramsDiv = document.createElement("div");
  paramsDiv.textContent = `Params: ${JSON.stringify(params)}`;

  querySql.appendChild(sqlHeading);
  querySql.appendChild(sqlPre);
  querySql.appendChild(paramsDiv);
}

actorPrefix.addEventListener("input", () => {
  hideSelectedObjectJson();
  if (autocompleteTimer) {
    clearTimeout(autocompleteTimer);
  }
  autocompleteTimer = setTimeout(async () => {
    const prefix = actorPrefix.value.trim();
    if (!prefix) {
      clearSuggestions();
      return;
    }

    try {
      const response = await fetch(`/autocomplete?prefix=${encodeURIComponent(prefix)}`);
      const values = await response.json();
      renderSuggestions(Array.isArray(values) ? values : []);
    } catch (_) {
      clearSuggestions();
    }
  }, 150);
});

actorPrefix.addEventListener("keydown", (event) => {
  if (actorSuggestions.hidden || !suggestionValues.length) {
    return;
  }

  if (event.key === "ArrowDown") {
    event.preventDefault();
    setHighlightedSuggestionIndex(
      highlightedSuggestionIndex + 1 >= suggestionValues.length ? 0 : highlightedSuggestionIndex + 1
    );
    return;
  }

  if (event.key === "ArrowUp") {
    event.preventDefault();
    setHighlightedSuggestionIndex(
      highlightedSuggestionIndex - 1 < 0 ? suggestionValues.length - 1 : highlightedSuggestionIndex - 1
    );
    return;
  }

  if (event.key === "Enter" && highlightedSuggestionIndex >= 0) {
    event.preventDefault();
    selectSuggestion(suggestionValues[highlightedSuggestionIndex]);
    return;
  }

  if (event.key === "Escape") {
    clearSuggestions();
  }
});

actorPrefix.addEventListener("blur", () => {
  setTimeout(() => {
    clearSuggestions();
  }, 120);
});

actorForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  actorResults.innerHTML = "";
  clearSuggestions();

  const prefix = actorPrefix.value.trim();
  if (!prefix) {
    actorResults.textContent = "Enter a prefix to search.";
    hideSelectedObjectJson();
    return;
  }

  try {
    const response = await fetch(`/actor-search?prefix=${encodeURIComponent(prefix)}`);
    const results = await response.json();
    renderActorResults(results);
    const selectedActor =
      Array.isArray(results)
        ? results.find(
            (candidate) =>
              candidate &&
              typeof candidate.preferredUsername === "string" &&
              candidate.preferredUsername.toLowerCase() === prefix.toLowerCase()
          )
        : null;
    if (selectedActor && selectedActor.uri) {
      await showSelectedActorJsonByUri(selectedActor.uri);
    } else {
      hideSelectedObjectJson();
    }
  } catch (error) {
    actorResults.textContent = `Request failed: ${error}`;
    hideSelectedObjectJson();
  }
});

function renderQueryTable(columns, rows) {
  if (!rows.length) {
    queryResults.textContent = "No rows returned.";
    return;
  }

  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  for (const col of columns) {
    const th = document.createElement("th");
    th.textContent = col;
    headerRow.appendChild(th);
  }
  thead.appendChild(headerRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    row.forEach((value, index) => {
      const td = document.createElement("td");
      const columnName = columns[index];
      if (columnName === "uri" && value) {
        const uri = String(value);
        const link = document.createElement("a");
        link.href = uri;
        link.textContent = uri;
        link.addEventListener("click", async (event) => {
          event.preventDefault();
          await openObjectModal(uri);
        });
        td.appendChild(link);
      } else {
        td.textContent = value === null ? "NULL" : String(value);
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);

  queryResults.appendChild(table);
}

function renderSuggestions(values) {
  actorSuggestions.innerHTML = "";
  suggestionValues = values;
  highlightedSuggestionIndex = -1;

  if (!values.length) {
    actorSuggestions.hidden = true;
    actorPrefix.setAttribute("aria-expanded", "false");
    return;
  }

  for (let index = 0; index < values.length; index += 1) {
    const value = values[index];
    const item = document.createElement("li");
    item.textContent = value;
    item.setAttribute("role", "option");
    item.id = `actor-suggestion-${index}`;
    item.dataset.index = String(index);
    item.addEventListener("mousedown", (event) => {
      event.preventDefault();
      selectSuggestion(value);
    });
    actorSuggestions.appendChild(item);
  }

  actorSuggestions.hidden = false;
  actorPrefix.setAttribute("aria-expanded", "true");
}

function setHighlightedSuggestionIndex(index) {
  highlightedSuggestionIndex = index;

  Array.from(actorSuggestions.children).forEach((child, childIndex) => {
    const isActive = childIndex === highlightedSuggestionIndex;
    child.classList.toggle("active", isActive);
    child.setAttribute("aria-selected", isActive ? "true" : "false");
    if (isActive) {
      actorPrefix.setAttribute("aria-activedescendant", child.id);
    }
  });
}

function selectSuggestion(value) {
  actorPrefix.value = value;
  clearSuggestions();
}

function clearSuggestions() {
  actorSuggestions.innerHTML = "";
  actorSuggestions.hidden = true;
  suggestionValues = [];
  highlightedSuggestionIndex = -1;
  actorPrefix.setAttribute("aria-expanded", "false");
  actorPrefix.removeAttribute("aria-activedescendant");
}

async function openObjectModal(uri) {
  jsonModal.title.textContent = uri;
  jsonModal.pre.textContent = "Loading...";
  jsonModal.dialog.showModal();

  try {
    const response = await fetch(`/object?uri=${encodeURIComponent(uri)}`);
    const payload = await response.json();
    if (!response.ok) {
      jsonModal.pre.textContent = `Error: ${payload.error || "Request failed."}`;
      return;
    }

    jsonModal.pre.textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    jsonModal.pre.textContent = `Request failed: ${error}`;
  }
}

function createJsonModal() {
  const dialog = document.createElement("dialog");
  dialog.className = "json-modal";

  const header = document.createElement("div");
  header.className = "json-modal-header";

  const title = document.createElement("h3");
  title.className = "json-modal-title";
  header.appendChild(title);

  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.textContent = "Close";
  closeButton.addEventListener("click", () => {
    dialog.close();
  });
  header.appendChild(closeButton);

  const pre = document.createElement("pre");
  pre.className = "json-modal-content";

  dialog.appendChild(header);
  dialog.appendChild(pre);
  document.body.appendChild(dialog);

  dialog.addEventListener("click", (event) => {
    const rect = dialog.getBoundingClientRect();
    const clickedInDialog =
      rect.top <= event.clientY &&
      event.clientY <= rect.top + rect.height &&
      rect.left <= event.clientX &&
      event.clientX <= rect.left + rect.width;
    if (!clickedInDialog) {
      dialog.close();
    }
  });

  return { dialog, title, pre };
}

function renderActorResults(results) {
  if (!results.length) {
    actorResults.textContent = "No matching actors.";
    return;
  }

  const list = document.createElement("ul");
  for (const actor of results) {
    const item = document.createElement("li");
    item.textContent = `${actor.preferredUsername} — ${actor.uri}${actor.summary ? ` (${actor.summary})` : ""}`;
    list.appendChild(item);
  }
  actorResults.appendChild(list);
}

async function showSelectedActorJsonByUri(uri) {
  if (!uri) {
    hideSelectedObjectJson();
    return;
  }

  try {
    const objectResponse = await fetch(`/object?uri=${encodeURIComponent(uri)}`);
    const objectPayload = await objectResponse.json();
    if (!objectResponse.ok) {
      renderSelectedObjectJson(uri, { error: objectPayload.error || "Request failed." });
      return;
    }

    renderSelectedObjectJson(uri, objectPayload);
  } catch (_) {
    hideSelectedObjectJson();
  }
}

function renderSelectedObjectJson(uri, payload) {
  actorSelectedJson.innerHTML = "";

  const heading = document.createElement("strong");
  heading.textContent = `Selected object: ${uri}`;

  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(payload, null, 2);

  actorSelectedJson.appendChild(heading);
  actorSelectedJson.appendChild(pre);
  actorSelectedJson.hidden = false;
}

function hideSelectedObjectJson() {
  actorSelectedJson.hidden = true;
  actorSelectedJson.innerHTML = "";
}
