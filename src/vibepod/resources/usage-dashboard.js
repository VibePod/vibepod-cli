const cards = document.getElementById("cards");
const status = document.getElementById("status");
const profile = document.getElementById("profile");
let snapshot = { providers: [] };
const names = { codex: "Codex", claude: "Claude", agy: "Antigravity" };
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function date(seconds) {
  return new Date(seconds * 1000).toLocaleString();
}
function render() {
  cards.replaceChildren();
  const rows = snapshot.providers.filter(
    (row) => !profile.value || row.profile === profile.value,
  );
  document.getElementById("empty").hidden = rows.length > 0;
  for (const row of rows) {
    const card = element("article", undefined, "card");
    const header = element("div", undefined, "card-head");
    const expired = row.stale || row.windows.some((w) => w.stale);
    header.append(
      element("h2", names[row.provider] || row.provider),
      element(
        "span",
        row.error ? "Unavailable" : expired ? "Stale snapshot" : "Observed",
        `badge${expired ? " stale" : ""}`,
      ),
    );
    card.append(header, element("p", row.profile, "profile"));
    if (row.message) card.append(element("p", row.message, "note"));
    for (const window of row.windows) {
      const section = element("div", undefined, "window");
      const heading = element("div", undefined, "window-header");
      const known = !window.disabled && typeof window.used_percent === "number";
      const remaining = known ? Math.max(0, 100 - window.used_percent) : null;
      const title = window.window_minutes
        ? `${window.bucket} · ${window.window_minutes >= 1440 ? `${window.window_minutes / 1440}d` : `${window.window_minutes / 60}h`}`
        : window.bucket;
      const value = element(
        "span",
        remaining === null ? "—" : `${Math.round(remaining)}%`,
        "value",
      );
      if (remaining !== null) value.append(element("small", " left"));
      heading.append(element("span", title), value);
      section.append(heading);
      if (remaining !== null) {
        const bar = element(
          "progress",
          undefined,
          `bar${remaining < 20 ? " low" : ""}${window.stale ? " stale" : ""}`,
        );
        bar.max = 100;
        bar.value = remaining;
        bar.setAttribute(
          "aria-label",
          `${window.bucket}: ${Math.round(remaining)} percent remaining`,
        );
        section.append(bar);
      } else if (
        window.remaining_amount !== null &&
        window.remaining_amount !== undefined &&
        !window.disabled
      ) {
        section.append(
          element("p", `${window.remaining_amount} units remaining`, "reset"),
        );
      } else
        section.append(
          element(
            "p",
            window.disabled
              ? "Not available on this plan"
              : "Remaining allowance unknown",
            "reset",
          ),
        );
      section.append(
        element(
          "p",
          window.resets_at
            ? `Reset: ${date(window.resets_at)}`
            : "Reset time not reported",
          "reset",
        ),
      );
      if (window.stale)
        section.append(
          element(
            "p",
            "Last known value · awaiting a fresh observation",
            "note",
          ),
        );
      card.append(section);
    }
    card.append(
      element(
        "p",
        row.observed_at
          ? `Observed ${date(row.observed_at)}`
          : "No successful observation yet",
        "observed",
      ),
    );
    cards.append(card);
  }
}
async function reload() {
  try {
    const response = await fetch("/api/usage", {
      cache: "no-store",
      credentials: "omit",
    });
    if (!response.ok) throw new Error();
    snapshot = await response.json();
    const selected = profile.value;
    profile.replaceChildren(new Option("All profiles", ""));
    for (const name of [...new Set(snapshot.providers.map((r) => r.profile))])
      profile.add(new Option(name, name));
    profile.value = selected;
    status.textContent =
      "Saved snapshots loaded. Each allowance shows when it was observed.";
    render();
  } catch {
    status.textContent =
      "Could not reload snapshots. Previously displayed values may be stale.";
    snapshot.providers.forEach((row) => {
      row.stale = true;
      row.windows.forEach((w) => {
        w.stale = true;
      });
    });
    render();
  }
}
profile.addEventListener("change", render);
document.getElementById("reload").addEventListener("click", reload);
reload();
setInterval(reload, 30000);
