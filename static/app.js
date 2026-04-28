(function () {
  "use strict";

  const cfgEl = document.getElementById("app-config");
  const cfg = cfgEl ? JSON.parse(cfgEl.textContent || "{}") : {};
  const supported = new Set(
    (cfg.supportedExtensions || []).map(function (e) {
      return String(e).toLowerCase();
    })
  );

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const browseBtn = document.getElementById("browse-btn");
  const presetSelect = document.getElementById("preset");
  const jobsList = document.getElementById("jobs-list");
  const audioSelect = document.getElementById("audio");

  function extOf(name) {
    const i = name.lastIndexOf(".");
    return i >= 0 ? name.slice(i + 1).toLowerCase() : "";
  }

  function isSupportedFile(file) {
    const ext = extOf(file.name);
    return supported.has(ext);
  }

  function fmtBytes(n) {
    if (n == null || Number.isNaN(n)) return "—";
    let v = Number(n);
    const units = ["B", "KB", "MB", "GB", "TB"];
    let u = 0;
    while (Math.abs(v) >= 1024 && u < units.length - 1) {
      v /= 1024;
      u += 1;
    }
    return v.toFixed(u === 0 ? 0 : 1) + " " + units[u];
  }

  function stateLabel(state) {
    switch (state) {
      case "queued":
        return "Queued…";
      case "probing":
        return "Probing…";
      case "encoding":
        return "Encoding…";
      case "done":
        return "Done";
      case "cancelled":
        return "Cancelled";
      case "error":
        return "Error";
      default:
        return state || "…";
    }
  }

  function syncAudioBitrateVisibility() {
    const wrap = document.getElementById("audio-bitrate-wrap");
    const audioEl = document.getElementById("audio");
    if (!wrap || !audioEl) return;
    const v = audioEl.value;
    wrap.classList.toggle("d-none", v === "none" || v === "copy");
  }

  if (audioSelect) {
    audioSelect.addEventListener("change", syncAudioBitrateVisibility);
    syncAudioBitrateVisibility();
  }

  function appendEncodeOptions(fd) {
    const container = document.getElementById("container");
    const ffmpegPreset = document.getElementById("ffmpeg_preset");
    const audio = document.getElementById("audio");
    const audioBitrate = document.getElementById("audio_bitrate");
    const crf = document.getElementById("crf");
    const codec = document.getElementById("codec");
    const keep = document.getElementById("keep");
    const hwaccel = document.getElementById("hwaccel");
    if (container) fd.append("container", container.value);
    if (ffmpegPreset) fd.append("ffmpeg_preset", ffmpegPreset.value);
    if (audio) fd.append("audio", audio.value);
    if (audioBitrate) fd.append("audio_bitrate", audioBitrate.value);
    if (crf) fd.append("crf", crf.value);
    if (codec) fd.append("codec", codec.value);
    if (keep && keep.checked) fd.append("keep", "true");
    if (hwaccel && hwaccel.checked) fd.append("hwaccel", "true");
  }

  function createJobCard(filename, sizeBytes) {
    const wrap = document.createElement("div");
    wrap.className = "card shadow-sm state-encoding";
    wrap.innerHTML =
      '<div class="card-body">' +
      '<div class="d-flex justify-content-between align-items-start gap-2 flex-wrap">' +
      '<div class="flex-grow-1 min-w-0">' +
      '<div class="fw-semibold job-title text-truncate"></div>' +
      '<div class="small text-muted job-meta"></div>' +
      '<div class="small mt-2 job-sizes"><span class="text-muted">Before:</span> <span class="job-before"></span>' +
      ' <span class="text-muted ms-2">After:</span> <span class="job-after">—</span></div>' +
      "</div>" +
      '<div class="job-actions d-flex gap-2 align-items-center flex-shrink-0"></div>' +
      "</div>" +
      '<div class="progress mt-3" role="progressbar" aria-valuemin="0" aria-valuemax="100">' +
      '<div class="progress-bar progress-bar-striped progress-bar-animated job-bar" style="width: 0%"></div>' +
      "</div>" +
      '<div class="small mt-2 job-status text-muted"></div>' +
      '<div class="small mt-1 job-message text-warning"></div>' +
      '<div class="small mt-1 job-path text-secondary font-monospace"></div>' +
      "</div>";
    wrap.querySelector(".job-title").textContent = filename;
    wrap.querySelector(".job-meta").textContent = "Source: " + fmtBytes(sizeBytes);
    wrap.querySelector(".job-before").textContent = fmtBytes(sizeBytes);
    wrap.querySelector(".job-status").textContent = "Starting…";
    return wrap;
  }

  function setCardState(card, state) {
    card.classList.remove("state-encoding", "state-done", "state-error", "state-cancelled");
    if (state === "done") card.classList.add("state-done");
    else if (state === "error") card.classList.add("state-error");
    else if (state === "cancelled") card.classList.add("state-cancelled");
    else card.classList.add("state-encoding");
  }

  function updateJobCard(card, data) {
    const bar = card.querySelector(".job-bar");
    const statusEl = card.querySelector(".job-status");
    const msgEl = card.querySelector(".job-message");
    const pathEl = card.querySelector(".job-path");
    const actions = card.querySelector(".job-actions");
    const beforeEl = card.querySelector(".job-before");
    const afterEl = card.querySelector(".job-after");

    const pct = Math.min(100, Math.max(0, Number(data.percent) || 0));
    bar.style.width = pct + "%";
    bar.setAttribute("aria-valuenow", String(Math.round(pct)));

    if (beforeEl && data.original_bytes != null) {
      beforeEl.textContent = fmtBytes(data.original_bytes);
    }
    if (afterEl) {
      if (data.new_bytes != null) {
        afterEl.textContent = fmtBytes(data.new_bytes);
      } else if (data.state === "done" || data.state === "error" || data.state === "cancelled") {
        afterEl.textContent = "—";
      } else {
        afterEl.textContent = "…";
      }
    }

    statusEl.textContent = stateLabel(data.state);
    if (data.summary) {
      statusEl.textContent = data.summary;
    }

    msgEl.textContent = "";
    pathEl.textContent = "";
    actions.innerHTML = "";

    const jobId = card.dataset.jobId;
    const active = data.state === "queued" || data.state === "probing" || data.state === "encoding";
    if (active && jobId) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-sm btn-outline-danger";
      btn.textContent = "Cancel";
      btn.addEventListener("click", function () {
        fetch("/cancel/" + encodeURIComponent(jobId), { method: "POST" })
          .then(function (r) {
            return r.json().then(function (j) {
              if (!r.ok) throw new Error(j.error || r.statusText);
              return j;
            });
          })
          .catch(function () {
            /* ignore */
          });
      });
      actions.appendChild(btn);
    }

    if (data.message) {
      msgEl.textContent = data.message;
    }

    if (data.state === "done" && data.download_url) {
      const a = document.createElement("a");
      a.className = "btn btn-sm btn-primary";
      a.href = data.download_url;
      a.textContent = "Download";
      a.setAttribute("download", "");
      actions.appendChild(a);
    }

    if (data.output_relpath) {
      pathEl.textContent = "Saved on server: " + data.output_relpath;
    }

    if (data.state === "error" && data.error) {
      msgEl.textContent = data.error;
      msgEl.classList.remove("text-warning");
      msgEl.classList.add("text-danger");
    } else if (data.state === "cancelled") {
      msgEl.classList.remove("text-danger");
      msgEl.classList.add("text-warning");
    } else {
      msgEl.classList.add("text-warning");
      msgEl.classList.remove("text-danger");
    }

    if (data.state === "done" || data.state === "error" || data.state === "cancelled") {
      bar.classList.remove("progress-bar-animated", "progress-bar-striped");
      setCardState(card, data.state);
    } else {
      bar.classList.add("progress-bar-striped", "progress-bar-animated");
      setCardState(card, "encoding");
    }
  }

  function startPolling(jobId, card) {
    const tick = function () {
      fetch("/status/" + encodeURIComponent(jobId))
        .then(function (r) {
          if (!r.ok) throw new Error("Status " + r.status);
          return r.json();
        })
        .then(function (data) {
          updateJobCard(card, data);
          if (data.state === "done" || data.state === "error" || data.state === "cancelled") {
            clearInterval(iv);
          }
        })
        .catch(function (e) {
          clearInterval(iv);
          updateJobCard(card, {
            state: "error",
            percent: 0,
            error: e && e.message ? e.message : "Status request failed",
          });
        });
    };
    tick();
    const iv = setInterval(tick, 1000);
  }

  function uploadFile(file) {
    if (!isSupportedFile(file)) {
      window.alert(
        "Skipping unsupported type: " +
          file.name +
          "\nAllowed: " +
          Array.from(supported).sort().join(", ")
      );
      return;
    }

    const card = createJobCard(file.name, file.size);
    jobsList.prepend(card);

    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("preset", presetSelect.value);
    appendEncodeOptions(fd);

    fetch("/upload", { method: "POST", body: fd })
      .then(function (r) {
        return r.json().then(function (j) {
          if (!r.ok) throw new Error(j.error || r.statusText || "Upload failed");
          return j;
        });
      })
      .then(function (j) {
        if (!j.job_id) throw new Error("No job_id in response");
        card.dataset.jobId = j.job_id;
        updateJobCard(card, { state: "queued", percent: 0, original_bytes: file.size });
        startPolling(j.job_id, card);
      })
      .catch(function (e) {
        updateJobCard(card, {
          state: "error",
          percent: 0,
          error: e && e.message ? e.message : "Upload failed",
          original_bytes: file.size,
        });
      });
  }

  function handleFiles(fileList) {
    const files = Array.prototype.slice.call(fileList || [], 0);
    files.forEach(uploadFile);
  }

  if (browseBtn && fileInput) {
    browseBtn.addEventListener("click", function () {
      fileInput.click();
    });
  }

  if (fileInput) {
    fileInput.addEventListener("change", function () {
      handleFiles(fileInput.files);
      fileInput.value = "";
    });
  }

  if (dropzone && fileInput) {
    dropzone.addEventListener("click", function () {
      fileInput.click();
    });

    ["dragenter", "dragover"].forEach(function (ev) {
      dropzone.addEventListener(ev, function (e) {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add("dragover");
      });
    });

    ["dragleave", "drop"].forEach(function (ev) {
      dropzone.addEventListener(ev, function (e) {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove("dragover");
      });
    });

    dropzone.addEventListener("drop", function (e) {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) {
        handleFiles(dt.files);
      }
    });
  }
})();
