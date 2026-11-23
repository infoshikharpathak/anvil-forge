// All UI writes go through the /v1 API with the CSRF header
// (a cross-site form can't send custom headers).
async function api(method, url, body, raw) {
  const opts = { method, headers: { "X-Garrison": "1" }, credentials: "same-origin" };
  if (raw !== undefined) { opts.body = raw; opts.headers["Content-Type"] = "application/yaml"; }
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await fetch(url, opts);
  const isJson = (r.headers.get("content-type") || "").includes("json");
  const data = isJson ? await r.json() : await r.text();
  if (!r.ok) {
    let d = isJson ? data.detail : data;
    if (Array.isArray(d)) d = d.map(e => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("\n");
    throw new Error(d || r.statusText);
  }
  return data;
}

function say(el, text, ok) {
  if (!el) return;
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "err");
}

// Forms declare what they do: data-api="POST /v1/apps", data-body="json|yaml|fields", data-then="reload|<url>"
document.addEventListener("submit", async (e) => {
  const form = e.target.closest("form[data-api]");
  if (!form) return;
  e.preventDefault();
  const [method, url] = form.dataset.api.split(" ");
  const status = form.querySelector(".msg");
  try {
    let result;
    if (form.dataset.body === "yaml") {
      result = await api(method, url, undefined, form.querySelector("textarea").value);
    } else if (form.dataset.body === "json") {
      result = await api(method, url, JSON.parse(form.querySelector("textarea").value));
    } else {
      const data = {};
      for (const el of form.querySelectorAll("[name]")) {
        if (el.dataset.type === "json") { if (el.value.trim()) Object.assign(data, JSON.parse(el.value)); }
        else if (el.dataset.type === "int") data[el.name] = parseInt(el.value, 10);
        else if (el.value !== "" || el.dataset.keepEmpty) data[el.name] = el.value;
      }
      result = await api(method, url, data);
    }
    if (form.dataset.then === "showkey") {
      const box = document.getElementById("new-key");
      box.hidden = false;
      box.querySelector("code").textContent = result.key;
      say(status, "Key created. Copy it now: it won't be shown again.", true);
      return;
    }
    say(status, "Saved.", true);
    const then = form.dataset.then || "reload";
    setTimeout(() => { then === "reload" ? location.reload() : (location.href = then); }, 300);
  } catch (err) {
    say(status, err.message, false);
  }
});

// Buttons for one-click actions: data-api="DELETE /v1/...", data-body='{"version": 2}', data-confirm="..."
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-api]");
  if (!btn) return;
  if (btn.dataset.confirm && !btn.dataset.armed) {  // two-step confirm without window.confirm
    btn.dataset.armed = "1";
    btn.dataset.label = btn.textContent;
    btn.textContent = btn.dataset.confirm;
    setTimeout(() => { delete btn.dataset.armed; btn.textContent = btn.dataset.label; }, 4000);
    return;
  }
  const [method, url] = btn.dataset.api.split(" ");
  const status = btn.closest("[data-status]")?.querySelector(".msg") || document.getElementById("page-msg");
  try {
    await api(method, url, btn.dataset.body ? JSON.parse(btn.dataset.body) : undefined);
    if (btn.dataset.then) location.href = btn.dataset.then; else location.reload();
  } catch (err) {
    say(status, err.message, false);
  }
});
