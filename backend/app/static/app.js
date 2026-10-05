"use strict";

const $ = (id) => document.getElementById(id);
const storage = {
  get(key) {
    try {
      return sessionStorage.getItem(`isojam.${key}`);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      sessionStorage.setItem(`isojam.${key}`, value);
    } catch {
      /* In-memory login still works. */
    }
  },
  remove(key) {
    try {
      sessionStorage.removeItem(`isojam.${key}`);
    } catch {
      /* Nothing persisted. */
    }
  },
};
const state = {
  token: storage.get("token"),
  email: storage.get("email"),
  savedJob: storage.get("job"),
  info: null,
  authMode: "login",
  authBusy: false,
  busy: false,
  epoch: 0,
  jobEpoch: 0,
  file: null,
  uploadId: null,
  key: null,
  accepted: false,
  job: null,
  timer: null,
  previewUrl: null,
  previewTurn: 0,
  downloading: false,
  downloadUrls: new Set(),
};
const stemOrder = [
  "vocals",
  "drums",
  "bass",
  "guitar",
  "piano",
  "other",
  "instrumental",
];
const descriptions = {
  vocals: "The voice at the center",
  drums: "Find the groove",
  bass: "Follow the low end",
  guitar: "Hear every phrase",
  piano: "Keys and chords",
  other: "Everything in between",
  instrumental: "A track without the vocals",
};
class CancelledOperation extends Error {}

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "notice error" : "notice";
  $("notice").hidden = !message;
}
function report(error, epoch) {
  if (epoch === state.epoch && !(error instanceof CancelledOperation))
    notice(error.message, true);
}
function clearPreview() {
  state.previewTurn += 1;
  $("player").pause();
  $("player").removeAttribute("src");
  $("player").load();
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
  $("player-panel").hidden = true;
}
function clearJob() {
  clearTimeout(state.timer);
  state.jobEpoch += 1;
  state.job = null;
  state.uploadId = null;
  state.key = null;
  state.accepted = false;
  $("stems").replaceChildren();
  $("active-session").hidden = true;
  $("empty-session").hidden = false;
  $("job-status").hidden = true;
  clearPreview();
}
function clearSession() {
  state.epoch += 1;
  state.token = null;
  state.email = null;
  state.savedJob = null;
  state.busy = false;
  state.authBusy = false;
  state.downloading = false;
  for (const key of ["token", "email", "job"]) storage.remove(key);
  for (const url of state.downloadUrls) URL.revokeObjectURL(url);
  state.downloadUrls.clear();
  clearJob();
  state.file = null;
  $("audio-file").value = "";
  $("file-name").textContent = "Choose a WAV recording";
  $("file-detail").textContent = "Select a song from your computer";
  $("resume-id").value = "";
  $("password").value = "";
  updateControls();
}
function updateControls() {
  const signedIn = Boolean(state.token);
  const active =
    state.job && ["pending", "processing"].includes(state.job.status);
  $("signed-in").hidden = !signedIn;
  $("signed-out").hidden = signedIn;
  $("account-email").textContent = state.email || "";
  $("auth-submit").disabled = state.authBusy;
  $("auth-submit").textContent = state.authBusy
    ? "Please wait…"
    : state.authMode === "login"
      ? "Sign in"
      : "Create account";
  $("audio-file").disabled =
    !signedIn ||
    !state.info ||
    state.busy ||
    Boolean(active) ||
    state.info?.processing_mode === "disabled";
  $("separate").disabled =
    !signedIn ||
    !state.file ||
    !state.info ||
    state.info.processing_mode === "disabled" ||
    state.busy ||
    Boolean(active) ||
    state.accepted;
  $("separate").textContent = state.busy
    ? "Submitting…"
    : state.accepted
      ? "Choose another recording"
      : state.uploadId
        ? "Retry submission ↗"
        : "Separate audio ↗";
  $("resume-submit").disabled = !signedIn || state.busy;
}
async function jsonRequest(path, { method = "GET", body, auth = true } = {}) {
  const token = state.token;
  const headers = {};
  if (auth && token) headers.Authorization = `Bearer ${token}`;
  if (body && !(body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, { method, body, headers, cache: "no-store" });
  } catch {
    throw new Error(
      "Could not reach the server. Check your connection and retry the same submission.",
    );
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (auth && response.status === 401 && token === state.token) {
      clearSession();
      notice(
        "Your sign-in expired. Sign in again, then open the session using its job ID.",
        true,
      );
      throw new CancelledOperation();
    }
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => item.msg).join(". ")
      : data.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : `Request failed (${response.status}).`,
    );
  }
  return data;
}
function setAuthMode(mode) {
  if (state.authBusy) return;
  state.authMode = mode;
  $("login-tab").classList.toggle("selected", mode === "login");
  $("register-tab").classList.toggle("selected", mode === "register");
  $("login-tab").setAttribute("aria-pressed", String(mode === "login"));
  $("register-tab").setAttribute("aria-pressed", String(mode === "register"));
  $("password").autocomplete =
    mode === "login" ? "current-password" : "new-password";
  $("password").minLength = mode === "register" ? 8 : 1;
  updateControls();
}
$("login-tab").addEventListener("click", () => setAuthMode("login"));
$("register-tab").addEventListener("click", () => setAuthMode("register"));
$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.authBusy) return;
  const epoch = state.epoch;
  state.authBusy = true;
  updateControls();
  notice("");
  const credentials = {
    email: $("email").value.trim(),
    password: $("password").value,
  };
  try {
    if (state.authMode === "register")
      await jsonRequest("/register", {
        method: "POST",
        body: credentials,
        auth: false,
      });
    const result = await jsonRequest("/login", {
      method: "POST",
      body: credentials,
      auth: false,
    });
    if (epoch !== state.epoch) return;
    state.token = result.access_token;
    state.email = credentials.email.toLowerCase();
    storage.set("token", state.token);
    storage.set("email", state.email);
    $("password").value = "";
    notice("Welcome in. Choose a recording to start your session.");
  } catch (error) {
    report(error, epoch);
  } finally {
    if (epoch === state.epoch) {
      state.authBusy = false;
      updateControls();
    }
  }
});
$("logout").addEventListener("click", () => {
  clearSession();
  notice("Signed out. Accepted jobs continue processing on the server.");
});
$("audio-file").addEventListener("change", () => {
  clearJob();
  state.savedJob = null;
  storage.remove("job");
  const file = $("audio-file").files[0];
  state.file = null;
  notice("");
  $("file-name").textContent = file?.name || "Choose a WAV recording";
  $("file-detail").textContent = file
    ? `${(file.size / 1024 / 1024).toFixed(1)} MiB · WAV recording`
    : "Select a song from your computer";
  if (file) {
    if (!file.name.toLowerCase().endsWith(".wav"))
      notice("Please choose a WAV recording.", true);
    else if (!file.size || file.size > state.info.max_upload_bytes)
      notice("This file is empty or exceeds the upload limit.", true);
    else {
      state.file = file;
      state.key = crypto.randomUUID();
    }
  }
  updateControls();
});
$("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (
    !state.file ||
    !state.token ||
    !state.info ||
    state.info.processing_mode === "disabled" ||
    state.busy ||
    state.accepted ||
    ["pending", "processing"].includes(state.job?.status)
  )
    return;
  const epoch = state.epoch;
  state.busy = true;
  updateControls();
  notice("");
  try {
    if (!state.uploadId) {
      const body = new FormData();
      body.append(
        "audio_file",
        state.file.slice(0, state.file.size, "audio/wav"),
        state.file.name,
      );
      const upload = await jsonRequest("/uploads", { method: "POST", body });
      if (epoch !== state.epoch) return;
      state.uploadId = upload.id;
    }
    // Preserve this key after an ambiguous response; retry retrieves the original job.
    const response = await fetch("/jobs", {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${state.token}`,
        "Idempotency-Key": state.key,
      },
      body: JSON.stringify({ upload_id: state.uploadId }),
    });
    const data = await response.json().catch(() => null);
    if (epoch !== state.epoch) return;
    if (response.status === 401) {
      clearSession();
      notice("Your sign-in expired. Sign in again to continue.", true);
      return;
    }
    if (!response.ok)
      throw new Error(
        typeof data?.detail === "string"
          ? data.detail
          : `Submission failed (${response.status}).`,
      );
    if (
      !data ||
      typeof data.id !== "string" ||
      !["pending", "processing", "completed", "failed"].includes(data.status) ||
      !data.outputs ||
      typeof data.outputs !== "object"
    )
      throw new Error(
        "The submission response was incomplete. Retry submission to retrieve the same job.",
      );
    state.accepted = true;
    state.jobEpoch += 1;
    showJob(data);
    notice(
      "Your session was accepted. Save its job ID so you can return later.",
    );
    schedulePoll();
  } catch (error) {
    if (error instanceof TypeError)
      error = new Error(
        "The submission response was lost. Retry submission to retrieve the same job.",
      );
    report(error, epoch);
  } finally {
    if (epoch === state.epoch) {
      state.busy = false;
      updateControls();
    }
  }
});
function showJob(job) {
  state.job = job;
  state.savedJob = job.id;
  storage.set("job", job.id);
  $("resume-id").value = job.id;
  $("job-id").textContent = job.id;
  $("empty-session").hidden = true;
  $("active-session").hidden = false;
  $("job-status").hidden = false;
  $("job-status").textContent = job.status;
  $("job-status").className = `status-badge ${job.status}`;
  const active = ["pending", "processing"].includes(job.status);
  $("processing-indicator").hidden = !active;
  $("job-message").textContent =
    {
      pending: "Your recording is waiting to be processed.",
      processing:
        "Separating your recording. You can leave this tab and return using the job ID.",
      completed:
        "Your tracks are ready. Preview an instrument or download its WAV file.",
      failed:
        "This session could not finish. Its job ID is saved above; choose another recording to start a new session.",
    }[job.status] || "Checking your session…";
  if (job.status === "completed") renderStems(job.outputs);
  else {
    $("stems").replaceChildren();
    clearPreview();
  }
  updateControls();
}
function schedulePoll() {
  clearTimeout(state.timer);
  if (!state.job || !["pending", "processing"].includes(state.job.status))
    return;
  const epoch = state.epoch,
    jobEpoch = state.jobEpoch;
  state.timer = setTimeout(() => pollJob(epoch, jobEpoch), 1500);
}
async function pollJob(epoch = state.epoch, jobEpoch = state.jobEpoch) {
  clearTimeout(state.timer);
  if (!state.job || !state.token) return;
  const id = state.job.id;
  try {
    const job = await jsonRequest(`/jobs/${encodeURIComponent(id)}`);
    if (epoch !== state.epoch || jobEpoch !== state.jobEpoch) return;
    showJob(job);
    schedulePoll();
  } catch (error) {
    report(error, epoch);
  }
}
$("refresh-job").addEventListener("click", () => pollJob());
async function openSession(id) {
  if (!state.token) {
    notice("Sign in to open a session.", true);
    return;
  }
  const epoch = state.epoch;
  const jobEpoch = ++state.jobEpoch;
  clearTimeout(state.timer);
  state.busy = true;
  updateControls();
  try {
    const job = await jsonRequest(`/jobs/${encodeURIComponent(id)}`);
    if (epoch !== state.epoch || jobEpoch !== state.jobEpoch) return;
    clearPreview();
    showJob(job);
    notice("");
    schedulePoll();
  } catch (error) {
    report(error, epoch);
  } finally {
    if (epoch === state.epoch) {
      state.busy = false;
      updateControls();
    }
  }
}
$("resume-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!state.busy) openSession($("resume-id").value.trim());
});
$("copy-job").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(state.job.id);
    notice("Job ID copied.");
  } catch {
    notice("Select the job ID above and copy it to return to this session.");
  }
});
async function audioBlob(path, epoch) {
  const url = new URL(path, location.origin);
  if (url.origin !== location.origin || !url.pathname.startsWith("/jobs/"))
    throw new Error("The audio URL is invalid.");
  const token = state.token;
  if (!token) throw new CancelledOperation();
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  if (epoch !== state.epoch) throw new CancelledOperation();
  if (response.status === 401) {
    clearSession();
    notice("Your sign-in expired. Sign in again to listen or download.", true);
    throw new CancelledOperation();
  }
  if (!response.ok)
    throw new Error(
      `Audio is unavailable (${response.status}). Refresh the session and try again.`,
    );
  const blob = await response.blob();
  if (epoch !== state.epoch) throw new CancelledOperation();
  return blob;
}
function renderStems(outputs) {
  $("stems").replaceChildren();
  for (const [stem, path] of Object.entries(outputs).sort(
    ([a], [b]) => stemOrder.indexOf(a) - stemOrder.indexOf(b),
  )) {
    const row = document.createElement("div");
    row.className = "stem-row";
    const label = document.createElement("div");
    const name = document.createElement("span");
    name.className = "stem-name";
    name.textContent = stem;
    const description = document.createElement("span");
    description.className = "stem-description";
    description.textContent = descriptions[stem] || "Separated track";
    label.append(name, description);
    const actions = document.createElement("div");
    actions.className = "stem-actions";
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "button secondary";
    preview.textContent = "Preview";
    preview.setAttribute("aria-label", `Preview ${stem}`);
    preview.addEventListener("click", async () => {
      const epoch = state.epoch;
      clearPreview();
      const turn = state.previewTurn;
      preview.disabled = true;
      try {
        const blob = await audioBlob(path, epoch);
        if (turn !== state.previewTurn) return;
        state.previewUrl = URL.createObjectURL(blob);
        $("player").src = state.previewUrl;
        $("playing-stem").textContent = stem;
        $("player-panel").hidden = false;
        await $("player")
          .play()
          .catch(() => {});
      } catch (error) {
        report(error, epoch);
      } finally {
        preview.disabled = false;
      }
    });
    const download = document.createElement("button");
    download.type = "button";
    download.className = "button secondary download-button";
    download.textContent = "Download ↓";
    download.setAttribute("aria-label", `Download ${stem}`);
    download.addEventListener("click", async () => {
      if (state.downloading) return;
      const epoch = state.epoch,
        jobId = state.job.id;
      state.downloading = true;
      document.querySelectorAll(".download-button").forEach((button) => {
        button.disabled = true;
      });
      try {
        const blob = await audioBlob(path, epoch);
        const url = URL.createObjectURL(blob);
        state.downloadUrls.add(url);
        const link = document.createElement("a");
        link.href = url;
        link.download = `${stem.replace(/[^a-z0-9_-]/gi, "")}-${jobId}.wav`;
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => {
          URL.revokeObjectURL(url);
          state.downloadUrls.delete(url);
        }, 10000);
      } catch (error) {
        report(error, epoch);
      } finally {
        if (epoch === state.epoch) {
          state.downloading = false;
          document.querySelectorAll(".download-button").forEach((button) => {
            button.disabled = false;
          });
        }
      }
    });
    actions.append(preview, download);
    row.append(label, actions);
    $("stems").append(row);
  }
}
async function initialize() {
  updateControls();
  try {
    state.info = await jsonRequest("/ui-info", { auth: false });
    const mode = state.info.processing_mode;
    $("service-status").textContent = {
      local: "Separation ready",
      queued: "Demo queue",
      disabled: "Playback only",
    }[mode];
    $("mode-note").textContent = {
      local: "Choose a recording and let the model find its instrument tracks.",
      queued:
        "This queue currently uses a demonstration worker that produces test audio. Real separation is available in local mode.",
      disabled:
        "New separation is unavailable on this server. Sign in to listen to an existing completed session.",
    }[mode];
    $("upload-limits").textContent =
      `Up to ${Math.floor(state.info.max_upload_bytes / 1024 / 1024)} MiB · ${Math.floor(state.info.max_audio_duration_seconds / 60)} min · mono or stereo`;
    updateControls();
    if (state.token && state.savedJob) await openSession(state.savedJob);
  } catch (error) {
    $("service-status").textContent = "Service unavailable";
    notice(error.message, true);
  }
}
window.addEventListener("pagehide", () => {
  clearTimeout(state.timer);
  clearPreview();
  for (const url of state.downloadUrls) URL.revokeObjectURL(url);
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) schedulePoll();
});
initialize();
