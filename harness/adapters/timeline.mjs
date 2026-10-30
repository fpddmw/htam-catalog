/** Exact authorized-thread persisted input observation. No mutation or human-origin inference. */
import { createHash } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { AdapterError, stamp } from './transport.mjs';
import { fingerprint } from './events.mjs';

const utf8Hash = value => createHash('sha256').update(value, 'utf8').digest('hex');
const validId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value);
const wireId = (value, field) => {
  if (!validId(value)) throw new AdapterError('IDENTITY_MISMATCH', 'Invalid native ' + field);
  return value;
};
const integer = (value, low, high, name) => {
  if (!Number.isInteger(value) || value < low || value > high) throw new AdapterError('INVALID', name + ' is outside its finite bound');
  return value;
};
function prefix(text, limit) {
  const bytes = Buffer.from(text, 'utf8'); let end = Math.min(bytes.length, limit);
  while (end < bytes.length && end > 0 && (bytes[end] & 0xc0) === 0x80) end--;
  return { text: bytes.subarray(0, end).toString('utf8'), originalBytes: bytes.length, storedBytes: end, truncated: end < bytes.length };
}
function sourceFields(value, keys) {
  const fields = {};
  for (const key of keys) if (Object.hasOwn(value, key)) {
    const json = JSON.stringify(value[key]);
    if (json === undefined) continue;
    const bounded = prefix(json, 1024);
    fields[key] = { jsonSha256: utf8Hash(json), originalBytes: bounded.originalBytes, truncated: bounded.truncated,
      ...(bounded.truncated ? { jsonPrefix: bounded.text } : { value: value[key] }) };
  }
  return fields;
}
function messageKey(hostId, threadId, turnId, itemId) {
  return 'native-message:' + fingerprint({ hostId, threadId, turnId, itemId });
}

function project(adapter, item, context, budget) {
  const { threadId, turnId, threadSourceFields } = context;
  wireId(item.id, 'itemId');
  if (!Array.isArray(item.content)) throw new AdapterError('PROTOCOL_ERROR', 'Native user message content is not an array');
  if (item.clientId != null && typeof item.clientId !== 'string') throw new AdapterError('PROTOCOL_ERROR', 'Native client ID is not a string');
  const contentSha256 = fingerprint(item.content), key = messageKey(adapter.hostId, threadId, turnId, item.id);
  const blocks = [], contentTypes = new Set(); let remaining = Math.min(budget.perMessage, budget.total), textTruncated = false;
  const contentBlocksObserved = Math.min(item.content.length, 32);
  for (let index = 0; index < contentBlocksObserved; index++) {
    const part = item.content[index];
    if (!part || typeof part.type !== 'string') throw new AdapterError('PROTOCOL_ERROR', 'Native input block has no type');
    contentTypes.add(part.type.slice(0, 64));
    if (part.type !== 'text') continue; // Hash only; never fetch paths/URLs/attachments.
    if (typeof part.text !== 'string') throw new AdapterError('PROTOCOL_ERROR', 'Native text block has no text');
    const bounded = prefix(part.text, remaining);
    blocks.push({ index, type: 'text', text: bounded.text, textSha256: utf8Hash(part.text),
      originalBytes: bounded.originalBytes, storedBytes: bounded.storedBytes, truncated: bounded.truncated });
    remaining -= bounded.storedBytes; budget.total -= bounded.storedBytes; textTruncated ||= bounded.truncated;
  }
  const contentTruncated = contentBlocksObserved < item.content.length || item.content.some(part => typeof part?.type === 'string' && part.type.length > 64);
  const identityTruncated = typeof item.clientId === 'string' && Buffer.byteLength(item.clientId, 'utf8') > 1024;
  const itemSources = sourceFields(item, ['source', 'origin', 'provenance', 'role', 'delivery']);
  const sourceTruncated = [...Object.values(itemSources), ...Object.values(threadSourceFields)].some(value => value.truncated);
  const body = { threadId, turnId, itemId: item.id, itemType: 'userMessage', clientUserMessageId: identityTruncated ? null : item.clientId ?? null,
    identityTruncated, ...(identityTruncated ? { nativeClientIdSha256: utf8Hash(item.clientId) } : {}),
    nativeMessageKey: key, nativeContentKey: key + ':' + contentSha256,
    contentSha256, contentHashAlgorithm: 'json-native-v1', textHashAlgorithm: 'sha256-utf8',
    contentBlockCount: item.content.length, contentBlocksObserved, contentTypes: [...contentTypes],
    textBlocks: blocks, textTruncated, contentTruncated, sourceTruncated,
    nonTextContent: contentTypes.size > (contentTypes.has('text') ? 1 : 0),
    contentCoverage: 'available_persisted_native_full_item',
    nativeSourceFields: { item: itemSources, thread: threadSourceFields,
      itemOriginAvailability: Object.keys(itemSources).length ? 'present_unverified' : 'not_present_in_public_user_message' },
    originClassification: 'unverified', source: 'public_app_server_history',
    sourceMeaning: 'Native user role, thread origin and source fields do not establish human_direct' };
  const fact = adapter.fact('native_user_message_observed', body);
  const { observedAt: _time, runtimeId: _observer, eventId: _id, ...identity } = fact;
  fact.eventId = 'native:' + fingerprint(identity);
  return fact;
}

export async function observeTimeline(adapter, { threadId, anchor = null, maxPages = 3, pageSize = 10,
  maxItemPages = 3, itemPageSize = 20, maxTurns = 30, maxItems = 200, maxMessages = 64,
  maxTextBytes = 16384, maxTotalTextBytes = 65536, maxRequests = 20, deadlineMs = 30000 } = {}) {
  adapter.scope(threadId);
  for (const [name, value, low, high] of [
    ['maxPages', maxPages, 1, 10], ['pageSize', pageSize, 1, 50], ['maxItemPages', maxItemPages, 1, 10],
    ['itemPageSize', itemPageSize, 1, 100], ['maxTurns', maxTurns, 1, 100], ['maxItems', maxItems, 1, 1000],
    ['maxMessages', maxMessages, 1, 128], ['maxTextBytes', maxTextBytes, 0, 65536],
    ['maxTotalTextBytes', maxTotalTextBytes, 0, 262144], ['maxRequests', maxRequests, 1, 100], ['deadlineMs', deadlineMs, 1, 60000]]) integer(value, low, high, name);
  if (anchor !== null && (!anchor || typeof anchor !== 'object' || Array.isArray(anchor)
      || Object.keys(anchor).some(key => !['turnId', 'itemId', 'contentSha256'].includes(key))
      || !validId(anchor.turnId) || (anchor.itemId !== undefined && !validId(anchor.itemId))
      || (anchor.contentSha256 !== undefined && (!anchor.itemId || !/^[a-f0-9]{64}$/.test(anchor.contentSha256)))))
    throw new AdapterError('INVALID', 'Timeline anchor needs exact turn/item identities and optional content hash');
  if (!adapter.connected || adapter.closed) throw new AdapterError('CLOSED', 'Read-only timeline needs a connected observer');
  const limits = { maxPages, pageSize, maxItemPages, itemPageSize, maxTurns, maxItems, maxMessages,
    maxTextBytes, maxTotalTextBytes, maxRequests, deadlineMs };
  const begun = performance.now(), startedAt = stamp(), messages = [], gaps = [], turns = [], messageEvents = new Set(), contentKeys = new Set();
  const nativeHashes = new Map(), seenTurns = new Set(), budget = { perMessage: maxTextBytes, total: maxTotalTextBytes };
  let requests = 0, turnPages = 0, itemPages = 0, itemsObserved = 0, cursor, exhausted = false, readError = null, lastReadRequest = null,
    boundaryTurnObserved = false, anchorFound = anchor === null ? null : false, itemsComplete = true,
    textTruncated = false, contentTruncated = false, sourceTruncated = false, identityTruncated = false, bounded = false, latestObservedTurnId = null,
    threadSourceFields = {}, fatal = null, stoppedMidPage = false;
  const gap = (code, facts = {}) => { if (gaps.length < 32 && !gaps.some(item => item.code === code && item.turnId === facts.turnId)) gaps.push({ code, ...facts }); };
  const request = async (method, params) => {
    lastReadRequest = { method, target: { threadId, ...(params.turnId ? { turnId: params.turnId } : {}) } };
    const remaining = Math.floor(deadlineMs - (performance.now() - begun));
    if (requests >= maxRequests || remaining < 1) { bounded = true; throw new AdapterError('READ_BOUND', requests >= maxRequests ? 'request_limit' : 'deadline'); }
    requests++;
    try {
      const response = await adapter.transport.request(method, params, { timeoutMs: Math.min(remaining, adapter.transport.requestTimeoutMs ?? 15000) });
      if (Number.isInteger(response.requestId)) lastReadRequest.requestId = response.requestId;
      return response.result;
    }
    catch (error) {
      readError = { method, target: { threadId, ...(params.turnId ? { turnId: params.turnId } : {}) },
        code: error instanceof AdapterError ? error.code : 'OBSERVATION_FAILED',
        ...(Number.isInteger(error.facts?.requestId) ? { requestId: error.facts.requestId } : {}),
        ...(Number.isInteger(error.facts?.rpcCode) ? { rpcCode: error.facts.rpcCode } : {}),
        message: String(error.message ?? 'Native read failed').slice(0, 1024) };
      throw error;
    }
  };
  const page = (result, limit) => {
    if (!Array.isArray(result?.data) || result.data.length > limit || (result.threadId && result.threadId !== threadId))
      throw new AdapterError(result?.threadId && result.threadId !== threadId ? 'IDENTITY_MISMATCH' : 'PROTOCOL_ERROR', 'Invalid exact-thread timeline page');
    if (result.nextCursor != null && (typeof result.nextCursor !== 'string' || !result.nextCursor || result.nextCursor.length > 4096))
      throw new AdapterError('PROTOCOL_ERROR', 'Invalid bounded native pagination cursor');
    return result;
  };
  const observeItems = async (items, turnId) => {
    for (const item of items) {
      if (itemsObserved >= maxItems) { itemsComplete = false; bounded = true; gap('item_limit', { turnId }); return false; }
      itemsObserved++;
      if (!item || (item.threadId && item.threadId !== threadId) || (item.turnId && item.turnId !== turnId))
        throw new AdapterError('IDENTITY_MISMATCH', 'Native item target differs from exact requested thread/turn');
      if (item.type !== 'userMessage') continue;
      wireId(item.id, 'user itemId');
      if (!Array.isArray(item.content)) throw new AdapterError('PROTOCOL_ERROR', 'Native user message has no content array');
      const key = messageKey(adapter.hostId, threadId, turnId, item.id), hash = fingerprint(item.content);
      if (anchor?.turnId === turnId && anchor.itemId === item.id) {
        anchorFound = true;
        if (anchor.contentSha256 && anchor.contentSha256 !== hash) gap('anchor_content_changed', { turnId, itemId: item.id });
      }
      if (nativeHashes.has(key) && nativeHashes.get(key) !== hash) gap('item_content_conflict', { turnId, itemId: item.id });
      nativeHashes.set(key, hash);
      const contentKey = key + ':' + hash;
      if (contentKeys.has(contentKey)) continue;
      if (messages.length >= maxMessages) { itemsComplete = false; bounded = true; gap('message_limit', { turnId }); return false; }
      contentKeys.add(contentKey);
      const fact = project(adapter, item, { threadId, turnId, threadSourceFields }, budget);
      textTruncated ||= fact.textTruncated; contentTruncated ||= fact.contentTruncated; sourceTruncated ||= fact.sourceTruncated;
      identityTruncated ||= fact.identityTruncated;
      if (!messageEvents.has(fact.eventId)) {
        messageEvents.add(fact.eventId); messages.push(fact); await adapter.record(fact, !!adapter.factSink);
      }
    }
    return true;
  };
  try {
    const metadata = await request('thread/read', { threadId, includeTurns: false });
    if (metadata?.thread?.id !== threadId) throw new AdapterError('IDENTITY_MISMATCH', 'Native metadata returned another thread');
    threadSourceFields = sourceFields(metadata.thread, ['source', 'originator', 'threadSource']);
    const turnCursors = new Set();
    do {
      const result = page(await request('thread/turns/list', { threadId, sortDirection: 'desc', itemsView: 'full', limit: pageSize,
        ...(cursor ? { cursor } : {}) }), pageSize);
      turnPages++;
      for (const turn of result.data) {
        wireId(turn?.id, 'turnId');
        if (turn.threadId && turn.threadId !== threadId) throw new AdapterError('IDENTITY_MISMATCH', 'Native turn returned another thread');
      }
      if (turnPages === 1) latestObservedTurnId = result.data[0]?.id ?? null;
      for (const turn of result.data) {
        if (!seenTurns.has(turn.id) && turns.length >= maxTurns) { bounded = true; stoppedMidPage = true; gap('turn_limit'); break; }
        if (!seenTurns.has(turn.id)) {
          seenTurns.add(turn.id); turns.push({ turnId: turn.id, status: turn.status ?? null, itemsView: turn.itemsView ?? 'unknown',
            rootTurnId: turn.rootTurnId ?? null, startedAt: turn.startedAt ?? null, completedAt: turn.completedAt ?? null });
        }
        if (anchor?.turnId === turn.id) { boundaryTurnObserved = true; if (!anchor.itemId) anchorFound = true; }
        if (turn.itemsView === 'full' && Array.isArray(turn.items)) {
          if (!(await observeItems(turn.items, turn.id))) { stoppedMidPage = true; break; }
        } else {
          let itemCursor, itemExhausted = false, pagesForTurn = 0; const itemCursors = new Set();
          const summaryIds = new Set((Array.isArray(turn.items) ? turn.items : []).filter(item => item?.type === 'userMessage').map(item => item.id));
          do {
            const listed = page(await request('thread/items/list', { threadId, turnId: turn.id, sortDirection: 'asc', limit: itemPageSize,
              ...(itemCursor ? { cursor: itemCursor } : {}) }), itemPageSize);
            itemPages++; pagesForTurn++;
            for (const entry of listed.data) if (entry?.turnId !== turn.id || !entry.item)
              throw new AdapterError('IDENTITY_MISMATCH', 'Native item entry returned another turn');
            for (const entry of listed.data) summaryIds.delete(entry.item.id);
            if (!(await observeItems(listed.data.map(entry => entry.item), turn.id))) { stoppedMidPage = true; break; }
            itemCursor = listed.nextCursor; itemExhausted = !itemCursor;
            if (itemCursor && itemCursors.has(itemCursor)) throw new AdapterError('PROTOCOL_ERROR', 'Native item pagination repeated a cursor');
            if (itemCursor) itemCursors.add(itemCursor);
          } while (!itemExhausted && pagesForTurn < maxItemPages && !stoppedMidPage);
          if (!itemExhausted) { itemsComplete = false; bounded = true; gap('item_pages_incomplete', { turnId: turn.id }); }
          if (itemExhausted && summaryIds.size) { itemsComplete = false; gap('summary_user_items_not_recovered', { turnId: turn.id }); }
        }
        if (boundaryTurnObserved || stoppedMidPage) break;
      }
      cursor = result.nextCursor; exhausted = !cursor;
      if (cursor && turnCursors.has(cursor)) throw new AdapterError('PROTOCOL_ERROR', 'Native turn pagination repeated a cursor');
      if (cursor) turnCursors.add(cursor);
    } while (!exhausted && turnPages < maxPages && !boundaryTurnObserved && !stoppedMidPage);
    if (!boundaryTurnObserved && !exhausted) { bounded = true; gap('turn_pages_incomplete'); }
    if (anchor && !anchorFound) gap(boundaryTurnObserved ? 'anchor_item_not_found' : 'anchor_turn_not_found');
  } catch (error) {
    itemsComplete = false;
    const code = error instanceof AdapterError ? error.code : 'OBSERVATION_FAILED';
    readError ??= { ...(lastReadRequest ?? { target: { threadId } }), code,
      message: String(error.message ?? 'Native read response validation failed').slice(0, 1024) };
    gap(code === 'READ_BOUND' ? 'read_budget_exhausted' : code === 'IDENTITY_MISMATCH' ? 'identity_mismatch' : 'native_read_incomplete');
    fatal = new AdapterError(code, 'Exact-thread timeline observation was incomplete', { threadId, noAutomaticRetry: true, ...(readError ? { readError } : {}) });
  }
  if (textTruncated) gap('text_truncated');
  if (contentTruncated) gap('content_blocks_truncated');
  if (sourceTruncated) gap('source_fields_truncated');
  if (identityTruncated) gap('identity_fields_truncated');
  const rangeComplete = anchor ? !!anchorFound : exhausted;
  const coverage = { scope: 'exact_authorized_thread_persisted_history_only', limits, requests, turnPages, itemPages,
    turnsObserved: turns.length, itemsObserved, messagesObserved: messages.length, anchorFound, boundaryTurnObserved,
    itemsComplete, textTruncated, contentTruncated, sourceTruncated, identityTruncated, bounded, nextCursor: cursor ?? null,
    nextCursorMaySkipUnprocessedPage: stoppedMidPage, olderHistoryOmittedByAnchor: boundaryTurnObserved,
    exhausted, rangeComplete, gap: gaps.length > 0, gaps,
    complete: rangeComplete && itemsComplete && gaps.length === 0,
    consistency: 'non_atomic_public_reads; active changes after observation are outside this receipt',
    noAutomaticRetry: true, humanOriginVerified: false, attachmentsFetched: false, ...(readError ? { readError } : {}) };
  const scan = adapter.fact('thread_timeline_observed', { threadId, requestedAnchor: anchor, latestObservedTurnId,
    startedAt, coverage, turns, messageEventIds: messages.map(message => message.eventId), source: 'public_app_server_history' });
  await adapter.record(scan, !!adapter.factSink);
  if (fatal && ['IDENTITY_MISMATCH', 'PERSISTENCE_FAILED'].includes(fatal.code)) {
    fatal.facts.scanEventId = scan.eventId; throw fatal;
  }
  return { threadId, state: coverage.complete ? 'OBSERVED' : 'INCOMPLETE', latestObservedTurnId,
    messages, scan, coverage, ...(fatal ? { error: { code: fatal.code, noAutomaticRetry: true } } : {}),
    entry: adapter.entry(threadId), observedAt: scan.observedAt, factsPersisted: !!adapter.factSink && !adapter.persistenceError,
    observation: 'Read-only persisted input facts; no input adopted, human origin inferred, runtime ownership acquired or permission changed' };
}
