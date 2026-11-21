import test from "node:test";
import assert from "node:assert/strict";
import {
  initialSelectedCityIds,
  mergeSessionSnapshot,
  wordCenterOffset,
} from "../static/session_state.js";

const snapshot = (id, version, playerState = "playing", status = "active") => ({
  session: { id, state_version: version, status },
  player: { state: playerState, session_score: 12 },
  crossword: [],
});

test("rejects a snapshot for a different session", () => {
  const current = snapshot("session-a", 4);
  assert.equal(mergeSessionSnapshot(current, snapshot("session-b", 5)), null);
});

test("rejects an older state version from the same session", () => {
  const current = snapshot("session-a", 8);
  assert.equal(mergeSessionSnapshot(current, snapshot("session-a", 7)), null);
});

test("keeps a confirmed win even if a later response tries to roll it back", () => {
  const current = snapshot("session-a", 12, "won", "finished");
  assert.equal(mergeSessionSnapshot(current, snapshot("session-a", 13, "playing")), null);
});

test("allows the explicit new-game response to replace the previous session", () => {
  const current = snapshot("session-a", 12, "won", "finished");
  const next = snapshot("session-b", 1, "selecting_route");
  assert.equal(mergeSessionSnapshot(current, next, { allowSessionReplacement: true }).session.id, "session-b");
});

test("positions the word center from the server pixel offset and 26px cells", () => {
  assert.equal(wordCenterOffset(52, 6), 130);
});

test("starts manual route selection from the assigned source city", () => {
  assert.deepEqual(initialSelectedCityIds("selecting_route", 42), [42]);
  assert.deepEqual(initialSelectedCityIds("playing", 42), []);
});
