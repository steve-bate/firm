/**
 * SseClient – reusable SSE client for the fedi-spikes streaming API.
 *
 * Handles:
 *   • topic management  (GET/POST/DELETE /sse/control/subscriptions)
 *   • stream ticket issuance         (POST /sse/control)
 *   • EventSource lifecycle          (start / stop)
 *
 * All side-effects (DOM, UI state) are left to the caller via callbacks.
 *
 * @example
 *   import { SseClient } from '/static/js/sse-client.js';
 *
 *   const client = new SseClient({
 *     onEvent(type, data, id)   { console.log(type, data); },
 *     onStatusChange(status)    { console.log('status:', status); },
 *     onError(msg)              { console.error(msg); },
 *     onTopicsChange(topics)    { console.log([...topics]); },
 *   });
 *
 *   await client.subscribe('my-topic');
 *   await client.start();
 *   // later…
 *   client.stop();
 */
export class SseClient {
  /** @type {Set<string>} */
  #topics = new Set();

  /** @type {EventSource|null} */
  #evtSource = null;

  #urls;
  #onEvent;
  #onStatusChange;
  #onError;
  #onTopicsChange;

  /**
   * @param {object}   [opts]
   * @param {string}   [opts.subscriptionsUrl='/sse/control/subscriptions']
   * @param {string}   [opts.controlUrl='/sse/control']
   * @param {function} [opts.onEvent]        Called with (eventType, parsedData, lastEventId)
   * @param {function} [opts.onStatusChange] Called with 'connecting'|'connected'|'disconnected'|'error'
   * @param {function} [opts.onError]        Called with an error message string
   * @param {function} [opts.onTopicsChange] Called with a (readonly) copy of the topics Set
   */
  constructor({
    subscriptionsUrl = '/sse/control/subscriptions',
    controlUrl       = '/sse/control',
    onEvent          = () => {},
    onStatusChange   = () => {},
    onError          = () => {},
    onTopicsChange   = () => {},
  } = {}) {
    this.#urls          = { subscriptionsUrl, controlUrl };
    this.#onEvent       = onEvent;
    this.#onStatusChange = onStatusChange;
    this.#onError       = onError;
    this.#onTopicsChange = onTopicsChange;
  }

  /** Returns a snapshot copy of the current topic set. */
  get topics() {
    return new Set(this.#topics);
  }

  /** True while an EventSource connection is open. */
  get streaming() {
    return this.#evtSource !== null;
  }

  /**
   * Fetch the current server-side subscription list.  Throws on HTTP error.
   * @returns {Promise<string[]>}
   */
  async getSubscriptions() {
    const res = await fetch(this.#urls.subscriptionsUrl, { credentials: 'include' });
    if (!res.ok) throw new Error(await res.text());
    const { topics } = await res.json();
    return topics;
  }

  /**
   * Subscribe to a topic.  Throws on HTTP error.
   * @param {string} topic
   */
  async subscribe(topic) {
    const res = await fetch(this.#urls.subscriptionsUrl, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ topic }),
    });
    if (!res.ok) throw new Error(await res.text());
    this.#topics.add(topic);
    this.#onTopicsChange(this.topics);
  }

  /**
   * Unsubscribe from a topic.  Throws on HTTP error.
   * @param {string} topic
   */
  async unsubscribe(topic) {
    const url = new URL(this.#urls.subscriptionsUrl, location.origin);
    url.searchParams.set('topic', topic);
    const res = await fetch(url.toString(), { method: 'DELETE', credentials: 'include' });
    if (!res.ok) throw new Error(await res.text());
    this.#topics.delete(topic);
    this.#onTopicsChange(this.topics);
  }

  /**
   * Issue a stream ticket then open the EventSource connection.
   * Fires onStatusChange('connecting') immediately, then 'connected' on open
   * or 'error' if the connection fails.
   * Throws if the ticket request fails.
   */
  async start() {
    // Obtain a short-lived ticket; the server sets it as a cookie and
    // returns the stream URL and other metadata.
    const ticketRes = await fetch(this.#urls.controlUrl, {
      method:      'POST',
      credentials: 'include',
    });
    if (!ticketRes.ok) {
      const body = await ticketRes.json().catch(() => ({ detail: ticketRes.statusText }));
      throw new Error(body.detail || ticketRes.statusText);
    }
    const { stream_url: streamUrl } = await ticketRes.json();

    this.#onStatusChange('connecting');

    // withCredentials ensures the ticket cookie is sent cross-origin.
    this.#evtSource = new EventSource(streamUrl, { withCredentials: true });

    const handleEvent = e => {
      let data;
      try   { data = JSON.parse(e.data); }
      catch { data = { topic: '?', time: '?' }; }
      this.#onEvent(e.type, data, e.lastEventId);
    };

    this.#evtSource.addEventListener('notification',  handleEvent);
    this.#evtSource.addEventListener('message', handleEvent);

    this.#evtSource.onopen = () => {
      console.log('[SseClient] connection opened');
      this.#onStatusChange('connected');
    };

    this.#evtSource.onerror = () => {
      console.error('[SseClient] connection error', this.#evtSource);
      this.#evtSource = null;
      this.#onStatusChange('error');
      this.#onError('Stream error or server closed the connection.');
    };
  }

  /** Close the EventSource connection if open. */
  stop() {
    if (this.#evtSource) {
      this.#evtSource.close();
      this.#evtSource = null;
    }
    this.#onStatusChange('disconnected');
  }

  /**
   * Revoke the current session ticket (DELETE /sse/control) and stop the
   * stream.  The server invalidates the ticket and clears the cookie.
   * Throws on HTTP error.
   */
  async revoke() {
    this.stop();
    const res = await fetch(this.#urls.controlUrl, {
      method:      'DELETE',
      credentials: 'include',
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(body.detail || res.statusText);
    }
  }
}
