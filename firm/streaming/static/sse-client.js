/**
 * SseClient – reusable SSE client for the fedi-spikes streaming API.
 *
 * Handles:
 *   • topic subscribe / unsubscribe  (POST /sse/control/subscribe|unsubscribe)
 *   • stream ticket issuance         (POST /sse/control/issue-ticket)
 *   • EventSource lifecycle          (start / stop)
 *
 * All side-effects (DOM, UI state) are left to the caller via callbacks.
 *
 * @example
 *   import { SseClient } from '/static/sse-client.js';
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
   * @param {string}   [opts.subscribeUrl='/sse/control/subscribe']
   * @param {string}   [opts.unsubscribeUrl='/sse/control/unsubscribe']
   * @param {string}   [opts.issueTicketUrl='/sse/control/issue-ticket']
   * @param {string}   [opts.streamUrl='/sse/stream']
   * @param {function} [opts.onEvent]        Called with (eventType, parsedData, lastEventId)
   * @param {function} [opts.onStatusChange] Called with 'connecting'|'connected'|'disconnected'|'error'
   * @param {function} [opts.onError]        Called with an error message string
   * @param {function} [opts.onTopicsChange] Called with a (readonly) copy of the topics Set
   */
  constructor({
    subscribeUrl   = '/sse/control/subscribe',
    unsubscribeUrl = '/sse/control/unsubscribe',
    issueTicketUrl = '/sse/control/issue-ticket',
    streamUrl      = '/sse/stream',
    onEvent        = () => {},
    onStatusChange = () => {},
    onError        = () => {},
    onTopicsChange = () => {},
  } = {}) {
    this.#urls          = { subscribeUrl, unsubscribeUrl, issueTicketUrl, streamUrl };
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
   * Subscribe to a topic.  Throws on HTTP error.
   * @param {string} topic
   */
  async subscribe(topic) {
    const res = await fetch(this.#urls.subscribeUrl, {
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
    const res = await fetch(this.#urls.unsubscribeUrl, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ topic }),
    });
    if (!res.ok) throw new Error(await res.text());
    this.#topics.delete(topic);
    this.#onTopicsChange(this.topics);
  }

  /**
   * Issue a stream ticket then open the EventSource connection.
   * Fires onStatusChange('connecting') immediately, then 'connected' on open
   * or 'error' if the connection fails.
   * Throws if no topics are subscribed or the ticket request fails.
   */
  async start() {
    if (this.#topics.size === 0) {
      throw new Error('Add at least one topic before starting the stream.');
    }

    // Obtain a short-lived ticket; the server sets it as a cookie.
    const ticketRes = await fetch(this.#urls.issueTicketUrl, {
      method:      'POST',
      credentials: 'include',
    });
    if (!ticketRes.ok) {
      const body = await ticketRes.json().catch(() => ({ detail: ticketRes.statusText }));
      throw new Error(body.detail || ticketRes.statusText);
    }

    this.#onStatusChange('connecting');

    // withCredentials ensures the ticket cookie is sent cross-origin.
    this.#evtSource = new EventSource(this.#urls.streamUrl, { withCredentials: true });

    const handleEvent = e => {
      let data;
      try   { data = JSON.parse(e.data); }
      catch { data = { topic: '?', time: '?' }; }
      this.#onEvent(e.type, data, e.lastEventId);
    };

    this.#evtSource.addEventListener('update',  handleEvent);
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
}
