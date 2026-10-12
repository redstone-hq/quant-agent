"use strict";
// The token lives in this variable only: no cookie, no storage; a reload forgets it.
let token = "";
let pending = null;
const $ = (id) => document.getElementById(id);
const WORDS = { stop: "Stop the desk", freeze: "Freeze the desk", start: "Start the desk" };

document.querySelectorAll("#choose button").forEach((b) =>
  b.addEventListener("click", () => {
    const typed = $("token").value.trim();
    if (typed) { token = typed; $("token").value = ""; $("token").placeholder = "token held"; }
    if (!token) { $("result").textContent = "Enter the owner token first."; return; }
    pending = b.dataset.action;
    $("question").textContent = WORDS[pending] + "?";
    $("choose").hidden = true; $("confirm").hidden = false; $("result").textContent = "";
  })
);

function reset() { pending = null; $("confirm").hidden = true; $("choose").hidden = false; }
$("no").addEventListener("click", reset);
$("yes").addEventListener("click", async () => {
  const action = pending; reset();
  $("result").textContent = "Sending...";
  try {
    const r = await fetch("/action", {
      method: "POST", credentials: "omit", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Owner-Token": token },
      body: JSON.stringify({ action }),
    });
    const d = await r.json();
    if (r.status === 403) token = "";
    $("result").textContent = d.ok ? `${WORDS[action]}: recorded (#${d.intent}); the desk acts at its next pickup.`
                                   : `Refused: ${d.error}`;
  } catch (e) {
    $("result").textContent = "Not sent (no connection). Nothing changed.";
  }
});
