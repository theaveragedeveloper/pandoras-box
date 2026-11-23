// The hub is authoritative. The browser keeps a snapshot and queues only
// user actions that still need to reach the hub. A snapshot never gets
// uploaded as new data, apart from the gated one-time legacy migration.
window.mk1Pins = (function () {
  const key = 'mk1-pins';
  let inTab = Promise.resolve();

  function valid(pin) {
    if (!pin || typeof pin !== 'object') return false;
    const id = pin.id;
    if (!(typeof id === 'string' && id.trim() && id.trim().length <= 128) &&
        !(Number.isSafeInteger(id) && id >= 0)) return false;
    return Number.isFinite(pin.lat) && Math.abs(pin.lat) <= 90 &&
      Number.isFinite(pin.lon) && Math.abs(pin.lon) <= 180;
  }

  function readState() {
    let value;
    try { value = JSON.parse(localStorage.getItem(key)); }
    catch { /* Invalid or inaccessible storage has no snapshot. */ }
    const pins = items => Array.isArray(items) ? items.filter(valid) : [];
    return {
      cache: pins(Array.isArray(value) ? value : value?.cache),
      legacy: pins(Array.isArray(value) ? value : value?.legacy),
      recovery: pins(value?.recovery),
      pending: Array.isArray(value?.pending) ? value.pending.filter(op =>
        ['add', 'update', 'delete'].includes(op?.action) && valid(op?.pin)) : []
    };
  }

  function save(state) {
    localStorage.setItem(key, JSON.stringify(state));
  }

  function read() { return readState().cache; }
  function recovered() { return readState().recovery; }

  function newId() {
    return Array.from(crypto.getRandomValues(new Uint8Array(8)), b => b.toString(16).padStart(2, '0')).join('');
  }

  function serial(operation) {
    if (navigator.locks) return navigator.locks.request(key, operation);
    const result = inTab.then(operation);
    inTab = result.catch(() => {});
    return result;
  }

  function snapshot(data) {
    if (!Array.isArray(data?.pins) || !data.pins.every(valid)) {
      throw new Error('The hub returned an invalid pin list.');
    }
    return data.pins;
  }

  async function fetchState() {
    const response = await fetch('/api/pins', { cache: 'no-store', signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw new Error('Could not load the hub pin list.');
    const data = await response.json();
    snapshot(data);
    return data;
  }

  async function fetchServer() { return snapshot(await fetchState()); }

  async function postAction(body) {
    const response = await fetch('/api/pins', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), signal: AbortSignal.timeout(8000)
    });
    let data;
    try { data = await response.json(); } catch { /* The status still explains the failure. */ }
    if (!response.ok) {
      const error = new Error(data?.error || 'Could not save pins to the hub.');
      error.status = response.status;
      throw error;
    }
    snapshot(data);
    return data;
  }

  const idOf = pin => String(pin.id).trim();
  const byId = pins => new Map(pins.map(pin => [idOf(pin), pin]));

  function overlay(pins, pending) {
    const merged = byId(pins);
    for (const { action, pin } of pending) {
      const id = idOf(pin);
      if (action === 'delete') merged.delete(id);
      else if (action === 'add' || merged.has(id)) merged.set(id, pin);
    }
    return Array.from(merged.values());
  }

  async function syncLocked() {
    const state = readState();
    let server;
    try { server = await fetchState(); }
    catch { return state.cache; }

    let pins = snapshot(server);
    if (state.legacy.length) {
      // The server only accepts this action on a genuinely new store.
      // Ambiguous old caches are kept separately for deliberate export.
      if (server.legacy_migration === true) {
        pins = snapshot(await postAction({ action: 'migrate', pins: Array.from(byId(state.legacy).values()) }));
      }
      const known = byId(pins);
      state.recovery = Array.from(byId(state.recovery.concat(
        state.legacy.filter(pin => !known.has(idOf(pin))))).values());
      state.legacy = [];
    }
    state.cache = overlay(pins, state.pending);
    save(state);
    return flush(state);
  }

  async function flush(state) {
    while (state.pending.length) {
      const { action, pin } = state.pending[0];
      let pins;
      try {
        const body = action === 'delete' ? { action, id: pin.id } : { action, pin };
        pins = snapshot(await postAction(body));
      } catch (error) {
        if (error.status !== 400) throw error;
        const duplicate = action === 'add' && /already exists/i.test(error.message);
        const missing = action !== 'add' && /no pin with id/i.test(error.message);
        pins = await fetchServer();
        state.pending.shift();
        if (!duplicate && !missing) {
          if (action !== 'delete') {
            state.recovery = Array.from(byId(state.recovery.concat(pin)).values());
          }
          state.cache = overlay(pins, state.pending);
          save(state);
          throw error;
        }
        state.cache = overlay(pins, state.pending);
        save(state);
        continue;
      }
      state.pending.shift();
      state.cache = overlay(pins, state.pending);
      save(state);
    }
    return state.cache;
  }

  function sync() { return serial(syncLocked); }

  function update(change) {
    return serial(async () => {
      const state = readState();
      const before = byId(state.cache);
      const next = change(state.cache).filter(valid);
      const after = byId(next);
      for (const [id, pin] of after) {
        const old = before.get(id);
        if (!old) state.pending.push({ action: 'add', pin });
        else if (old.lat !== pin.lat || old.lon !== pin.lon || (old.label || '') !== (pin.label || '') ||
                 (old.address || '') !== (pin.address || '')) state.pending.push({ action: 'update', pin });
      }
      for (const [id, pin] of before) {
        if (!after.has(id)) state.pending.push({ action: 'delete', pin });
      }
      state.cache = next;
      save(state);
      if (state.legacy.length) {
        const pins = await syncLocked();
        if (readState().pending.length) throw new Error('Pin changes are saved in this browser and will sync when the hub returns.');
        return pins;
      }
      return flush(state);
    });
  }

  function importPins(pins, replace) {
    return serial(async () => {
      await syncLocked();
      const data = await postAction({ action: 'import', pins, replace: replace === true });
      const state = readState();
      state.cache = snapshot(data);
      state.pending = [];
      state.legacy = [];
      save(state);
      return data;
    });
  }

  return { key, read, recovered, valid, update, newId, fetchServer, importPins, sync };
})();
