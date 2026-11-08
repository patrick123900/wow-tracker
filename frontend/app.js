"use strict";

const CLASS_COLORS = {
  WARRIOR: "#C69B6D", PALADIN: "#F48CBA", HUNTER: "#AAD372", ROGUE: "#FFF468",
  PRIEST: "#FFFFFF", SHAMAN: "#0070DD", MAGE: "#3FC7EB", WARLOCK: "#8788EE",
  DRUID: "#FF7C0A", DEATHKNIGHT: "#C41E3A", MONK: "#00FF98",
};
const POWER = {
  MANA: ["Mana", "var(--mana)"], RAGE: ["Rage", "var(--rage)"],
  ENERGY: ["Energy", "var(--energy)"], FOCUS: ["Focus", "var(--focus)"],
};
const EVENT_ICONS = { level: "★", death: "☠", zone: "⚑", quest: "✔", accept: "!", abandon: "✕", login: "⏻", skill: "⚒" };
const FILTER_GROUPS = { quest: ["quest", "accept", "abandon"] };
const SESSION_GAP = 15 * 60; // seconds; longer gaps count as a break
const DEFAULT_MAP = { width: 1002, height: 668 };

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat();
let state = null;
let history = { positions: [], xp: [], events: [] };
let characters = [];
let selected = new URLSearchParams(location.search).get("c"); // "Name-Realm", or the last played
let stream = null;
let journalFilter = "all";
let mapIndex = {};
let shownMap = null;

// --- helpers -------------------------------------------------------------------

function ago(t, now = Date.now() / 1000) {
  const s = Math.max(0, Math.round(now - t));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function duration(sec) {
  if (!isFinite(sec) || sec <= 0) return "–";
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function clock(t) {
  const d = new Date(t * 1000);
  const today = new Date().toDateString() === d.toDateString();
  return today
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function money(copper) {
  if (copper == null) return "–";
  const g = Math.floor(copper / 10000), s = Math.floor(copper / 100) % 100, c = copper % 100;
  const parts = [];
  if (g) parts.push(`<span class="coin g">${fmt.format(g)}</span>`);
  if (g || s) parts.push(`<span class="coin s">${s}</span>`);
  parts.push(`<span class="coin c">${c}</span>`);
  return parts.join("");
}

function moneyText(copper) {
  const g = Math.floor(copper / 10000), s = Math.floor(copper / 100) % 100, c = copper % 100;
  return [g && `${g}g`, s && `${s}s`, c && `${c}c`].filter(Boolean).join(" ");
}

function pct(a, b) { return b > 0 ? Math.max(0, Math.min(100, (a / b) * 100)) : 0; }

function svgEl(tag, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// XP gained between two samples, across level boundaries.
function xpGain(a, b) {
  if (b.level === a.level) return Math.max(0, b.xp - a.xp);
  if (b.level === a.level + 1) return Math.max(0, a.xpMax - a.xp) + b.xp;
  return 0;
}

// XP/hour over the most recent play session.
function xpRate(samples) {
  let gained = 0, active = 0;
  for (let i = samples.length - 1; i > 0; i--) {
    const dt = samples[i].t - samples[i - 1].t;
    if (dt > SESSION_GAP) break;
    gained += xpGain(samples[i - 1], samples[i]);
    active += dt;
    if (active > 3 * 3600) break;
  }
  return active >= 300 ? (gained / active) * 3600 : null;
}

function mapSize(mapID) {
  return mapIndex[mapID] || DEFAULT_MAP;
}

// --- render ------------------------------------------------------------------

function renderHero(s) {
  const color = CLASS_COLORS[s.classToken] || "var(--gold)";
  document.documentElement.style.setProperty("--class", color);
  const fullName = [s.name, s.surname].filter(Boolean).join(" ");
  document.title = `${fullName} · Forever Tracker`;
  const portrait = racePortrait(s);
  if (changed("crest", [portrait, s.class])) $("crest").innerHTML = portraitHtml(s);
  $("name").textContent = fullName;
  $("realm").textContent = [s.realm, s.faction].filter(Boolean).join(" · ") || "World of Warcraft: Forever";
  $("subtitle").textContent = [`Level ${s.level ?? "?"} ${s.race ?? ""} ${s.class ?? ""}`, s.guild && `<${s.guild}>`]
    .filter(Boolean).join("  ·  ");
}

function renderPresence() {
  const pill = $("presence");
  const online = state && state.online;
  pill.className = `pill ${online ? "online" : "offline"}`;
  $("presence-text").textContent = !online ? "Offline" : state.live ? "Live" : "Online";
  let text = "";
  if (online && state.live) text = "Updating every second";
  else if (state?.updatedAt) text = `Last update ${ago(state.updatedAt)}`;
  if (!online && state?.presenceAt) text += `${text ? " · " : ""}seen ${ago(state.presenceAt)}`;
  $("freshness").textContent = text;
}

function renderProgress(s) {
  $("level").textContent = s.level ?? "–";
  const maxLevel = s.maxLevel || 60;
  const atCap = s.level >= maxLevel;
  $("level-cap").textContent = s.level == null ? "" : atCap ? "Max level" : `${maxLevel - s.level} levels to go`;

  const known = s.xp != null && s.xpMax;
  const xpP = atCap ? 100 : known ? pct(s.xp, s.xpMax) : 0;
  const restP = atCap || !known ? 0 : Math.min(100, pct(s.xp + (s.rested || 0), s.xpMax));
  $("xp-fill").style.width = `${xpP}%`;
  $("rested-fill").style.width = `${restP}%`;
  $("xp-text").textContent = atCap ? "Max level"
    : known ? `${fmt.format(s.xp)} / ${fmt.format(s.xpMax)} XP (${xpP.toFixed(1)}%)` : "XP hidden right now";
  $("rested-text").textContent = s.rested ? `${fmt.format(s.rested)} rested` : "";

  const journey = $("journey");
  if (journey.childElementCount !== maxLevel) {
    journey.replaceChildren();
    for (let lvl = 1; lvl <= maxLevel; lvl++) {
      const span = document.createElement("span");
      span.title = `Level ${lvl}`;
      journey.append(span);
    }
    journey.style.gridTemplateColumns = `repeat(${maxLevel}, 1fr)`;
  }
  [...journey.children].forEach((span, i) => {
    span.className = i + 1 < s.level ? "done" : i + 1 === s.level ? "now" : "";
  });

  const rate = xpRate(history.xp);
  $("xph").textContent = rate ? fmt.format(Math.round(rate)) : "–";
  $("ttl").textContent = rate && known && !atCap ? duration((s.xpMax - s.xp) / rate * 3600) : "–";
  $("money").innerHTML = money(s.money);
  $("deaths").textContent = history.events.filter((e) => e.type === "death").length;
}

// Exact values when the game allows it, else its percentage. Returns false when
// neither is known (the game keeps it secret); callers then hide the bar.
function setBar(fill, text, cur, max, percent) {
  if (cur != null && max) {
    $(fill).style.width = `${pct(cur, max)}%`;
    $(text).textContent = `${fmt.format(cur)} / ${fmt.format(max)}`;
    return true;
  }
  if (percent != null) {
    $(fill).style.width = `${percent}%`;
    $(text).textContent = max ? `${percent}% of ${fmt.format(max)}` : `${percent}%`;
    return true;
  }
  return false;
}

// When the game hasn't reported a current value (it keeps them secret), show
// the bar as it rests out of combat: full, or empty for rage and when dead.
function resting(cur, max, percent, empty) {
  return cur != null || percent != null ? cur : empty ? 0 : max;
}

function renderVitals(s) {
  const dead = s.dead || s.ghost;
  const hp = setBar("hp-fill", "hp-text", resting(s.health, s.healthMax, s.healthPct, dead), s.healthMax, s.healthPct);
  $("hp-fill").closest(".vital").hidden = !hp;
  const p = shownPower(s.power);
  const [label, color] = POWER[p.type] || [p.type ? p.type[0] + p.type.slice(1).toLowerCase() : "Power", "var(--mana)"];
  $("power-name").textContent = label;
  const pw = setBar("power-fill", "power-text", resting(p.cur, p.max, p.pct, dead || p.type === "RAGE"), p.max, p.pct);
  $("power-fill").closest(".vital").hidden = !pw;
  $("power-fill").style.setProperty("--power", color);

  const flags = [];
  if (s.ghost) flags.push(["dead", "Ghost"]);
  else if (s.dead) flags.push(["dead", "Dead"]);
  if (s.combat) flags.push(["combat", "In combat"]);
  if (s.resting) flags.push(["resting", "Resting"]);
  $("flags").innerHTML = flags.map(([c, t]) => `<span class="flag ${c}">${t}</span>`).join("");

  renderEnemy(s);
}

const RANKS = { elite: "Elite", rare: "Rare", rareelite: "Rare elite", worldboss: "Boss" };

// What you're fighting: your target, or the creature you last traded blows
// with; shown only while in combat.
function renderEnemy(s) {
  const e = s.enemy;
  $("enemy").hidden = !(e && s.combat);
  if ($("enemy").hidden) return;
  const level = e.level === -1 ? "??" : e.level;
  $("enemy-who").innerHTML = `Fighting <span class="who">${esc(e.name)}</span>`
    + `${level != null ? ` · Lv ${level}` : ""}${e.rank ? `<span class="rank">${RANKS[e.rank]}</span>` : ""}`
    + `${e.more ? `<span class="more">+${e.more} more</span>` : ""}`;
}

function setMapArt(mapID) {
  if (shownMap === mapID) return;
  shownMap = mapID;
  const map = $("map");
  const { width, height } = mapSize(mapID);
  map.style.aspectRatio = `${width} / ${height}`;
  $("map-svg").setAttribute("viewBox", `0 0 ${width} ${height}`);
  map.style.backgroundImage = "";
  map.classList.remove("has-art");
  if (!mapIndex[mapID]) return;
  const img = new Image();
  img.onload = () => {
    if (shownMap !== mapID) return;
    map.style.backgroundImage = `url(${img.src})`;
    map.classList.add("has-art");
  };
  img.src = `maps/${mapID}.jpg`;
}

// Static layer: grid (when there's no art), trail and death markers.
function renderTrail(s) {
  const g = $("map-trail");
  g.replaceChildren();
  const { width: W, height: H } = mapSize(s.mapID);
  if (!mapIndex[s.mapID]) {
    for (let i = 1; i < 10; i++) {
      g.append(svgEl("line", { class: "gridline", x1: i * W / 10, y1: 0, x2: i * W / 10, y2: H }));
      g.append(svgEl("line", { class: "gridline", x1: 0, y1: i * H / 10, x2: W, y2: i * H / 10 }));
    }
  }
  const trail = history.positions.filter((p) => p.mapID === s.mapID && p.x != null);
  if (trail.length > 1) {
    // Break the line where the gap between samples is a long break.
    let d = "";
    trail.forEach((p, i) => {
      const cmd = i === 0 || p.t - trail[i - 1].t > SESSION_GAP ? "M" : "L";
      d += `${cmd}${(p.x * W).toFixed(1)},${(p.y * H).toFixed(1)}`;
    });
    g.append(svgEl("path", { class: "trail", d }));
    for (const p of trail) {
      const c = svgEl("circle", { class: "stop", cx: p.x * W, cy: p.y * H, r: 4 });
      c.dataset.tip = `${clock(p.t)} · ${(p.x * 100).toFixed(1)}, ${(p.y * 100).toFixed(1)}`;
      g.append(c);
    }
  }
  for (const e of history.events) {
    if (e.type === "death" && e.mapID === s.mapID && e.x != null) {
      const c = svgEl("circle", { class: "stop death", cx: e.x * W, cy: e.y * H, r: 7 });
      c.dataset.tip = `☠ Died at level ${e.level} · ${clock(e.t)}`;
      g.append(c);
    }
  }
}

// Moving layer: the player marker, eased between live updates.
function renderMe(s) {
  $("zone").textContent = s.zone || s.map || "Unknown zone";
  $("coords").textContent = s.x != null ? `${(s.x * 100).toFixed(1)}, ${(s.y * 100).toFixed(1)}` : "–";
  const mapChanged = shownMap !== s.mapID;
  setMapArt(s.mapID);
  const me = $("map-me");
  if (s.x == null) { me.hidden = true; return; }
  const { width: W, height: H } = mapSize(s.mapID);
  me.hidden = false;
  me.classList.toggle("live", !!state.live);
  // No easing across map changes or the first placement.
  me.classList.toggle("jump", mapChanged || !me.dataset.placed);
  me.style.transform = `translate(${s.x * W}px, ${s.y * H}px)`;
  me.dataset.placed = "1";
  me.dataset.tip = `${[s.name, s.surname].filter(Boolean).join(" ")} · ${s.zone || ""}`;
  if (mapChanged) renderTrail(s);
}

function renderChart() {
  const svg = $("chart-svg");
  const box = $("chart").getBoundingClientRect();
  const W = Math.max(200, box.width), H = box.height, padL = 30, padB = 22, padT = 8;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.replaceChildren();

  const samples = history.xp.filter((x) => x.xpMax > 0);
  if (samples.length < 2) {
    const t = svgEl("text", { class: "label", x: W / 2, y: H / 2, "text-anchor": "middle" });
    t.textContent = "The curve appears once there's some XP history.";
    svg.append(t);
    return;
  }

  // x = active play time (breaks collapsed), y = level + fraction into level
  let x = 0;
  const pts = samples.map((p, i) => {
    if (i) x += Math.min(p.t - samples[i - 1].t, 600);
    return { ax: x, lv: p.level + p.xp / p.xpMax, src: p };
  });
  const maxX = Math.max(1, x);
  const minL = Math.floor(pts[0].lv), maxL = Math.ceil(pts.at(-1).lv + 0.001);
  const sx = (v) => padL + (v / maxX) * (W - padL - 6);
  const sy = (v) => padT + (1 - (v - minL) / Math.max(1, maxL - minL)) * (H - padT - padB);

  const defs = svgEl("defs");
  const grad = svgEl("linearGradient", { id: "chartFill", x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(svgEl("stop", { offset: "0%", "stop-color": "var(--gold)", "stop-opacity": ".35" }));
  grad.append(svgEl("stop", { offset: "100%", "stop-color": "var(--gold)", "stop-opacity": "0" }));
  defs.append(grad);
  svg.append(defs);

  const step = Math.max(1, Math.ceil((maxL - minL) / 6));
  for (let l = minL; l <= maxL; l += step) {
    svg.append(svgEl("line", { class: "axis", x1: padL, x2: W, y1: sy(l), y2: sy(l) }));
    const t = svgEl("text", { class: "label", x: padL - 6, y: sy(l) + 4, "text-anchor": "end" });
    t.textContent = l;
    svg.append(t);
  }
  const hours = maxX / 3600;
  const tl = svgEl("text", { class: "label", x: W - 6, y: H - 4, "text-anchor": "end" });
  tl.textContent = `${hours >= 1 ? hours.toFixed(1) + " h" : Math.round(maxX / 60) + " min"} played`;
  svg.append(tl);

  const line = pts.map((p, i) => `${i ? "L" : "M"}${sx(p.ax).toFixed(1)},${sy(p.lv).toFixed(1)}`).join("");
  svg.append(svgEl("path", { class: "area", d: `${line}L${sx(pts.at(-1).ax)},${sy(minL)}L${sx(0)},${sy(minL)}Z` }));
  svg.append(svgEl("path", { class: "line", d: line }));

  const cursor = svgEl("line", { class: "cursor", y1: padT, y2: H - padB, visibility: "hidden" });
  const dot = svgEl("circle", { class: "cursor-dot", r: 4, visibility: "hidden" });
  svg.append(cursor, dot);
  const tip = $("chart-tip");

  svg.onpointermove = (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    let best = pts[0];
    for (const p of pts) if (Math.abs(sx(p.ax) - px) < Math.abs(sx(best.ax) - px)) best = p;
    const cx = sx(best.ax), cy = sy(best.lv);
    cursor.setAttribute("x1", cx); cursor.setAttribute("x2", cx);
    dot.setAttribute("cx", cx); dot.setAttribute("cy", cy);
    cursor.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.style.left = `${(cx / W) * r.width}px`;
    tip.style.top = `${(cy / H) * r.height}px`;
    tip.textContent = `Level ${best.src.level} · ${pct(best.src.xp, best.src.xpMax).toFixed(0)}% · ${clock(best.src.t)}`;
  };
  svg.onpointerleave = () => {
    cursor.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden");
    tip.hidden = true;
  };
}

function renderQuests() {
  const list = $("quest-list");
  const quests = state?.quests?.list || [];
  const done = quests.filter((q) => q.complete).length;
  $("quest-count").textContent = quests.length
    ? `${quests.length} active${done ? ` · ${done} ready to turn in` : ""}` : "";
  if (!quests.length) {
    list.innerHTML = `<p class="empty">No quests in the log${state?.quests ? "" : " yet"}.</p>`;
    return;
  }
  let html = "", header;
  // Quests in the current zone first, then the rest in log order.
  const zone = state.snapshot?.zone;
  const ordered = [...quests].sort((a, b) => (b.header === zone) - (a.header === zone));
  for (const q of ordered) {
    if (q.header !== header) {
      header = q.header;
      if (header) html += `<div class="quest-group">${esc(header)}</div>`;
    }
    const cls = q.failed ? "failed" : q.complete ? "complete" : "";
    const status = q.failed ? "Failed" : q.complete ? "Ready to turn in" : "";
    html += `<div class="quest ${cls}">
      <div class="quest-title"><span>${esc(q.title)}</span>
        <span class="lvl">${q.level ? `Lv ${q.level}` : ""}</span></div>
      ${status ? `<div class="state">${status}</div>` : ""}
      ${q.objectives.map((o) => {
        const p = o.need ? pct(o.have, o.need) : o.done ? 100 : 0;
        // The count is shown separately: drop "3/10 " prefixes and ": 3/10" suffixes.
        const label = o.text.replace(/^\s*\d+\s*\/\s*\d+\s*/, "").replace(/:\s*\d+\s*\/\s*\d+\s*$/, "");
        return `<div class="objective ${o.done ? "done" : ""}">
          <div class="row"><span>${esc(label)}</span><span>${o.need ? `${o.have}/${o.need}` : o.done ? "✓" : ""}</span></div>
          ${o.need ? `<div class="mini"><div style="width:${p}%"></div></div>` : ""}
        </div>`;
      }).join("")}
    </div>`;
  }
  list.innerHTML = html;
}

// --- equipment (laid out like the in-game character pane) -----------------------

// Inventory slot ids per column of the character pane (4 = shirt, 19 = tabard).
const PANE = {
  left: [[1, "Head"], [2, "Neck"], [3, "Shoulder"], [15, "Back"], [5, "Chest"], [4, "Shirt"], [19, "Tabard"], [9, "Wrist"]],
  right: [[10, "Hands"], [6, "Waist"], [7, "Legs"], [8, "Feet"], [11, "Finger"], [12, "Finger"], [13, "Trinket"], [14, "Trinket"]],
  bottom: [[16, "Main hand"], [17, "Off hand"], [18, "Ranged"]],
};
const COSMETIC_SLOTS = new Set([4, 19]);
const ICON_URL = (name) => `https://wow.zamimg.com/images/wow/icons/large/${name}.jpg`;
const RACE_TOKENS = { "night elf": "nightelf", undead: "scourge", "blood elf": "bloodelf" };
// For items Wowhead doesn't know (Forever-only items): a plain icon for the slot.
const SLOT_ICONS = {
  1: "inv_helmet_01", 2: "inv_jewelry_necklace_01", 3: "inv_shoulder_01", 15: "inv_misc_cape_01",
  5: "inv_chest_cloth_01", 4: "inv_shirt_01", 19: "inv_shirt_guildtabard_01", 9: "inv_bracer_01",
  10: "inv_gauntlets_01", 6: "inv_belt_01", 7: "inv_pants_01", 8: "inv_boots_01", 11: "inv_jewelry_ring_01",
  12: "inv_jewelry_ring_01", 13: "inv_jewelry_talisman_01", 14: "inv_jewelry_talisman_01",
  16: "inv_sword_01", 17: "inv_shield_01", 18: "inv_weapon_bow_01",
};

// The game counts rage in tenths (max 1000); its UI shows 0-100.
function shownPower(p) {
  if (p?.type === "RAGE" && p.max >= 1000) {
    const tenth = (v) => (v == null ? v : Math.round(v / 10));
    return { ...p, cur: tenth(p.cur), max: tenth(p.max) };
  }
  return p || {};
}

// Item id -> icon name, from Wowhead's public item data (Forever items match
// Classic ids). Remembered in the browser so each item is looked up once.
let icons = {};
try { icons = JSON.parse(localStorage.getItem("ft-icons") || "{}"); } catch { /* private mode */ }
const iconLookups = new Map();

function itemIcon(id) {
  if (!id) return Promise.resolve(null);
  if (id in icons) return Promise.resolve(icons[id]);
  if (!iconLookups.has(id)) {
    iconLookups.set(id, fetch(`https://nether.wowhead.com/classic/tooltip/item/${id}`)
      .then((r) => (r.ok ? r.json() : {}))
      .then((d) => {
        const name = /^[a-z0-9_]+$/i.test(d.icon || "") ? d.icon.toLowerCase() : null;
        icons[id] = name;
        try { localStorage.setItem("ft-icons", JSON.stringify(icons)); } catch { /* full or blocked */ }
        return name;
      })
      .catch(() => null));
  }
  return iconLookups.get(id);
}

function racePortrait(s) {
  const race = (s.race || "").toLowerCase();
  const token = RACE_TOKENS[race] || race.replace(/[^a-z]/g, "");
  return token ? ICON_URL(`race_${token}_${s.sex === 3 ? "female" : "male"}`) : null;
}

function portraitHtml(s) {
  const portrait = racePortrait(s);
  return `${portrait ? `<img src="${portrait}" alt="" onerror="this.remove()">` : ""}`
    + `<span>${esc((s.class || "?").charAt(0))}</span>`;
}

function slotHtml([slot, label], it) {
  if (!it) return `<span class="slot empty" title="${label}"><span>${label}</span></span>`;
  const q = Number.isInteger(it.quality) ? Math.min(Math.max(it.quality, 0), 7) : 1;
  const title = `${it.name || `Item ${it.id ?? ""}`}${it.ilvl ? ` · item level ${it.ilvl}` : ""}`;
  // icons[id]: a name, null when Wowhead doesn't have the item, missing until looked up.
  const known = it.id && icons[it.id];
  const unknown = !it.id || icons[it.id] === null;
  const img = known || unknown ? `<img src="${ICON_URL(known || SLOT_ICONS[slot])}" alt="" loading="lazy">` : "";
  // Enchant and random suffix ("of the Bear") make Wowhead's tooltip match the real item.
  const extra = [it.enchant && `ench=${it.enchant}`, it.suffix && `rand=${it.suffix}`].filter(Boolean).join("&");
  return `<a class="slot q${q}" data-item="${it.id ?? ""}" title="${esc(title)}"
      ${unknown ? "" : `href="https://www.wowhead.com/classic/item=${it.id}${extra ? `?${extra}` : ""}" target="_blank" rel="noopener"`}
      ${extra ? `data-wowhead="${extra}"` : ""}>
    ${img || `<span class="letter">${esc((it.name || "?").charAt(0))}</span>`}
  </a>`;
}

// Only redraw what changed: a redraw under the mouse would close the item tooltip.
const drawn = {};
function changed(key, value) {
  const v = JSON.stringify(value);
  if (drawn[key] === v) return false;
  drawn[key] = v;
  return true;
}

function renderGear() {
  const s = state?.snapshot || {};
  const items = state?.gear?.items || [];
  const bySlot = new Map(items.map((it) => [it.slot, it]));
  if (changed("slots", items)) {
    for (const [col, slots] of Object.entries(PANE)) {
      $(`slots-${col}`).innerHTML = slots.map((def) => slotHtml(def, bySlot.get(def[0]))).join("");
    }
  }
  // Fill in icons as their lookups come back.
  for (const it of items) {
    if (it.id && !(it.id in icons)) {
      itemIcon(it.id).then((name) => {
        const el = document.querySelector(`.slot[data-item="${it.id}"]`);
        if (!el || el.querySelector("img")) return;
        if (!name) el.removeAttribute("href");  // not on Wowhead: no tooltip to link to
        el.innerHTML = `<img src="${ICON_URL(name || SLOT_ICONS[it.slot])}" alt="" loading="lazy">`;
      });
    }
  }

  if (changed("doll-portrait", [racePortrait(s), s.class])) $("doll-portrait").innerHTML = portraitHtml(s);
  $("doll-title").innerHTML = s.level != null
    ? `<b>Level ${s.level}</b> ${esc(s.race || "")} <span class="cls">${esc(s.class || "")}</span>` : "";

  // Average item level over real gear slots; shirt and tabard don't count.
  const levels = items.filter((it) => !COSMETIC_SLOTS.has(it.slot) && it.ilvl).map((it) => it.ilvl);
  const avg = levels.length ? Math.round(levels.reduce((a, b) => a + b, 0) / levels.length) : null;
  const p = shownPower(s.power);
  const rows = [
    ["Health", s.healthMax != null ? fmt.format(s.healthMax) : null],
    [(POWER[p.type] || [p.type || "Power"])[0], p.max != null ? fmt.format(p.max) : null],
    ["Item level", avg],
    ["Equipped", `${items.filter((it) => !COSMETIC_SLOTS.has(it.slot)).length} / 17`],
    ["Gold", s.money != null ? money(s.money) : null],
  ].filter(([, v]) => v != null);
  $("doll-stats").innerHTML = rows.map(([k, v]) => `<div><dt>${esc(k)}:</dt><dd>${v}</dd></div>`).join("");
  $("gear-note").textContent = !items.length ? "No equipment received yet"
    : state.gear.t ? `Changed ${ago(state.gear.t)} · hover an item` : "Hover an item for its tooltip";
}

// --- professions ---------------------------------------------------------------

const PROF_ICONS = {
  alchemy: "trade_alchemy", blacksmithing: "trade_blacksmithing", enchanting: "trade_engraving",
  engineering: "trade_engineering", herbalism: "trade_herbalism", leatherworking: "trade_leatherworking",
  mining: "trade_mining", skinning: "inv_misc_pelt_wolf_01", tailoring: "trade_tailoring",
  jewelcrafting: "inv_misc_gem_01", inscription: "inv_inscription_tradeskill01",
  cooking: "inv_misc_food_15", "first aid": "spell_holy_sealofsacrifice", fishing: "trade_fishing",
};

// The training tier a skill cap belongs to.
function tier(max) {
  return max <= 75 ? "Apprentice" : max <= 150 ? "Journeyman" : max <= 225 ? "Expert" : "Artisan";
}

function profHtml(p) {
  const icon = PROF_ICONS[p.name.toLowerCase()];
  const maxed = p.max && p.rank >= p.max;
  return `<div class="prof">
    <span class="prof-icon">${icon ? `<img src="${ICON_URL(icon)}" alt="" loading="lazy" onerror="this.remove()">` : ""}${icon ? "" : esc(p.name.charAt(0))}</span>
    <div>
      <div class="vital-label">
        <span>${esc(p.name)}${p.max ? `<span class="tier">${tier(p.max)}</span>` : ""}</span>
        <span>${p.rank ?? "–"}${p.bonus ? ` <span class="bonus">(+${p.bonus})</span>` : ""}${p.max ? ` / ${p.max}` : ""}</span>
      </div>
      <div class="bar skill${maxed ? " maxed" : ""}"><div class="fill" style="width:${p.max ? pct(p.rank, p.max) : 0}%"></div></div>
    </div>
  </div>`;
}

function renderProfessions() {
  const list = state?.professions?.items;
  if (!changed("professions", list ?? null)) return;
  if (!list) {
    $("prof-list").innerHTML = `<p class="empty">No professions received yet.</p>`;
    $("prof-note").textContent = "";
    return;
  }
  const primary = list.filter((p) => !p.secondary);
  const secondary = list.filter((p) => p.secondary);
  $("prof-note").textContent = `${primary.length} of 2 professions`;
  $("prof-list").innerHTML = (primary.length ? primary.map(profHtml).join("") : `<p class="empty">No professions learned.</p>`)
    + (secondary.length ? `<div class="prof-group">Secondary skills</div>${secondary.map(profHtml).join("")}` : "");
}

function describe(e) {
  switch (e.type) {
    case "level": return [`Reached level ${e.level}`, e.zone];
    case "death": return [`Died at level ${e.level}`, [e.subzone, e.zone].filter(Boolean).join(", ")];
    case "zone": return [`Entered ${e.zone}`, e.level ? `Level ${e.level}` : ""];
    case "quest": return [e.title || `Quest #${e.questID}`,
      ["Completed", e.xp ? `${fmt.format(e.xp)} XP` : "", e.money ? moneyText(e.money) : ""].filter(Boolean).join(" · ")];
    case "accept": return [e.title || `Quest #${e.questID}`, "Accepted"];
    case "abandon": return [e.title || `Quest #${e.questID}`, "Abandoned"];
    case "login": return ["Logged in", [e.zone, e.level && `Level ${e.level}`].filter(Boolean).join(" · ")];
    case "skill": return [e.learned ? `Learned ${e.skill}` : `${e.skill} reached ${e.rank}`,
      e.max ? `${e.rank} / ${e.max} · ${tier(e.max)}` : ""];
    default: return [e.type, ""];
  }
}

function renderJournal() {
  const list = $("events");
  const types = FILTER_GROUPS[journalFilter] || [journalFilter];
  const items = history.events.filter((e) => journalFilter === "all" || types.includes(e.type));
  if (!items.length) {
    list.innerHTML = `<li class="empty">Nothing here yet.</li>`;
    return;
  }
  list.innerHTML = items.map((e) => {
    const [title, detail] = describe(e);
    return `<li class="${esc(e.type)}">
      <span class="icon">${EVENT_ICONS[e.type] || "•"}</span>
      <span class="what"><span>${esc(title)}</span><span class="detail">${esc(detail)}</span></span>
      <time datetime="${new Date(e.t * 1000).toISOString()}">${clock(e.t)}</time>
    </li>`;
  }).join("");
}

// Cheap per-second update from the live stream.
function renderLive() {
  renderPresence();
  const s = state?.snapshot;
  if (!s) return;
  renderHero(s);
  renderProgress(s);
  renderVitals(s);
  renderMe(s);
}

function renderAll() {
  renderPresence();
  renderQuests();
  renderGear();
  renderProfessions();
  const s = state?.snapshot;
  if (!s) return;
  renderLive();
  renderTrail(s);
  renderChart();
  renderJournal();
}

// --- characters ------------------------------------------------------------------

const forChar = () => (selected ? `?c=${encodeURIComponent(selected)}` : "");

function renderRoster() {
  $("roster").innerHTML = characters.map((c) => {
    const color = CLASS_COLORS[c.classToken] || "var(--gold)";
    const seen = c.online ? "Online now" : c.updatedAt ? `Seen ${ago(c.updatedAt)}` : "";
    return `<button type="button" data-id="${esc(c.id)}" class="${c.id === selected ? "on" : ""}" style="--c:${color}"
        title="${esc([c.name, c.surname].filter(Boolean).join(" "))} · ${esc(c.realm || "")}">
      <span class="mini-crest${c.online ? " online" : ""}">${portraitHtml(c)}</span>
      <span class="label"><b>${esc(c.name)}</b>
        <span>${c.level ? `Level ${c.level} ` : ""}${esc(c.class || "")}</span>
        <span class="seen${c.online ? " online" : ""}">${seen}</span></span>
    </button>`;
  }).join("");
}

async function loadCharacters() {
  try {
    characters = await fetch("api/characters").then((r) => (r.ok ? r.json() : []));
  } catch (err) {
    console.error("character list failed", err);
  }
  renderRoster();
}

function selectCharacter(id) {
  if (!id || id === selected) return;
  selected = id;
  const url = new URL(location.href);
  url.searchParams.set("c", id);
  window.history.replaceState(null, "", url); // `history` here is the trail data
  state = null;
  history = { positions: [], xp: [], events: [] };
  shownMap = null;
  delete $("map-me").dataset.placed; // no gliding from the last character's spot
  renderRoster();
  load();
  connect();
}

$("roster").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-id]");
  if (btn) selectCharacter(btn.dataset.id);
});

// --- data --------------------------------------------------------------------

async function load() {
  const asked = selected;
  try {
    const [st, hist] = await Promise.all([
      fetch(`api/state${forChar()}`).then((r) => r.json()),
      fetch(`api/history${forChar()}`).then((r) => r.json()),
    ]);
    if (asked !== selected) return; // switched character while this was loading
    state = st;
    history = hist;
    if (!selected && st.character) {
      selected = st.character; // the last played, chosen by the server
      renderRoster();
    }
    renderAll();
  } catch (err) {
    console.error("load failed", err);
  }
}

function connect() {
  if (!window.EventSource) return;
  stream?.close();
  const es = stream = new EventSource(`api/stream${forChar()}`);
  es.addEventListener("update", () => { load(); loadCharacters(); });
  es.addEventListener("live", (ev) => {
    const { quests, gear, professions } = state || {};
    state = JSON.parse(ev.data);
    Object.assign(state, { quests, gear, professions });
    renderLive();
    renderGear();
  });
  // After a dropped connection, catch up on anything missed.
  es.addEventListener("open", load);
}

// Map tooltips (delegated so re-renders don't need re-binding).
$("map-svg").addEventListener("pointerover", (ev) => {
  const target = ev.target.closest("[data-tip]");
  const tip = $("map-tip");
  if (!target) { tip.hidden = true; return; }
  const r = $("map").getBoundingClientRect();
  const c = target.getBoundingClientRect();
  tip.textContent = target.dataset.tip;
  tip.style.left = `${c.left + c.width / 2 - r.left}px`;
  tip.style.top = `${c.top - r.top}px`;
  tip.hidden = false;
});
$("map-svg").addEventListener("pointerleave", () => { $("map-tip").hidden = true; });

$("chips").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".chip");
  if (!btn) return;
  journalFilter = btn.dataset.filter;
  for (const c of $("chips").children) c.classList.toggle("on", c === btn);
  renderJournal();
});

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderChart, 150);
});

fetch("maps/index.json")
  .then((r) => (r.ok ? r.json() : {}))
  .catch(() => ({}))
  .then((idx) => { mapIndex = idx; })
  .then(() => loadCharacters())
  .finally(() => { load(); connect(); });
// Presence expires server-side without a push, so re-check now and then; the
// character list also shows who's online.
setInterval(load, 60_000);
setInterval(loadCharacters, 15_000);
