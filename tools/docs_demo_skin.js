/* MixMill documentation demo skin.
 *
 * Paste into the browser console of a running MixMill instance before taking
 * screenshots for docs/USER_GUIDE.md. It rewrites API responses in the page so
 * real program names, release titles, song names and library paths are replaced
 * with neutral demo values, and masks video frames and choreography pages.
 *
 * Display only. Nothing is written back to the server or the database.
 * Reload the page to remove it.
 */
(() => {
  if (window.__demoSkin) { window.__demoSkin.refresh(); return "demo skin refreshed"; }

  // Program names are learned from the instance, never listed here, so this file
  // says nothing about which releases a library holds.
  const PROGRAM_POOL = [
    "PULSE", "STRIKE", "SCULPT", "FLOW", "FORGE", "DRIVE", "STRIDE", "REACH",
  ];
  const SONGS = [
    "Rise Up", "Night Drive", "Open Road", "Gold Rush", "Paper Planes",
    "Slow Burn", "Wild Signal", "Clockwork", "Neon Tide", "Iron Will",
    "Midnight Run", "Afterglow", "Blue Static", "Free Fall", "Echo Park",
    "Hard Reset", "Long Way Home", "Second Wind", "Glass House", "Rooftop",
  ];

  const hash = (s) => {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h;
  };
  // A program is replaced everywhere it appears, including inside titles, paths
  // and song names, whatever spacing the source used ("POWERHOUR", "Power Hour").
  const known = new Map();
  const loosely = (name) =>
    new RegExp(
      name.replace(/\s+/g, "").split("").map((c) => c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s*"),
      "gi",
    );
  const learn = (name) => {
    const key = typeof name === "string" ? name.trim() : "";
    if (!key || known.has(key)) return;
    known.set(key, {
      re: loosely(key),
      to: PROGRAM_POOL[known.size] || `PROGRAM ${known.size + 1}`,
    });
  };
  const collect = (node) => {
    if (Array.isArray(node)) node.forEach(collect);
    else if (node && typeof node === "object") {
      if (typeof node.program === "string") learn(node.program);
      Object.values(node).forEach(collect);
    }
  };
  const programs = (s) => {
    let out = s;
    for (const { re, to } of known.values()) out = out.replace(re, to);
    return out;
  };

  // "POWERHOUR 13" -> "PULSE 13". Titles without a program word ("pick a side")
  // get a stable made-up number so no real release name survives.
  const releaseTitle = (title, program) => {
    const paren = (title.match(/\s*\([^)]*\)\s*$/) || [""])[0];
    const base = title.slice(0, title.length - paren.length);
    const found = base.match(/(\d{1,3})\s*$/);
    const num = found ? found[1] : String(1 + (hash(title) % 40));
    return `${programs(program)} ${num}${paren}`;
  };

  // "01 Tonight Is The Night Power Hour 13" -> "01 Rise Up"
  // Leading order numbers and track codes (H1, B2B) are kept: they are features.
  const songName = (s) => {
    if (typeof s !== "string" || !s.trim()) return s;
    const order = (s.match(/^[A-Za-z]?\d{1,2}[A-Za-z]?[\s._-]+/) || [""])[0];
    const rest = s.slice(order.length);
    const code = (rest.match(/\b([A-Z]\d[A-Z]?)\b/) || [])[1];
    const ext = (s.match(/\.(m4a|mp3|wav|flac|aac|ogg)$/i) || [""])[0];
    // hash the stem so a song and its filename get the same replacement
    const title = SONGS[hash(ext ? s.slice(0, -ext.length) : s) % SONGS.length];
    return `${order}${code ? code + " " : ""}${title}${ext}`;
  };

  const MIXES = [
    "Tuesday 45 Class", "Friday Express", "Endurance 50", "Coach Demo Mix",
    "Saturday Strength", "Short Warm-Up Set", "Cardio Test Mix", "Sunday Long Class",
  ];

  // Mix names are the user's own wording. Automatic generator names are kept so
  // the guide can show what generated mixes are called.
  const mixName = (name) =>
    /\d{4}-\d{2}-\d{2}/.test(name) ? programs(name) : MIXES[hash(name) % MIXES.length];

  const TRACKISH = new Set([
    "music_name", "song", "song_name", "music_file", "filename", "track_name",
  ]);
  // Real choreography PDFs are named after the release they belong to.
  const FILE_FIELDS = { source_name: "Choreography Notes.pdf" };
  const isSegment = (o) => o && typeof o === "object" && "start" in o && "end" in o;
  // /api/releases/{id}/music rows: {index, name, filename}
  const isMusicRow = (o) => o && typeof o === "object" && "index" in o && "filename" in o;
  // /api/releases/{id}/choreography-notes tracks[]: {track_id, name, mapping_mode}.
  // These carry the same tracks.name the segment rows do, so the notes studio and
  // the track table end up showing one track under one name.
  const isNotesRow = (o) => o && typeof o === "object" && "track_id" in o && "mapping_mode" in o;
  // Their options[] labels open with a heading read out of the PDF:
  // "02 MIXED IMPACT · pages 9-11 · 4:36". Only the heading is replaced — the
  // pages and durations are what the dropdown exists to show.
  const isNotesOption = (o) => o && typeof o === "object" && "recommended" in o && "page_start" in o;
  const optionLabel = (s) => {
    const [heading, ...rest] = s.split(" · ");
    return [songName(heading), ...rest].join(" · ");
  };

  const walk = (node, parent) => {
    if (Array.isArray(node)) return node.map((v) => walk(v, parent));
    if (node && typeof node === "object") {
      const out = {};
      if (typeof node.name === "string" && ("item_count" in node || Array.isArray(node.items))) {
        node = { ...node, name: mixName(node.name) };
      }
      if (typeof node.title === "string" && typeof node.program === "string") {
        const title = releaseTitle(node.title, node.program);
        const relpath = typeof node.relpath === "string"
          ? programs(node.relpath.split(node.title).join(title))
          : node.relpath;
        node = { ...node, title, relpath };
      }
      for (const [k, v] of Object.entries(node)) {
        if (typeof v === "string") {
          if (k in FILE_FIELDS) out[k] = FILE_FIELDS[k];
          else if (k === "title" || k === "relpath") out[k] = v;
          else if (k === "name" && ("item_count" in node || Array.isArray(node.items))) out[k] = v;
          else if (k === "label" && isNotesOption(node)) out[k] = programs(optionLabel(v));
          else if (TRACKISH.has(k)
            || (k === "name" && (isSegment(node) || isMusicRow(node) || isNotesRow(node)))) out[k] = programs(songName(v));
          else out[k] = programs(v);
        } else out[k] = walk(v, node);
      }
      return out;
    }
    return node;
  };

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const res = await realFetch(input, init);
    const type = res.headers.get("content-type") || "";
    if (!type.includes("application/json")) return res;
    let data;
    try { data = await res.clone().json(); } catch { return res; }
    collect(data);
    return new Response(JSON.stringify(walk(data, null)), {
      status: res.status, statusText: res.statusText, headers: res.headers,
    });
  };

  const cover = (seed) => {
    const h = hash(String(seed)) % 360;
    const svg =
      `<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270">` +
      `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">` +
      `<stop offset="0" stop-color="hsl(${h},32%,34%)"/>` +
      `<stop offset="1" stop-color="hsl(${(h + 40) % 360},30%,17%)"/>` +
      `</linearGradient></defs><rect width="480" height="270" fill="url(#g)"/>` +
      `<g fill="none" stroke="rgba(255,255,255,.34)" stroke-width="3">` +
      `<circle cx="240" cy="135" r="34"/><path d="M230 118l30 17-30 17z" fill="rgba(255,255,255,.34)" stroke="none"/>` +
      `</g></svg>`;
    return "data:image/svg+xml;utf8," + encodeURIComponent(svg);
  };

  const skinImages = (root) => {
    root.querySelectorAll("img.thumb, img[data-src*='/thumb']").forEach((img) => {
      const src = img.getAttribute("data-src") || img.getAttribute("src") || img.alt || "";
      if (img.dataset.demoSkinned) return;
      img.dataset.demoSkinned = "1";
      const url = cover(src);
      if (img.hasAttribute("data-src")) img.setAttribute("data-src", url);
      img.src = url;
    });
  };

  const maskVideos = (root) => {
    root.querySelectorAll("#rel-video, #seg-video, #mix-video").forEach((video) => {
      const parent = video.parentElement;
      if (!parent || parent.querySelector(".demo-mask")) return;
      if (getComputedStyle(parent).position === "static") parent.style.position = "relative";
      const mask = document.createElement("div");
      mask.className = "demo-mask";
      mask.innerHTML = `<span>Video preview</span>`;
      parent.appendChild(mask);
    });
  };

  const ready = (fn) => {
    if (document.body) fn();
    else document.addEventListener("DOMContentLoaded", fn, { once: true });
  };

  const style = document.createElement("style");
  style.id = "demo-skin-style";
  style.textContent = `
    .demo-mask {
      position: absolute; inset: 0 0 44px 0; pointer-events: none;
      display: grid; place-content: center; gap: 10px;
      background: linear-gradient(135deg, hsl(206,30%,26%), hsl(232,32%,13%));
      color: rgba(255,255,255,.62); font-size: 11px; letter-spacing: .16em;
      text-transform: uppercase;
    }
    .demo-mask::before {
      content: ""; justify-self: center; width: 0; height: 0;
      border-left: 22px solid rgba(255,255,255,.45);
      border-top: 14px solid transparent; border-bottom: 14px solid transparent;
    }
    #notes-preview-image, .notes-page-thumb img { filter: blur(5px); }
  `;
  const refresh = () => {
    skinImages(document);
    maskVideos(document);
  };
  window.__demoSkin = { refresh, cover, songName, programs, releaseTitle };

  // As a Playwright init script this runs before <body> exists.
  ready(() => {
    (document.head || document.documentElement).appendChild(style);
    new MutationObserver(refresh).observe(document.body, { childList: true, subtree: true });
    refresh();
  });
  return "demo skin installed";
})();
