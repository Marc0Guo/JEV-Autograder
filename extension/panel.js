(() => {
  const API = "http://127.0.0.1:8010";

  function slug(text) {
    return String(text || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 40);
  }

  function parsePoints(text) {
    const found = [...String(text || "").matchAll(/(-?\d+(?:\.\d+)?)\s*pts\b/gi)].map((match) => Number(match[1]));
    if (!found.length || found.some((value) => Number.isNaN(value))) return null;
    return Math.max(...found);
  }

  function isNoise(line) {
    return (
      !line ||
      /^criterion score$/i.test(line) ||
      /view longer description/i.test(line) ||
      /^threshold:/i.test(line) ||
      /^[\d.\s/—–-]*pts?$/i.test(line) ||
      /^[\d.\s/—–-]+$/.test(line) ||
      line === "--"
    );
  }

  function rowName(row) {
    const cell = row.querySelector(":scope > td, :scope > [role='cell']") || row;
    const lines = (cell.innerText || cell.textContent || "")
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => !isNoise(line));
    return lines[0] || "";
  }

  function buttonRecord(el) {
    const testid = el.getAttribute("data-testid") || "";
    const description = el.querySelector(`[data-testid="${testid}-description"]`);
    const pointsNode = el.querySelector(`[data-testid="${testid}-points"]`);
    const label = ((description && description.textContent) || "").trim();
    const pointsText = ((pointsNode && pointsNode.textContent) || "").trim();
    const text = label ? `${label}\n${pointsText}` : el.innerText || el.textContent || "";
    return { el, text, label, points: parsePoints(pointsText || text) };
  }

  function collectTraditional(doc) {
    const nodes = [...doc.querySelectorAll("button[data-testid^='traditional-criterion-']")];
    const groups = new Map();
    for (const el of nodes) {
      const match = (el.getAttribute("data-testid") || "").match(/^traditional-criterion-(.+)-ratings-(\d+)$/);
      if (!match) continue;
      const id = match[1];
      if (!groups.has(id)) {
        groups.set(id, {
          row: el.closest("tr, [role='row']") || el.parentElement,
          buttons: [],
        });
      }
      groups.get(id).buttons[Number(match[2])] = buttonRecord(el);
    }
    return [...groups.values()]
      .map((group) => ({
        name: rowName(group.row),
        element: group.row,
        buttons: group.buttons.filter(Boolean),
      }))
      .filter((row) => row.buttons.length);
  }

  function collectClassic(doc) {
    const rows = [...doc.querySelectorAll("tr.criterion")];
    return rows
      .map((row) => {
        const nameNode = row.querySelector(".criterion_description .description, .description_content, .description");
        const buttons = [...row.querySelectorAll(".rating, .rating-tier")].map(buttonRecord);
        return { name: (nameNode && nameNode.textContent.trim()) || rowName(row), element: row, buttons };
      })
      .filter((row) => row.buttons.length);
  }

  function collectRows(doc) {
    const traditional = collectTraditional(doc);
    return traditional.length ? traditional : collectClassic(doc);
  }

  function matchRow(rows, criterion) {
    const id = String(criterion.id || "");
    const exact = rows.filter((row) => slug(row.name) === id);
    if (exact.length === 1) return exact[0];
    const loose = rows.filter((row) => {
      const key = slug(row.name);
      return key && (key.startsWith(id) || id.startsWith(key));
    });
    return loose.length === 1 ? loose[0] : null;
  }

  function pickButton(buttons, chosen) {
    const label = String(chosen.label || "").trim().toLowerCase();
    if (label && label !== "yes" && label !== "no") {
      const textHits = buttons.filter((button) => button.text.toLowerCase().includes(label));
      if (textHits.length === 1) return textHits[0];
    }
    if (label === "yes") {
      const full = buttons.filter((button) => /full marks?/.test(button.text.toLowerCase()));
      if (full.length === 1) return full[0];
    }
    if (label === "no") {
      const none = buttons.filter((button) => /no marks?/.test(button.text.toLowerCase()));
      if (none.length === 1) return none[0];
    }
    const points = Number(chosen.points);
    if (Number.isFinite(points)) {
      const exact = buttons.filter((button) => button.points != null && Math.abs(button.points - points) < 0.001);
      if (exact.length === 1) return exact[0];
    }
    return null;
  }

  function isSelected(el) {
    if (el.classList.contains("selected")) return true;
    const visible = el.innerText || "";
    if (/(^|\n)\s*Selected\b/i.test(visible)) return true;
    return [...el.querySelectorAll("*")].some((node) =>
      /^(Selected|Selected and Self Assessment)$/i.test((node.textContent || "").trim())
    );
  }

  function ratingName(button) {
    if (button.label) return button.label;
    const line = button.text
      .split("\n")
      .map((item) => item.trim())
      .find((item) => item && !/pts\b/i.test(item) && !/\bselected\b/i.test(item));
    return line || button.text.trim().split("\n")[0] || "rating";
  }

  function chosenRating(criterion) {
    const ratings = criterion.ratings || [];
    return (
      ratings.find((item) => item.key === criterion.model_key) || {
        label: criterion.label,
        points: criterion.points,
      }
    );
  }

  function wait(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function applyRatings(doc, criteria) {
    const selected = [];
    const missed = [];
    for (const criterion of criteria || []) {
      const row = matchRow(collectRows(doc), criterion);
      if (!row) {
        missed.push(criterion.id || "a criterion");
        continue;
      }
      const button = pickButton(row.buttons, chosenRating(criterion));
      if (!button) {
        missed.push(row.name || criterion.id);
        continue;
      }
      if (!isSelected(button.el)) {
        button.el.click();
        await wait(120);
      }
      const live = matchRow(collectRows(doc), criterion);
      const liveButton = live && pickButton(live.buttons, chosenRating(criterion));
      const chosen = liveButton || button;
      if (chosen && !isSelected(chosen.el)) writeScore(live && live.element, chosen.points);
      selected.push({ name: row.name, rating: ratingName(chosen) });
    }
    return { selected, missed };
  }

  const root = document.createElement("div");
  root.id = "jev-review-root";
  const shadow = root.attachShadow({ mode: "open" });
  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      .bar {
        display: block;
        box-sizing: border-box;
        width: 100%;
        margin: 0 0 12px;
        padding: 10px 12px;
        background: #fffdf8;
        color: #1c1915;
        border: 1px solid #d9d1c5;
        border-radius: 10px;
        font: 14px/1.4 "Segoe UI", sans-serif;
      }
      h2 { font: 500 18px Georgia, serif; margin: 0; }
      .note { color: #5c564c; margin: 6px 0 0; }
      .error { color: #9a3412; margin: 6px 0 0; }
      button {
        margin-top: 8px;
        font: inherit;
        border-radius: 8px;
        padding: 8px 12px;
        cursor: pointer;
        border: 0;
        background: #1c1915;
        color: #fff;
      }
      button:disabled { opacity: 0.55; cursor: wait; }
    </style>
    <div class="bar">
      <h2>JEV</h2>
      <p class="note" id="page"></p>
      <p class="note" id="note">Grade with JEV, then the rubric ratings on this page are selected for you.</p>
      <p class="error" id="error"></p>
      <button type="button" id="grade">Grade with JEV</button>
    </div>
  `;

  const page = shadow.getElementById("page");
  const note = shadow.getElementById("note");
  const error = shadow.getElementById("error");
  const gradeButton = shadow.getElementById("grade");
  let busy = false;
  let gradedStudent = "";

  function findRubricTable(doc) {
    const scope = doc || document;
    const view = scope.querySelector("[data-testid='rubric-assessment-traditional-view']");
    const table = view && view.querySelector("table");
    return table || scope.querySelector("#rubric_full table") || scope.querySelector("table.rubric_table");
  }

  function writeScore(row, points) {
    if (!row || points == null || Number.isNaN(Number(points))) return;
    const input = row.querySelector("input[data-testid^='criterion-score-']");
    if (!input) return;
    const text = String(points);
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    input.focus();
    setter.call(input, text);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.blur();
  }

  function place() {
    const table = findRubricTable(document);
    if (table) {
      const span = table.querySelector("[colspan]");
      const cols = Number(span && span.getAttribute("colspan")) || 3;
      let holder = document.getElementById("jev-review-row");
      if (!holder) {
        holder = document.createElement("tr");
        holder.id = "jev-review-row";
        const cell = document.createElement("td");
        cell.colSpan = cols;
        cell.style.padding = "8px";
        cell.style.background = "#fff";
        holder.appendChild(cell);
      }
      const head = table.tHead || table.querySelector("thead") || table;
      if (holder.parentElement !== head) head.insertBefore(holder, head.firstChild);
      const cell = holder.querySelector("td");
      root.style.position = "";
      root.style.top = "";
      root.style.right = "";
      root.style.width = "100%";
      root.style.zIndex = "";
      if (root.parentElement !== cell) cell.appendChild(root);
      return;
    }
    root.style.position = "fixed";
    root.style.top = "72px";
    root.style.right = "16px";
    root.style.width = "280px";
    root.style.zIndex = "2147483646";
    if (!root.isConnected) document.documentElement.appendChild(root);
  }

  function pageContext(href) {
    const url = new URL(href || location.href);
    const match = url.pathname.match(/\/courses\/(\d+)\/gradebook\/speed_grader/);
    const fromUrl = url.searchParams.get("student_id") || "";
    const select = document.querySelector("#students_selectmenu");
    const fromSelect = select && select.value && select.value !== "0" ? select.value : "";
    return {
      courseId: match ? match[1] : "",
      assignmentId: url.searchParams.get("assignment_id") || "",
      studentId: fromUrl || fromSelect,
      anonymousId: url.searchParams.get("anonymous_id") || "",
    };
  }

  function describePage(ctx) {
    if (!ctx.courseId) return "Open a Canvas SpeedGrader page.";
    const parts = [`Course ${ctx.courseId}`];
    if (ctx.assignmentId) parts.push(`Assignment ${ctx.assignmentId}`);
    if (ctx.studentId) parts.push(`Student ${ctx.studentId}`);
    else if (ctx.anonymousId) parts.push(`Anonymous ${ctx.anonymousId}`);
    return parts.join(" · ");
  }

  async function api(path, body) {
    const response = await fetch(API + path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = { detail: text };
    }
    if (!response.ok) {
      const detail = (payload && (payload.detail || payload.message)) || response.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return payload;
  }

  function showError(message) {
    error.textContent = message;
    note.textContent = "Grade with JEV, then the rubric ratings on this page are selected for you.";
  }

  gradeButton.onclick = async () => {
    if (busy) return;
    const ctx = pageContext();
    page.textContent = describePage(ctx);
    error.textContent = "";
    if (!ctx.courseId || !ctx.assignmentId || (!ctx.studentId && !ctx.anonymousId)) {
      showError("Open a student in SpeedGrader first.");
      return;
    }
    const table = findRubricTable(document);
    if (!table) {
      showError("Open the rubric on the right, then grade.");
      return;
    }
    busy = true;
    gradeButton.disabled = true;
    note.textContent = "Scoring this student…";
    try {
      const review = window.__JEV_MOCK_REVIEW
        ? window.__JEV_MOCK_REVIEW
        : await api("/api/review", {
            source: "canvas",
            course_id: ctx.courseId,
            assignment_id: ctx.assignmentId,
            student_id: ctx.studentId,
            anonymous_id: ctx.anonymousId,
          });
      const applied = await applyRatings(table, (review.grade && review.grade.criteria) || []);
      gradedStudent = ctx.studentId || ctx.anonymousId;
      const picked = applied.selected.map((item) => `${item.name}: ${item.rating}`);
      if (!picked.length && applied.missed.length) {
        showError(`Could not select ratings for ${applied.missed.join(", ")}.`);
      } else {
        note.textContent = picked.length
          ? "Ratings are selected in this table. Submit Assessment to save."
          : "JEV returned no ratings to select.";
        if (applied.missed.length) error.textContent = `Could not match ${applied.missed.join(", ")}.`;
      }
    } catch (exc) {
      const offline = /Failed to fetch|NetworkError/i.test(exc.message);
      showError(offline ? "Start the local autograder at http://127.0.0.1:8010." : exc.message);
    } finally {
      busy = false;
      gradeButton.disabled = false;
    }
  };

  place();
  page.textContent = describePage(pageContext());
  setInterval(() => {
    place();
    const ctx = pageContext();
    const student = ctx.studentId || ctx.anonymousId;
    page.textContent = describePage(ctx);
    if (gradedStudent && student && student !== gradedStudent && !busy) {
      gradedStudent = "";
      error.textContent = "";
      note.textContent = "This is a different student. Grade with JEV again.";
    }
  }, 700);

  window.__jevReview = { applyRatings, collectRows, slug, pageContext, describePage };
})();
