import { authenticate } from "./auth.js?v=workspace";
import { JobHistory } from "./history.js?v=workspace";
import { audioUploadBody, validateAudioFile } from "./uploads.js?v=audio-imports";

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
  jobName: null,
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
class CancelledOperation extends Error {}
const jobHistory = new JobHistory(jsonRequest, renderHistory);
const historyDateFormat = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

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
  state.jobName = null;
  state.uploadId = null;
  state.key = null;
  state.accepted = false;
  $("stems").replaceChildren();
  $("active-session").hidden = true;
  $("session-panel").hidden = true;
  $("session-title").textContent = "Recording";
  $("upload-title").textContent = "Separate a recording";
  $("upload-disclosure").open = true;
  $("job-details").open = false;
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
  jobHistory.clear();
  state.file = null;
  $("audio-file").value = "";
  $("file-name").textContent = "Choose an audio file";
  $("file-detail").textContent = "WAV, MP3, M4A, AAC or FLAC";
  $("resume-id").value = "";
  $("job-id-lookup").open = false;
  $("signed-in").open = false;
  $("password").value = "";
  $("invitation-code").value = "";
  state.authMode = "login";
  updateControls();
}
function updateControls() {
  const signedIn = Boolean(state.token);
  const canRegister = ["open", "invite"].includes(
    state.info?.registration_mode,
  );
  const registering = state.authMode === "register";
  const needsInvitation =
    registering && state.info?.registration_mode === "invite";
  const active =
    state.job && ["pending", "processing"].includes(state.job.status);
  $("signed-in").hidden = !signedIn;
  $("signed-out").hidden = signedIn;
  $("account-panel").hidden = signedIn;
  $("account-email").textContent = state.email || "";
  $("login-tab").disabled = state.authBusy;
  $("register-tab").hidden = !canRegister;
  $("register-tab").disabled = state.authBusy || !canRegister;
  $("login-tab").classList.toggle("selected", !registering);
  $("register-tab").classList.toggle("selected", registering);
  $("login-tab").setAttribute("aria-pressed", String(!registering));
  $("register-tab").setAttribute("aria-pressed", String(registering));
  $("password").autocomplete = registering
    ? "new-password"
    : "current-password";
  $("password").minLength = registering ? 8 : 1;
  $("registration-note").textContent =
    {
      open: "Create an account or sign in to your existing account.",
      invite:
        "Have an invitation? Create an account with the email and code shared with you.",
      closed:
        "New account registration is closed. Existing accounts can still sign in.",
    }[state.info?.registration_mode] ||
    "Sign in below. Account creation is unavailable until the server settings load.";
  $("invitation-field").hidden = !needsInvitation;
  $("invitation-code").required = needsInvitation;
  $("invitation-code").disabled = !needsInvitation || state.authBusy;
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
    ? state.uploadId ? "Submitting…" : "Preparing audio…"
    : state.accepted
      ? "Submitted"
      : state.uploadId
        ? "Retry submission"
        : "Separate audio";
  $("resume-submit").disabled = !signedIn || state.busy;
  $("history-refresh").hidden = !signedIn;
  $("history-refresh").disabled = jobHistory.loading;
  $("history-more").hidden = !signedIn || !jobHistory.nextCursor;
  $("history-more").disabled = jobHistory.loading;
  $("history-more").textContent = jobHistory.loading ? "Loading…" : "Load more";
  for (const button of document.querySelectorAll(".history-job")) {
    button.disabled = !signedIn || state.busy;
    if (button.dataset.jobId === state.job?.id)
      button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  }
}

function renderHistory() {
  const focusedId = document.activeElement?.dataset.jobId;
  const list = $("job-history");
  list.replaceChildren();
  list.setAttribute("aria-busy", String(jobHistory.loading));
  for (const item of jobHistory.items) {
    const row = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-job";
    button.dataset.jobId = item.id;
    const filename = document.createElement("span");
    filename.className = "history-filename";
    filename.textContent = item.filename;
    const date = document.createElement("time");
    date.dateTime = item.created_at;
    date.textContent = historyDateFormat.format(new Date(item.created_at));
    const status = document.createElement("span");
    status.className = `status-badge ${item.status}`;
    status.textContent = item.status;
    button.append(filename, date, status);
    button.addEventListener("click", () => {
      if (!state.busy) openSession(item.id);
    });
    row.append(button);
    list.append(row);
    if (item.id === focusedId) button.focus({ preventScroll: true });
  }
  const message = $("history-message");
  message.textContent = !state.token
    ? "Sign in to see your recordings."
    : jobHistory.error
      ? `Could not load recordings. ${jobHistory.error}`
      : jobHistory.loading
        ? "Loading recordings…"
        : jobHistory.loaded && !jobHistory.items.length
          ? "No recordings yet. Submit a recording to get started."
          : "";
  message.hidden = !message.textContent;
  message.classList.toggle("error", Boolean(jobHistory.error));
  updateRecordingTitle();
  updateControls();
}

function updateRecordingTitle() {
  if (!state.job) return;
  const item = jobHistory.items.find((item) => item.id === state.job.id);
  if (item) state.jobName = item.filename;
  else if (state.uploadId === state.job.upload_id && state.file)
    state.jobName = state.file.name;
  $("session-title").textContent = state.jobName || "Recording";
}

$("history-refresh").addEventListener("click", () => {
  if (state.token) jobHistory.load();
});
$("history-more").addEventListener("click", () => {
  if (state.token) jobHistory.load({ more: true });
});

async function jsonRequest(path, { method = "GET", body, auth = true } = {}) {
  const token = state.token;
  const epoch = state.epoch;
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
      "Could not reach the server. Check your connection and try again.",
    );
  }
  const data = await response.json().catch(() => ({}));
  if (auth && (epoch !== state.epoch || token !== state.token))
    throw new CancelledOperation();
  if (!response.ok) {
    if (auth && response.status === 401 && token === state.token) {
      clearSession();
      notice(
        "Your sign-in expired. Sign in again to access your recordings.",
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
  if (
    state.authBusy ||
    (mode === "register" &&
      !["open", "invite"].includes(state.info?.registration_mode))
  )
    return;
  state.authMode = mode;
  if (mode === "login") $("invitation-code").value = "";
  updateControls();
}
$("login-tab").addEventListener("click", () => setAuthMode("login"));
$("register-tab").addEventListener("click", () => setAuthMode("register"));
$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.authBusy) return;
  const registering = state.authMode === "register";
  if (
    registering &&
    !["open", "invite"].includes(state.info?.registration_mode)
  )
    return;
  const epoch = state.epoch;
  state.authBusy = true;
  updateControls();
  notice("");
  const credentials = {
    email: $("email").value.trim(),
    password: $("password").value,
  };
  try {
    const result = await authenticate(
      jsonRequest,
      {
        ...credentials,
        invitationCode:
          registering && state.info.registration_mode === "invite"
            ? $("invitation-code").value
            : undefined,
      },
      {
        register: registering,
        onRegistered() {
          if (epoch !== state.epoch) return;
          $("invitation-code").value = "";
          state.authMode = "login";
          updateControls();
        },
      },
    );
    if (epoch !== state.epoch) return;
    state.token = result.access_token;
    state.email = credentials.email.toLowerCase();
    storage.set("token", state.token);
    storage.set("email", state.email);
    $("password").value = "";
    notice("");
    jobHistory.load();
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
  notice("Signed out. Submitted recordings continue processing.");
});
$("audio-file").addEventListener("change", () => {
  clearJob();
  state.savedJob = null;
  storage.remove("job");
  const file = $("audio-file").files[0];
  state.file = null;
  notice("");
  $("file-name").textContent = file?.name || "Choose an audio file";
  $("file-detail").textContent = file
    ? `${(file.size / 1024 / 1024).toFixed(1)} MiB`
    : "WAV, MP3, M4A, AAC or FLAC";
  if (file) {
    try {
      validateAudioFile(file, state.info);
      state.file = file;
      state.key = crypto.randomUUID();
    } catch (error) {
      notice(error.message, true);
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
      const body = audioUploadBody(state.file, state.info);
      const upload = await jsonRequest("/uploads", { method: "POST", body });
      if (epoch !== state.epoch) return;
      state.uploadId = upload.id;
      updateControls();
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
    notice("");
    jobHistory.load();
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
  if (state.job?.id !== job.id) {
    state.jobName = null;
    $("upload-disclosure").open = false;
    $("job-details").open = false;
  }
  state.job = job;
  updateRecordingTitle();
  state.savedJob = job.id;
  storage.set("job", job.id);
  $("resume-id").value = job.id;
  $("job-id").textContent = job.id;
  $("session-panel").hidden = false;
  $("upload-title").textContent = "Separate another recording";
  $("active-session").hidden = false;
  $("job-status").hidden = false;
  $("job-status").textContent = job.status;
  $("job-status").className = `status-badge ${job.status}`;
  const active = ["pending", "processing"].includes(job.status);
  $("processing-indicator").hidden = !active;
  $("job-message").textContent =
    {
      pending: "Queued. Processing will start when the processor is available.",
      processing:
        "Separating tracks. You can return to this recording from the sidebar.",
      completed: "Tracks ready. Preview a track or download its WAV file.",
      failed:
        "Separation failed. You can submit the recording again. The job ID is available under Details.",
    }[job.status] || "Checking recording status…";
  if (job.status === "completed") renderStems(job.outputs);
  else {
    $("stems").replaceChildren();
    clearPreview();
  }
  jobHistory.updateJob(job);
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
    notice("Sign in to open a recording.", true);
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
    notice("Select the job ID under Details and copy it manually.");
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
      `Audio is unavailable (${response.status}). Refresh the recording status and try again.`,
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
    label.append(name);
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
    download.textContent = "Download";
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
  if (state.token) jobHistory.load();
  try {
    state.info = await jsonRequest("/ui-info", { auth: false });
    const mode = state.info.processing_mode;
    $("service-status").textContent = {
      local: "Separation ready",
      queued: "Separation available",
      disabled: "Playback only",
    }[mode];
    $("mode-note").textContent = {
      local: "Separate a recording into individual instrument tracks.",
      queued:
        "Separate a recording into instrument tracks. Processing continues if you leave this page.",
      disabled:
        "New separation is unavailable. Sign in to preview or download your saved tracks.",
    }[mode];
    $("audio-file").accept = Object.keys(state.info.audio_upload_types).join(",");
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
  $("invitation-code").value = "";
  clearTimeout(state.timer);
  clearPreview();
  for (const url of state.downloadUrls) URL.revokeObjectURL(url);
});
window.addEventListener("pageshow", (event) => {
  if (event.persisted) schedulePoll();
});
initialize();
