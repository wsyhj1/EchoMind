// Metadata-only enhancement: no network, Python triggers, auto-submit, or instructions.
export function prefixQuery(value) {
  if (String(value ?? "").startsWith("//") || String(value ?? "").startsWith("&&")) return null;
  const match = String(value ?? "").match(/^([/&])([^\s]*)$/u);
  return match ? { prefix: match[1], query: match[2] } : null;
}
export function filterCatalog(catalog, query) {
  const norm = (text) => String(text ?? "").normalize("NFKC").toLocaleLowerCase();
  return catalog.filter((s) => [s.id, s.name, ...(s.aliases || [])].some((v) => norm(v).includes(norm(query))));
}
export function commandValue(current, skillId, fallbackPrefix = "/") {
  if (!/^[a-z][a-z0-9_-]{0,63}$/.test(skillId)) return null;
  const value = String(current ?? "");
  const parsed = value.match(/^([/&])([^\s]*)(?:\s+([\s\S]*))?$/u);
  const prefix = parsed ? parsed[1] : (fallbackPrefix === "&" ? "&" : "/");
  return `${prefix}${skillId} ${parsed ? (parsed[3] || "") : value}`;
}

export default function skillPalette(component) {
  const { data = {}, parentElement } = component;
  const doc = parentElement?.ownerDocument || document;
  const view = doc.defaultView || window;
  const ownerKey = Symbol.for("echomind.nativeSkillPalette");
  doc[ownerKey]?.cleanup?.();
  const catalog = Array.isArray(data.catalog) ? data.catalog.filter((s) =>
    s && /^[a-z][a-z0-9_-]{0,63}$/.test(s.id) && Array.isArray(s.aliases)) : [];
  const selector = '[data-testid="stChatInput"] textarea[data-testid="stChatInputTextArea"]';
  const suffix = String(component.key || Math.random().toString(36).slice(2)).replace(/[^a-zA-Z0-9_-]/g, "");
  const menuId = `em-skills-${suffix}`;
  let disposed = false, input = null, inputCleanup = [], originalAria = {};
  let matches = [], active = 0, open = false, manual = false, composing = false;
  let compositionEnded = -Infinity, dismissedValue = null, frame = null, blurTimer = null;
  const cleanups = [];
  const ariaKeys = ["aria-controls", "aria-expanded", "aria-autocomplete", "aria-activedescendant", "aria-haspopup"];
  if (parentElement?.style) { parentElement.style.height = "0px"; parentElement.style.minHeight = "0px"; }
  const style = doc.createElement("style");
  style.textContent = `
    .em-skill-toggle{position:fixed;z-index:1000;height:27px;padding:2px 10px;border:1px solid var(--st-border-color,#cbd8cf);border-radius:8px;background:var(--st-background-color,#fbfcfa);color:var(--st-text-color,#203c34);font:inherit;font-size:12px;cursor:pointer;box-shadow:0 1px 5px #0000000c}
    .em-skill-toggle:hover{border-color:var(--st-primary-color,#2d6954)}
    .em-skill-toggle:focus-visible{outline:2px solid var(--st-primary-color,#2d6954)}
    .em-skill-menu{position:fixed;z-index:1001;box-sizing:border-box;overflow:auto;border:1px solid var(--st-border-color,#cbd8cf);border-radius:12px;padding:6px;background:var(--st-background-color,#fbfcfa);color:var(--st-text-color,#203c34);box-shadow:0 8px 32px #00000024;font:inherit;font-size:13px;overscroll-behavior:contain}
    .em-skill-menu[hidden],.em-skill-toggle[hidden]{display:none!important}
    .em-skill-heading{padding:5px 8px 8px;font-size:11px;opacity:.72}
    .em-skill-option{display:block;width:100%;text-align:left;border:0;border-radius:8px;padding:8px;margin:0;background:transparent;color:inherit;font:inherit;cursor:pointer;box-sizing:border-box;overflow-wrap:anywhere}
    .em-skill-option[aria-selected="true"]{background:var(--st-secondary-background-color,#edf4ee);outline:1px solid var(--st-primary-color,#2d6954);outline-offset:-1px}
    .em-skill-title{display:flex;flex-wrap:wrap;gap:7px;align-items:baseline}
    .em-skill-command{font-weight:650}.em-skill-name{font-size:12px;opacity:.85}
    .em-skill-description{display:block;margin-top:3px;font-size:12px;opacity:.75;line-height:1.45}
    .em-skill-empty{padding:10px 8px;opacity:.75}`;
  const toggle = doc.createElement("button");
  toggle.type = "button"; toggle.className = "em-skill-toggle";
  toggle.dataset.testid = "em-skill-toggle"; toggle.textContent = "技能 /"; toggle.hidden = true;
  toggle.setAttribute("aria-label", "打开技能菜单"); toggle.setAttribute("aria-haspopup", "listbox");
  toggle.setAttribute("aria-controls", menuId);
  toggle.title = "在输入框开头输入 / 或 &；选择只填入命令，不发送消息";
  const menu = doc.createElement("div");
  menu.id = menuId; menu.className = "em-skill-menu"; menu.dataset.testid = "em-skill-menu";
  menu.setAttribute("role", "listbox"); menu.setAttribute("aria-label", "可用技能"); menu.hidden = true;
  function listen(target, name, handler, options, bucket = cleanups) {
    target.addEventListener(name, handler, options);
    bucket.push(() => target.removeEventListener(name, handler, options));
  }
  function disabled() { return Boolean(data.disabled || !input || input.disabled || input.readOnly); }
  function close(dismiss = false) {
    if (dismiss && input) dismissedValue = input.value;
    open = false; manual = false; menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    if (input) { input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); }
  }
  function position() {
    if (!input?.isConnected || disabled()) { toggle.hidden = true; close(); return; }
    const r = input.getBoundingClientRect(), width = view.innerWidth || doc.documentElement.clientWidth;
    toggle.hidden = false; toggle.style.left = `${Math.max(8, Math.min(r.right - 78, width - 86))}px`;
    toggle.style.top = `${Math.max(8, r.top - 34)}px`;
    if (!open) return;
    const w = Math.max(160, Math.min(500, r.width, width - 16));
    const available = Math.max(90, Math.min(360, r.top - 16));
    menu.style.width = `${w}px`; menu.style.maxHeight = `${available}px`;
    menu.style.left = `${Math.max(8, Math.min(r.left, width - w - 8))}px`;
    menu.style.top = `${Math.max(8, r.top - Math.min(menu.scrollHeight, available) - 8)}px`;
  }
  function highlight() {
    const rows = menu.querySelectorAll('[data-testid="em-skill-option"]');
    rows.forEach((row, index) => row.setAttribute("aria-selected", String(index === active)));
    const selected = rows[active];
    if (selected && input) { input.setAttribute("aria-activedescendant", selected.id); selected.scrollIntoView({ block: "nearest", inline: "nearest" }); }
    else input?.removeAttribute("aria-activedescendant");
  }
  function choose(skill) {
    if (!open || composing || disabled()) return;
    const value = commandValue(input.value, skill.id);
    const setter = Object.getOwnPropertyDescriptor(view.HTMLTextAreaElement.prototype, "value")?.set;
    if (value === null || !setter) return;
    setter.call(input, value);
    input.dispatchEvent(new view.InputEvent("input", { bubbles: true, composed: true, inputType: "insertReplacementText", data: value }));
    close(); input.focus({ preventScroll: true }); input.setSelectionRange(value.length, value.length);
  }
  function show(query, byButton = false) {
    if (disposed || composing || disabled()) return;
    manual = byButton; matches = filterCatalog(catalog, query); active = 0; menu.replaceChildren();
    const heading = doc.createElement("div"); heading.className = "em-skill-heading";
    heading.textContent = "技能与命令 · ↑↓选择 · Enter/Tab填入 · Esc关闭"; menu.appendChild(heading);
    matches.forEach((skill, index) => {
      const row = doc.createElement("button"); row.type = "button"; row.className = "em-skill-option";
      row.id = `${menuId}-option-${index}`; row.dataset.testid = "em-skill-option"; row.dataset.skillId = skill.id;
      row.tabIndex = -1; row.setAttribute("role", "option");
      const title = doc.createElement("span"); title.className = "em-skill-title";
      const command = doc.createElement("span"); command.className = "em-skill-command"; command.textContent = `/${skill.id}`;
      const name = doc.createElement("span"); name.className = "em-skill-name"; name.textContent = `${skill.name} · &${skill.id} 同义`;
      title.append(command, name);
      const desc = doc.createElement("span"); desc.className = "em-skill-description";
      desc.textContent = `${skill.description || ""}${skill.requires_input ? " · 需补充内容" : ""}`;
      row.append(title, desc);
      if (Array.isArray(skill.examples) && skill.examples[0]) row.title = skill.examples[0];
      row.addEventListener("pointerdown", (event) => { event.preventDefault(); event.stopPropagation(); choose(skill); });
      row.addEventListener("click", (event) => { event.preventDefault(); choose(skill); });
      menu.appendChild(row);
    });
    if (!matches.length) { const empty = doc.createElement("div"); empty.className = "em-skill-empty";
      empty.textContent = "没有匹配技能，可按Esc关闭后继续输入。"; menu.appendChild(empty); }
    open = true; menu.hidden = false; toggle.setAttribute("aria-expanded", "true"); input.setAttribute("aria-expanded", "true");
    position(); highlight();
  }
  function refresh() {
    if (disabled() || composing || doc.activeElement !== input) { close(); return; }
    if (dismissedValue === input.value) return;
    const prefix = prefixQuery(input.value);
    if (prefix) show(prefix.query); else close();
  }
  function keydown(event) {
    if (disabled()) return;
    if (composing || event.isComposing || event.keyCode === 229) {
      // Keep IME default commit/navigation but hide Enter from React's send handler.
      if (event.key === "Enter" || event.keyCode === 13) event.stopImmediatePropagation();
      return;
    }
    if (event.key === "Enter" && Date.now() - compositionEnded < 90) {
      event.preventDefault(); event.stopImmediatePropagation(); return;
    }
    if (!open || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); close(true); return; }
    if (event.shiftKey) return;
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && matches.length) {
      event.preventDefault(); event.stopImmediatePropagation();
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length; highlight();
    } else if ((event.key === "Enter" || event.key === "Tab") && matches.length) {
      event.preventDefault(); event.stopImmediatePropagation(); choose(matches[active]);
    }
  }
  const resizeObserver = view.ResizeObserver ? new view.ResizeObserver(position) : null;
  function unbind() {
    close(); inputCleanup.forEach((remove) => remove()); inputCleanup = []; resizeObserver?.disconnect();
    if (input) ariaKeys.forEach((key) => originalAria[key] === null ? input.removeAttribute(key) : input.setAttribute(key, originalAria[key]));
    input = null; toggle.hidden = true;
  }
  function locate() {
    if (data.disabled || !catalog.length || doc.querySelector('[role="dialog"][aria-modal="true"]')) return null;
    const candidates = [...doc.querySelectorAll(selector)].filter((node) => node.isConnected && !node.disabled && !node.readOnly && node.getClientRects().length);
    return candidates.find((node) => node === doc.activeElement) || candidates[candidates.length - 1] || null;
  }
  function bind() {
    frame = null; if (disposed) return;
    const next = locate(); if (next === input) { position(); return; }
    unbind(); if (!next) return;
    input = next; composing = false; dismissedValue = null;
    originalAria = Object.fromEntries(ariaKeys.map((key) => [key, input.getAttribute(key)]));
    input.setAttribute("aria-controls", menuId); input.setAttribute("aria-haspopup", "listbox"); input.setAttribute("aria-autocomplete", "list");
    listen(input, "keydown", keydown, true, inputCleanup);
    listen(input, "input", () => { dismissedValue = null; refresh(); }, false, inputCleanup);
    listen(input, "focus", refresh, false, inputCleanup);
    listen(input, "blur", () => { view.clearTimeout(blurTimer); blurTimer = view.setTimeout(() => {
      if (doc.activeElement !== input && !menu.contains(doc.activeElement) && doc.activeElement !== toggle) close();
    }, 0); }, false, inputCleanup);
    listen(input, "compositionstart", () => { composing = true; close(); }, false, inputCleanup);
    listen(input, "compositionend", () => { composing = false; compositionEnded = Date.now(); refresh(); }, false, inputCleanup);
    resizeObserver?.observe(input); position(); refresh();
  }
  function schedule() { if (!disposed && frame === null) frame = view.requestAnimationFrame(bind); }
  const observer = new view.MutationObserver((mutations) => {
    const relevant = mutations.some((change) => change.type === "attributes" ? change.target.matches?.(selector) :
      [...change.addedNodes, ...change.removedNodes].some((node) => node.nodeType === 1 &&
        (node.matches?.(selector + ',[role="dialog"]') || node.querySelector?.(selector + ',[role="dialog"]'))));
    if (relevant || (input && !input.isConnected)) schedule();
  });
  function cleanup() {
    if (disposed) return; disposed = true; observer.disconnect(); view.cancelAnimationFrame(frame); view.clearTimeout(blurTimer);
    unbind(); cleanups.forEach((remove) => remove()); toggle.remove(); menu.remove(); style.remove();
    if (doc[ownerKey]?.cleanup === cleanup) delete doc[ownerKey];
  }
  doc[ownerKey] = { cleanup };
  doc.head.appendChild(style); doc.body.append(toggle, menu);
  listen(toggle, "pointerdown", (event) => event.preventDefault(), false);
  listen(toggle, "click", () => {
    if (disabled()) return; if (open && manual) { close(true); return; }
    dismissedValue = null; input.focus({ preventScroll: true }); show("", true);
  }, false);
  listen(doc, "pointerdown", (event) => {
    if (event.target !== input && !menu.contains(event.target) && !toggle.contains(event.target)) close();
  }, true);
  listen(view, "resize", position, false); listen(doc, "scroll", position, true);
  observer.observe(doc.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "readonly"] });
  bind();
  return cleanup;
}
