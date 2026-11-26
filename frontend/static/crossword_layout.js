export function placementCellCenters(placement, source, target, cellSize = 26) {
  const sourceX = Number(source?.left);
  const sourceY = Number(source?.top);
  const targetX = Number(target?.left);
  const targetY = Number(target?.top);
  const offset = Number(placement?.offset_px);
  const yOffset = Number(placement?.y_offset_px ?? 0);
  const count = Number(placement?.letter_count);
  const dx = targetX - sourceX;
  const dy = targetY - sourceY;
  const length = Math.hypot(dx, dy);
  if (![sourceX, sourceY, targetX, targetY, offset, yOffset, count, cellSize, length].every(Number.isFinite)
    || length <= 0 || count < 1 || cellSize <= 0) return [];

  const ux = dx / length;
  const uy = dy / length;
  const nx = -uy;
  const ny = ux;
  const firstX = sourceX + ux * offset + nx * yOffset;
  const firstY = sourceY + uy * offset + ny * yOffset;
  const stepX = placement.orientation === "perpendicular" ? nx : ux;
  const stepY = placement.orientation === "perpendicular" ? ny : uy;
  return Array.from({ length: count }, (_, index) => ({
    x: firstX + stepX * index * cellSize,
    y: firstY + stepY * index * cellSize,
  }));
}

export function revealedCrossingLetters(placements) {
  const letters = new Map();
  for (const placement of placements || []) {
    if (!placement?.crossing_id || !placement.is_guessed || !placement.word_text) continue;
    const index = Number(placement.crossing_index);
    const letter = Array.from(placement.word_text)[index];
    if (Number.isInteger(index) && letter && !letters.has(placement.crossing_id)) {
      letters.set(placement.crossing_id, letter);
    }
  }
  return letters;
}
