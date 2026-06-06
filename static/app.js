"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
};

const STATUS_LABEL = { missing: "missing", too_small: "too small", unknown: "unknown", error: "error", ok: "ok" };
const STATUS_CLASS = { missing: "bad", too_small: "warn", unknown: "bad", error: "bad", ok: "ok" };

const els = {
  root: $("#musicRoot"),
  minSize: $("#minSize"),
  backup: $("#backup"),
  scanBtn: $("#scanBtn"),
  status: $("#status"),
  progress: $("#progress"),
  progressBar: $("#progressBar"),
  results: $("#results"),
  tpl: $("#albumTpl"),
};

let pollTimer = null;

function setStatus(msg, isError = false) {
  els.status.textContent = msg;
  els.status.classList.toggle("error", isError);
}

async function loadSettings() {
  try {
    const cfg = await api("/api/settings");
    els.root.value = cfg.music_root || "";
    els.minSize.value = cfg.min_size || 800;
  } catch (e) { /* ignore */ }
}

async function startScan() {
  els.scanBtn.disabled = true;
  els.results.innerHTML = "";
  els.progress.classList.remove("hidden");
  els.progressBar.style.width = "0%";
  setStatus("Starting scan…");
  try {
    await api("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        music_root: els.root.value,
        min_size: parseInt(els.minSize.value, 10) || 800,
      }),
    });
    poll();
  } catch (e) {
    setStatus(e.message, true);
    els.scanBtn.disabled = false;
    els.progress.classList.add("hidden");
  }
}

function poll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const s = await api("/api/scan");
      if (s.total > 0) {
        const pct = Math.round((s.scanned / s.total) * 100);
        els.progressBar.style.width = pct + "%";
        setStatus(`Scanning… ${s.scanned} / ${s.total} files (${pct}%)`);
      } else {
        setStatus(s.message || "Scanning…");
      }
      if (s.state === "scanning") {
        poll();
      } else if (s.state === "done") {
        finishScan(s.albums || []);
      } else if (s.state === "error") {
        setStatus("Scan failed: " + (s.error || "unknown error"), true);
        els.scanBtn.disabled = false;
        els.progress.classList.add("hidden");
      }
    } catch (e) {
      setStatus("Lost connection: " + e.message, true);
      els.scanBtn.disabled = false;
    }
  }, 700);
}

function finishScan(albums) {
  els.scanBtn.disabled = false;
  els.progress.classList.add("hidden");
  if (!albums.length) {
    setStatus("All folders have covers ≥ the minimum size. Nothing to fix 🎉");
    els.results.innerHTML = `<div class="empty">No folders need attention.</div>`;
    return;
  }
  const problems = albums.reduce((n, a) => n + a.n_problem, 0);
  setStatus(`${albums.length} folder(s) need attention — ${problems} file(s) with missing or small covers.`);
  els.results.innerHTML = "";
  albums.forEach(renderAlbum);
}

function statusCounts(files) {
  const c = {};
  files.forEach((f) => (c[f.status] = (c[f.status] || 0) + 1));
  return c;
}

function renderAlbum(album) {
  const node = els.tpl.content.cloneNode(true);
  const root = $(".album", node);
  $(".album-title", node).textContent = album.album || "(unknown album)";
  $(".album-artist", node).textContent = album.artist || "(unknown artist)";
  $(".album-folder", node).textContent = album.folder;

  const img = $(".thumb .current", node);
  const noart = $(".thumb .noart", node);
  if (album.has_cover) {
    img.src = `/api/albums/${album.id}/current-cover`;
    img.onload = () => { img.style.display = "block"; noart.style.display = "none"; };
    img.onerror = () => { img.style.display = "none"; noart.style.display = "block"; };
  }

  const badges = $(".badges", node);
  const counts = statusCounts(album.files);
  Object.entries(counts).forEach(([st, n]) => {
    const b = document.createElement("span");
    b.className = "badge " + (STATUS_CLASS[st] || "");
    b.textContent = `${n} ${STATUS_LABEL[st] || st}`;
    badges.appendChild(b);
  });
  const toggle = document.createElement("span");
  toggle.className = "badge link";
  toggle.textContent = `${album.n_files} track(s) — show files`;
  toggle.onclick = () => $(".files", node).classList.toggle("hidden");
  badges.appendChild(toggle);

  const filesEl = $(".files", node);
  album.files.forEach((f) => {
    const row = document.createElement("div");
    row.className = "file-row";
    const dim = f.w && f.h ? `${f.w}×${f.h}` : "";
    row.innerHTML = `<span>${escapeHtml(f.name)}</span><span class="st st-${f.status}">${(STATUS_LABEL[f.status] || f.status)} ${dim}</span>`;
    filesEl.appendChild(row);
  });

  const findBtn = $(".find", node);
  const customBtn = $(".custom", node);
  const uploadBtn = $(".upload", node);
  const uploadInput = $(".upload-input", node);
  const patchBtn = $(".patch", node);
  const candEl = $(".candidates", node);
  const resultEl = $(".result", node);
  const customBox = $(".custom-search", node);
  const customInput = $(".custom-input", node);
  const customGo = $(".custom-go", node);
  let selectedToken = null;
  let moreBtn = null;

  function addCandidateCard(c) {
    const card = document.createElement("div");
    card.className = "cand";
    card.innerHTML = `
      <img src="${c.url}" loading="lazy" alt="" />
      <div class="info">
        <div class="src">${c.source}</div>
        <div class="dim ${c.meets ? "" : "small"}">${c.width}×${c.height}${c.meets ? "" : " ⚠"}</div>
      </div>`;
    card.title = [c.title, c.artist].filter(Boolean).join(" — ");
    card.onclick = () => {
      candEl.querySelectorAll(".cand").forEach((x) => x.classList.remove("selected"));
      card.classList.add("selected");
      selectedToken = c.token;
      patchBtn.disabled = false;
    };
    candEl.appendChild(card);
    return card;
  }

  function updateMoreButton(hasMore) {
    if (hasMore) {
      if (!moreBtn) {
        const row = document.createElement("div");
        row.className = "more-row";
        moreBtn = document.createElement("button");
        moreBtn.className = "more";
        moreBtn.onclick = () => loadCandidates(true);
        row.appendChild(moreBtn);
        candEl.insertAdjacentElement("afterend", row);
      }
      moreBtn.textContent = "Find more covers";
      moreBtn.disabled = false;
    } else if (moreBtn) {
      moreBtn.textContent = "No more covers found";
      moreBtn.disabled = true;
    }
  }

  async function loadCandidates(more, customQ) {
    findBtn.disabled = true;
    if (moreBtn) {
      moreBtn.disabled = true;
      if (more) moreBtn.textContent = "Searching…";
    }
    if (!more) {
      const what = customQ ? `“${escapeHtml(customQ)}”` : "iTunes, Deezer & Cover Art Archive";
      candEl.innerHTML = `<div class="spinner">Searching ${what}…</div>`;
      selectedToken = null;
      patchBtn.disabled = true;
    }
    try {
      const params = [];
      if (more) params.push("more=1");
      if (!more && customQ) params.push("q=" + encodeURIComponent(customQ));
      const qs = params.length ? "?" + params.join("&") : "";
      const data = await api(`/api/albums/${album.id}/candidates${qs}`);
      if (!more) {
        candEl.innerHTML = "";
        findBtn.textContent = "Search again";
      }
      if (!more && !data.candidates.length) {
        candEl.innerHTML = `<div class="spinner">No covers found. Try editing the album/artist tags.</div>`;
      }
      if (more && !data.candidates.length && moreBtn) {
        moreBtn.textContent = "No more covers found";
        moreBtn.disabled = true;
        return;
      }
      data.candidates.forEach(addCandidateCard);
      updateMoreButton(data.has_more);
    } catch (e) {
      if (!more) {
        candEl.innerHTML = `<div class="spinner" style="color:var(--bad)">Error: ${escapeHtml(e.message)}</div>`;
      } else if (moreBtn) {
        moreBtn.textContent = "Find more covers";
        moreBtn.disabled = false;
      }
    } finally {
      findBtn.disabled = false;
    }
  }

  findBtn.onclick = () => loadCandidates(false);

  customBtn.onclick = () => {
    customBox.classList.toggle("hidden");
    if (!customBox.classList.contains("hidden")) {
      if (!customInput.value) customInput.value = album.query || "";
      customInput.focus();
      customInput.select();
    }
  };
  function runCustom() {
    const q = customInput.value.trim();
    if (!q) return;
    loadCandidates(false, q);
  }
  customGo.onclick = runCustom;
  customInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); runCustom(); }
  });

  uploadBtn.onclick = () => uploadInput.click();
  uploadInput.onchange = async () => {
    const f = uploadInput.files[0];
    if (!f) return;
    uploadBtn.disabled = true;
    const original = uploadBtn.textContent;
    uploadBtn.textContent = "Uploading…";
    try {
      const fd = new FormData();
      fd.append("file", f);
      const c = await api("/api/upload", { method: "POST", body: fd });
      candEl.querySelectorAll(".spinner").forEach((x) => x.remove());
      const card = addCandidateCard(c);
      card.click(); // auto-select the uploaded image
      card.scrollIntoView({ block: "nearest" });
    } catch (e) {
      resultEl.classList.remove("hidden", "success");
      resultEl.classList.add("fail");
      resultEl.textContent = "Upload failed: " + e.message;
    } finally {
      uploadBtn.disabled = false;
      uploadBtn.textContent = original;
      uploadInput.value = ""; // allow re-selecting the same file
    }
  };

  patchBtn.onclick = async () => {
    if (!selectedToken) return;
    patchBtn.disabled = true;
    findBtn.disabled = true;
    resultEl.classList.remove("hidden", "success", "fail");
    resultEl.textContent = `Patching ${album.n_files} track(s)…`;
    try {
      const res = await api(`/api/albums/${album.id}/patch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: selectedToken, backup: els.backup.checked }),
      });
      const okCount = res.results.filter((r) => r.ok).length;
      if (res.ok) {
        resultEl.classList.add("success");
        resultEl.textContent = `✓ Patched ${okCount}/${res.results.length} tracks with a ${res.cover.w}×${res.cover.h} JPEG (${Math.round(res.cover.bytes / 1024)} KB, q${res.cover.quality}).`;
        root.classList.add("done");
        img.src = `/api/candidate/${selectedToken}?t=` + Date.now();
        img.style.display = "block";
        noart.style.display = "none";
      } else {
        resultEl.classList.add("fail");
        const failed = res.results.filter((r) => !r.ok);
        resultEl.textContent = `Patched ${okCount}/${res.results.length}. Failed: ` +
          failed.map((r) => `${r.name} (${r.error})`).join("; ");
      }
    } catch (e) {
      resultEl.classList.remove("hidden");
      resultEl.classList.add("fail");
      resultEl.textContent = "Error: " + e.message;
    } finally {
      findBtn.disabled = false;
      patchBtn.disabled = false;
    }
  };

  els.results.appendChild(node);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

els.scanBtn.addEventListener("click", startScan);
loadSettings();
