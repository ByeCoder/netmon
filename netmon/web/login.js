document.getElementById("f").addEventListener("submit", async function (e) {
  e.preventDefault();
  var btn = document.getElementById("go"), err = document.getElementById("err");
  btn.disabled = true; err.textContent = "";
  try {
    var r = await fetch("/login", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user: document.getElementById("u").value.trim(), password: document.getElementById("p").value }) });
    var j = {};
    try { j = await r.json(); } catch (x) {}
    if (r.ok && j.ok) { location.href = "/"; return; }
    err.textContent = j.error || "Sign-in failed";
  } catch (x) {
    err.textContent = "Can't reach the server";
  }
  btn.disabled = false;
});
