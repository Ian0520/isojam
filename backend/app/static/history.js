const statuses = ["pending", "processing", "completed", "failed"];

function isHistoryPage(page) {
  return (
    page &&
    Array.isArray(page.items) &&
    (page.next_cursor === null ||
      (typeof page.next_cursor === "string" && page.next_cursor.length > 0)) &&
    page.items.every(
      (item) =>
        item &&
        typeof item.id === "string" &&
        typeof item.filename === "string" &&
        statuses.includes(item.status) &&
        typeof item.created_at === "string" &&
        Number.isFinite(Date.parse(item.created_at)),
    )
  );
}

export class JobHistory {
  constructor(request, onChange) {
    this.request = request;
    this.onChange = onChange;
    this.generation = 0;
    this.items = [];
    this.nextCursor = null;
    this.loading = false;
    this.loaded = false;
    this.error = "";
  }

  clear() {
    // A response from the previous account must never repopulate this list.
    this.generation += 1;
    this.items = [];
    this.nextCursor = null;
    this.loading = false;
    this.loaded = false;
    this.error = "";
    this.onChange();
  }

  async load({ more = false } = {}) {
    if (more && (this.loading || !this.nextCursor)) return;
    const generation = ++this.generation;
    const params = new URLSearchParams({ limit: "20" });
    if (more) params.set("cursor", this.nextCursor);
    this.loading = true;
    this.error = "";
    this.onChange();
    try {
      const page = await this.request(`/jobs?${params}`);
      if (generation !== this.generation) return;
      if (!isHistoryPage(page))
        throw new Error("The session list response was incomplete. Please refresh.");
      this.items = more ? [...this.items, ...page.items] : page.items;
      this.nextCursor = page.next_cursor;
      this.loaded = true;
    } catch (error) {
      if (generation === this.generation)
        this.error = error.message || "Could not load sessions. Please refresh.";
    } finally {
      if (generation === this.generation) {
        this.loading = false;
        this.onChange();
      }
    }
  }

  updateJob(job) {
    const item = this.items.find((item) => item.id === job.id);
    if (item && item.status !== job.status) {
      item.status = job.status;
      this.onChange();
    }
  }
}
