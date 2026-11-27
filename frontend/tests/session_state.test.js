import test from "node:test";
import assert from "node:assert/strict";
import {
  initialSelectedCityIds,
  mergeSessionSnapshot,
  wordCenterOffset,
} from "../static/session_state.js";
import {
  placementCellCenters,
  placementDisplayTransform,
  revealedCrossingLetters,
} from "../static/crossword_layout.js";

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
  assert.equal(wordCenterOffset(52, 6), 117);
});

test("places perpendicular letters along the local y axis through their crossing", () => {
  const centers = placementCellCenters({
    orientation: "perpendicular",
    offset_px: 52,
    y_offset_px: -26,
    letter_count: 3,
  }, { left: 0, top: 0 }, { left: 100, top: 0 });
  assert.deepEqual(centers, [
    { x: 52, y: -26 },
    { x: 52, y: 0 },
    { x: 52, y: 26 },
  ]);
});

test("displays non-crossing along words left to right when the route points left", () => {
  const placement = {
    orientation: "along",
    offset_px: 0,
    letter_count: 3,
  };
  const source = { left: 100, top: 20 };
  const target = { left: 0, top: 20 };
  const centers = placementCellCenters(placement, source, target);
  const { origin, angle } = placementDisplayTransform(placement, centers, source, target);
  const displayedCenters = Array.from({ length: 3 }, (_, index) => ({
    x: origin.x + Math.cos(angle * Math.PI / 180) * index * 26,
    y: origin.y + Math.sin(angle * Math.PI / 180) * index * 26,
  }));

  const expectedCenters = [...centers].reverse();
  displayedCenters.forEach((center, index) => {
    assert.equal(center.x, expectedCenters[index].x);
    assert.ok(Math.abs(center.y - expectedCenters[index].y) < 1e-9);
  });
  assert.equal(displayedCenters[0].x < displayedCenters[1].x, true);
  assert.equal(displayedCenters[1].x < displayedCenters[2].x, true);
});

test("keeps the existing orientation and crossing position for crossing along words", () => {
  const placement = {
    orientation: "along",
    offset_px: 0,
    letter_count: 3,
    crossing_id: "cross-1",
  };
  const source = { left: 100, top: 20 };
  const target = { left: 0, top: 20 };
  const centers = placementCellCenters(placement, source, target);
  const { origin, angle } = placementDisplayTransform(placement, centers, source, target);

  assert.deepEqual(origin, centers[0]);
  assert.equal(angle, 180);
});

test("reveals only the shared letter when either crossing word is solved", () => {
  const letters = revealedCrossingLetters([
    { crossing_id: "cross-1", crossing_index: 2, is_guessed: false },
    { crossing_id: "cross-1", crossing_index: 1, is_guessed: true, word_text: "train" },
    { crossing_id: "cross-2", crossing_index: 1, is_guessed: false },
  ]);
  assert.deepEqual([...letters], [["cross-1", "r"]]);
});

test("starts manual route selection from the assigned source city", () => {
  assert.deepEqual(initialSelectedCityIds("selecting_route", 42), [42]);
  assert.deepEqual(initialSelectedCityIds("playing", 42), []);
});
