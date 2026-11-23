// mk1Pins — map pins store.
//
// The hub is the source of truth: pins live in the hub's SQLite
// store (pins.db, /api/pins) so they survive a browser data clear,
// a different browser, and a laptop restart. This browser's
// localStorage is a fast local cache, kept in sync with the server.
//
// Load-time sync (call sync() on start): fetch the server's pins and
// MERGE with the local cache by id (union; the server's version wins
// a conflict). Local-only pins are pushed to the server with the
// merge import action, so a pin saved while the hub was briefly down
// is not lost on the next reload. If the server is unreachable (the
// fetch fails OR returns non-OK), the local cache is used as-is and
// nothing is migrated — only a positive empty response from the
// server triggers the one-time migration, and that migration merges
// (import, replace:false), never replaces.
//
// Every pin change (update()) is written to localStorage and then
// pushed to the server with the per-pin add/update/delete actions —
// never a full-list replace, so two browsers (or a stale tab) cannot
// delete each other's pins. A failed server push rejects the update
// promise so the caller can tell the user; the local change is kept,
// and the next sync() merges it back to the server.
//
// The full-list replace is reserved for an explicit import-with-
// replace: use importPins(pins, true) / replaceServer(pins) for that,
// never update().
//
// Pin format (unchanged): {id, lat, lon, label, address}.
// label is the display name (e.g. "Mom's house"); address is the
// geocoded display address when the pin was saved from an address
// lookup ("" otherwise). ids are random hex strings from newId();
// legacy numeric ids are preserved as strings after a server
// round-trip (the id is only ever used for identity).
window.mk1Pins = (function () {
  const key = 'mk1-pins';

  function read() {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
  }

  function valid(pin) {
    if (!pin || typeof pin !== 'object') return false;
    if (typeof pin.id === 'number') {
      if (!Number.isSafeInteger(pin.id)) return false;
    } else if (typeof pin.id !== 'string' || !pin.id) {
      return false;
    }
    return (
      Number.isFinite(pin.lat) && Math.abs(pin.lat) <= 90 &&
      Number.isFinite(pin.lon) && Math.abs(pin.lon) <= 180
    );
  }

  function newId() {
    return Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, '0')).join('');
  }

  function samePin(a, b) {
    return a && b && a.lat === b.lat && a.lon === b.lon &&
      (a.label || '') === (b.label || '') &&
      (a.address || '') === (b.address || '');
  }

  // -- server ----------------------------------------------------------
  // The pages that load this file are served by the hub, so these are
  // same-origin requests. POSTs send Origin, which the hub checks.
  async function fetchServer() {
    const r = await fetch('/api/pins', { cache: 'no-store' });
    if (!r.ok) throw new Error('pins fetch failed');
    const data = await r.json();
    return Array.isArray(data.pins) ? data.pins : [];
  }

  async function postAction(body) {
    const r = await fetch('/api/pins', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await r.json(); } catch (e) { /* non-JSON error body */ }
    if (!r.ok) {
      const err = new Error((data && data.error) || 'pins save failed');
      err.status = r.status;
      throw err;
    }
    return data;
  }

  // Full-list replace. Explicit import-with-replace ONLY — never for
  // a single-pin change (use the per-pin actions via update()).
  // Kept under its old name: the smoke test checks it exists.
  async function pushServer(pins) {
    const data = await postAction({ action: 'replace', pins: pins });
    return Array.isArray(data.pins) ? data.pins : [];
  }

  // Explicit replace under a clearer name (same endpoint as pushServer).
  async function replaceServer(pins) {
    return pushServer(pins);
  }

  // Merge pins into the server store (import action, replace:false
  // by default). Used by sync() for migration and for pushing
  // local-only pins; with replace=true it is an explicit
  // import-with-replace. Returns {added, skipped, pins}.
  async function importPins(pins, replace) {
    const data = await postAction({
      action: 'import', pins: pins, replace: replace === true,
    });
    return data || {};
  }

  // One per-pin server call. Idempotent where the server allows it:
  // an add that already exists falls back to update, an update of a
  // missing pin falls back to add, and deleting a pin that is already
  // gone counts as done — so a stale local cache cannot fail the
  // push, but a real server failure still rejects.
  async function pushPin(action, pin) {
    try {
      if (action === 'delete') {
        await postAction({ action: 'delete', id: pin.id });
      } else {
        await postAction({ action: action, pin: pin });
      }
      return;
    } catch (e) {
      const msg = String(e && e.message || '');
      if (action === 'add' && /already exists/i.test(msg)) {
        await postAction({ action: 'update', pin: pin });
        return;
      }
      if (action === 'update' && /no pin with id/i.test(msg)) {
        await postAction({ action: 'add', pin: pin });
        return;
      }
      if (action === 'delete' && /no pin with id/i.test(msg)) return;
      throw e;
    }
  }

  async function sync() {
    const local = read().filter(valid);
    let server;
    try {
      // A non-OK response throws here too: a failed read is "server
      // unreachable" — keep the local cache and do NOT migrate.
      server = (await fetchServer()).filter(valid);
    } catch (e) {
      server = null; // server unreachable — keep the local cache
    }
    if (server === null) return local;
    if (!server.length) {
      if (!local.length) return local;
      // One-time migration: the server positively reports an empty
      // store, so merge the local pins in (never replace).
      try {
        await importPins(local, false);
      } catch (e) {
        throw new Error('Could not save your saved places to the hub — they are still stored in this browser only.');
      }
      return local;
    }
    // Merge by id (union): pins the server has that this browser
    // lacks, plus pins this browser has that the server lacks. On a
    // conflict the server's version wins (it is the source of truth).
    const byId = new Map();
    local.forEach(function (p) { byId.set(String(p.id), p); });
    server.forEach(function (p) { byId.set(String(p.id), p); });
    const merged = Array.from(byId.values());
    const serverIds = new Set(server.map(function (p) { return String(p.id); }));
    const localOnly = merged.filter(function (p) { return !serverIds.has(String(p.id)); });
    if (localOnly.length) {
      // Push the merged set's local-only pins; surface a failure
      // instead of silently keeping a local-only copy.
      try {
        await importPins(localOnly, false);
      } catch (e) {
        try { localStorage.setItem(key, JSON.stringify(merged)); } catch (e2) {}
        throw new Error('Could not save your saved places to the hub — they are stored in this browser only for now.');
      }
    }
    try { localStorage.setItem(key, JSON.stringify(merged)); } catch (e) {}
    return merged;
  }

  function update(change) {
    const write = async () => {
      const before = read();
      const next = change(before);
      localStorage.setItem(key, JSON.stringify(next));
      // Push only the difference, with the per-pin actions — never a
      // full-list replace, so another browser's pins survive.
      const beforeById = new Map(before.filter(valid).map(function (p) { return [String(p.id), p]; }));
      const nextById = new Map(next.filter(valid).map(function (p) { return [String(p.id), p]; }));
      const ops = [];
      nextById.forEach(function (pin, id) {
        const old = beforeById.get(id);
        if (!old) ops.push(['add', pin]);
        else if (!samePin(old, pin)) ops.push(['update', pin]);
      });
      beforeById.forEach(function (pin, id) {
        if (!nextById.has(id)) ops.push(['delete', pin]);
      });
      for (const op of ops) {
        await pushPin(op[0], op[1]);
      }
      return next;
    };
    // Localhost and other secure contexts serialize writes across tabs.
    if (navigator.locks) return navigator.locks.request(key, write);
    return Promise.resolve().then(write);
  }

  return { key, read, valid, update, newId, fetchServer, pushServer, replaceServer, importPins, sync };
})();
