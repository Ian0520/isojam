// Execute the real page module against its real HTML IDs, without a test-only UI.
// Layout and native browser behavior are checked separately in a real browser.
import { readFile } from "node:fs/promises";

let pageNumber = 0;
export const settle = () => new Promise(setImmediate);

export async function browserPage(respond, values = new Map([
  ["isojam.token", "test-token"], ["isojam.email", "first@example.com"],
])) {
  const nodes = new Map(), timers = new Map(), events = new Map(), calls = [];
  let document, nextTimer = 0;
  class Element {
    constructor(attributes = "") {
      Object.assign(this, {
        children: [], listeners: new Map(), attributes: new Map(), dataset: {},
        value: "", files: [], className: "", _text: "",
        hidden: /\bhidden(?:\s|$)/.test(attributes),
        disabled: /\bdisabled(?:\s|$)/.test(attributes),
      });
      this.classList = { toggle: (name, enabled) => {
        const names = new Set(this.className.split(/\s+/).filter(Boolean));
        enabled ? names.add(name) : names.delete(name);
        this.className = [...names].join(" ");
      } };
    }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map((node) => node.textContent).join(""); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this._text = ""; this.children = children; }
    setAttribute(key, value) { this.attributes.set(key, value); }
    removeAttribute(key) { this.attributes.delete(key); }
    addEventListener(type, callback) { this.listeners.set(type, callback); }
    dispatch(type) { return this.listeners.get(type)?.({ preventDefault() {} }); }
    click() { if (!this.disabled) return this.dispatch("click"); }
    focus() { document.activeElement = this; }
    pause() {}
    load() {}
    remove() {}
  }
  const html = await readFile(new URL("../app/static/index.html", import.meta.url), "utf8");
  for (const match of html.matchAll(/<\w+\b([^>]*\bid="([^"]+)"[^>]*)>/g))
    nodes.set(match[2], new Element(match[1]));
  const descendants = (node) => node.children.flatMap((child) => [child, ...descendants(child)]);
  document = {
    getElementById: (id) => nodes.get(id) || null,
    createElement: () => new Element(),
    activeElement: null, body: new Element(),
    querySelectorAll(selector) {
      if (selector === ".import-list button")
        return descendants(nodes.get("youtube-imports")).filter((node) => node.dataset.importId);
      const className = selector.slice(1);
      return [...nodes.values()].flatMap((node) => descendants(node))
        .filter((node) => node.className.split(" ").includes(className));
    },
  };
  const globals = {
    document,
    window: { addEventListener: (type, callback) => events.set(type, callback) },
    location: { origin: "http://localhost" },
    sessionStorage: {
      getItem: (key) => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key),
    },
    setTimeout: (callback) => { timers.set(++nextTimer, callback); return nextTimer; },
    clearTimeout: (timer) => timers.delete(timer),
    fetch: async (path, options = {}) => {
      calls.push({ path, ...options });
      const result = await respond(path, options);
      return { ok: result.status >= 200 && result.status < 300,
        status: result.status, json: async () => result.body };
    },
  };
  const previous = new Map();
  for (const [key, value] of Object.entries(globals)) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
  }
  const close = async () => {
    events.get("pagehide")?.();
    await settle();
    for (const [key, descriptor] of previous)
      descriptor ? Object.defineProperty(globalThis, key, descriptor) : delete globalThis[key];
  };
  try {
    await import(`../app/static/app.js?page-test=${++pageNumber}`);
    await settle();
  } catch (error) { await close(); throw error; }
  return { nodes, calls, values, timers, close, async submit(url) {
    nodes.get("youtube-source").click();
    nodes.get("youtube-url").value = url;
    nodes.get("youtube-url").dispatch("input");
    nodes.get("youtube-form").dispatch("submit");
    await settle();
  } };
}
