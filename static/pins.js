'use strict';

// Keep the dashboard and map on the same store. Preserve legacy numeric IDs.
window.mk1Pins = (() => {
  const key = 'mk1-pins';

  function read() {
    const pins = JSON.parse(localStorage.getItem(key) || '[]');
    if (!Array.isArray(pins)) throw new Error('Saved pins are invalid.');
    return pins;
  }

  function valid(pin) {
    return pin && (Number.isSafeInteger(pin.id) || (typeof pin.id === 'string' && pin.id.length > 0))
      && Number.isFinite(pin.lat) && Number.isFinite(pin.lon)
      && Math.abs(pin.lat) <= 90 && Math.abs(pin.lon) <= 180;
  }

  function update(change) {
    const write = () => {
      const next = change(read());
      localStorage.setItem(key, JSON.stringify(next));
      return next;
    };
    // Localhost and other secure contexts serialize writes across tabs.
    return navigator.locks ? navigator.locks.request(key, write) : Promise.resolve().then(write);
  }

  function newId() {
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte =>
      byte.toString(16).padStart(2, '0')).join('');
  }

  return { key, read, valid, update, newId };
})();
