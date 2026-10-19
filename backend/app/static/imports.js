const importStatuses = ["pending", "processing", "ready", "failed"];
const jobStatuses = ["pending", "processing", "completed", "failed"];
const validId = (value) => typeof value === "string" &&
  /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value);

function isImport(item) {
  return item && validId(item.id) &&
    typeof item.video_id === "string" && /^[A-Za-z0-9_-]{11}$/.test(item.video_id) &&
    importStatuses.includes(item.status) &&
    typeof item.created_at === "string" && Number.isFinite(Date.parse(item.created_at)) &&
    (item.status !== "ready" ||
      (validId(item.upload_id) && typeof item.filename === "string")) &&
    (item.status !== "failed" || typeof item.error_code === "string");
}

export function importFailure(code) {
  return {
    unavailable: "This video is unavailable or YouTube refused the download. Try another video.",
    too_long: "This video's audio exceeds the duration limit. Choose a shorter recording.",
    too_large: "This video's audio exceeds the size limit. Choose a smaller recording.",
    unsupported: "Choose a non-live video with a supported audio stream.",
    timeout: "The download took too long. You can try the import again.",
    invalid_audio: "The downloaded audio could not be decoded. Try another video.",
    tooling: "The audio importer is unavailable. Please try again later.",
    interrupted: "The import was interrupted. Try again to start a new download.",
  }[code] || "The audio could not be imported. You can try again or choose another video.";
}

// The browser remembers one selected workflow per account, never credentials.
// The API remains the authority for ownership, admission and idempotency.
export class YouTubeImports {
  constructor(request, onChange, onJob, storage, {
    newKey = () => crypto.randomUUID(),
    setTimer = (callback) => setTimeout(callback, 1500),
    clearTimer = (timer) => clearTimeout(timer),
  } = {}) {
    Object.assign(this, { request, onChange, onJob, storage, newKey, setTimer, clearTimer });
    this.generation = 0;
    this.listTurn = 0;
    this.owner = null;
    this.intent = null;
    this.item = null;
    this.job = null;
    this.items = [];
    this.busy = false;
    this.loading = false;
    this.phase = "idle";
    this.error = "";
    this.listError = "";
    this.timer = null;
  }

  get active() {
    return this.busy || Boolean(this.intent &&
      !["failed", "rejected", "submitted"].includes(this.phase));
  }

  persist() {
    if (!this.owner) return;
    const key = `youtube.${this.owner}`;
    if (this.intent) this.storage.set(key, JSON.stringify(this.intent));
    else this.storage.remove(key);
  }

  connect(owner) {
    this.pause();
    Object.assign(this, {
      owner, intent: null, item: null, job: null, items: [], loading: false,
      phase: "idle", error: "", listError: "",
    });
    if (owner) {
      try {
        const saved = JSON.parse(this.storage.get(`youtube.${owner}`));
        if (saved && typeof saved.url === "string" && saved.url.length > 0 && saved.url.length <= 2048 &&
            typeof saved.key === "string" && /^[A-Za-z0-9._:-]{1,128}$/.test(saved.key) &&
            (saved.id === null || validId(saved.id))) {
          this.intent = { url: saved.url, key: saved.key, id: saved.id };
          this.phase = saved.id ? "pending" : "submitting";
        }
      } catch { /* A damaged bookmark must not prevent sign-in. */ }
    }
    this.onChange();
  }

  pause() {
    this.generation += 1;
    this.listTurn += 1;
    this.clearTimer(this.timer);
    this.timer = null;
    this.busy = false;
    this.loading = false;
  }

  restore() {
    if (!this.owner) return;
    this.refresh();
    if (this.intent && !this.busy) return this.retry();
  }

  start(url) {
    if (!this.owner || this.busy) return;
    url = url.trim();
    if (!url || url.length > 2048) return;
    if (!this.intent || this.intent.url !== url || this.phase === "failed") {
      this.intent = { url, key: this.newKey(), id: null };
      this.item = null;
    }
    this.job = null;
    this.persist(); // Before POST: even a lost response can be replayed after reload.
    return this.retry();
  }

  open(item) {
    if (!this.owner || this.busy || !isImport(item)) return;
    this.intent = {
      url: `https://www.youtube.com/watch?v=${item.video_id}`,
      key: this.newKey(), id: item.id,
    };
    this.item = item;
    this.job = null;
    this.persist();
    return this.retry();
  }

  reset() {
    if (this.busy || (this.active && this.item?.status !== "ready")) return;
    this.pause();
    Object.assign(this, { intent: null, item: null, job: null, phase: "idle", error: "" });
    this.persist();
    this.onChange();
  }

  retry() {
    if (!this.owner || !this.intent || this.busy) return;
    this.clearTimer(this.timer);
    return this.advance(++this.generation);
  }

  async advance(generation) {
    const intent = this.intent;
    if (!intent || !this.owner || generation !== this.generation) return;
    this.busy = true;
    this.error = "";
    this.phase = intent.id ? (this.item?.status || "pending") : "submitting";
    this.onChange();
    let submittingImport = !intent.id;
    try {
      const item = await this.request(intent.id ? `/imports/${encodeURIComponent(intent.id)}` : "/imports",
        intent.id ? {} : { method: "POST", body: { url: intent.url },
          headers: { "Idempotency-Key": intent.key } });
      if (generation !== this.generation) return;
      submittingImport = false;
      if (!isImport(item) || (intent.id && item.id !== intent.id))
        throw new Error("The import response was incomplete. Retry to check the same request.");
      intent.id = item.id;
      this.persist();
      this.item = item;
      this.items = [item, ...this.items.filter((old) => old.id !== item.id)].slice(0, 20);
      this.phase = item.status;
      this.onChange();
      if (item.status === "ready") {
        this.phase = "creating-job";
        this.onChange();
        // The same import always names the same job submission, even in another tab.
        const job = await this.request("/jobs", {
          method: "POST", body: { upload_id: item.upload_id },
          headers: { "Idempotency-Key": `youtube-import:${item.id}` },
        });
        if (generation !== this.generation) return;
        if (!job || !validId(job.id) || job.upload_id !== item.upload_id ||
            !jobStatuses.includes(job.status) || !job.outputs ||
            typeof job.outputs !== "object" || Array.isArray(job.outputs))
          throw new Error("The separation response was incomplete. Retry to retrieve the same job.");
        this.job = job;
        this.phase = "submitted";
        this.onJob(job, item);
        this.intent = null;
        this.persist();
      }
    } catch (error) {
      if (generation === this.generation) {
        this.error = error.message || "Could not check the import. Please retry.";
        if (this.item?.status === "ready") this.phase = "ready";
        if (submittingImport && [409, 422, 429, 503].includes(error.status))
          this.phase = "rejected";
      }
    } finally {
      if (generation === this.generation) {
        this.busy = false;
        this.onChange();
        if (!this.error && ["pending", "processing"].includes(this.item?.status))
          this.timer = this.setTimer(() => this.advance(generation));
      }
    }
  }

  async refresh() {
    if (!this.owner) return;
    const turn = ++this.listTurn;
    this.loading = true;
    this.listError = "";
    this.onChange();
    try {
      const items = await this.request("/imports?limit=20");
      if (turn !== this.listTurn) return;
      if (!Array.isArray(items) || !items.every(isImport))
        throw new Error("The import list response was incomplete. Please refresh.");
      this.items = items;
      if (this.item)
        this.items = [this.item, ...items.filter((item) => item.id !== this.item.id)].slice(0, 20);
    } catch (error) {
      if (turn === this.listTurn)
        this.listError = error.message || "Could not load recent imports.";
    } finally {
      if (turn === this.listTurn) {
        this.loading = false;
        this.onChange();
      }
    }
  }
}
