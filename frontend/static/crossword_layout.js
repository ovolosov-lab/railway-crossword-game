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

export function placementDisplayTransform(placement, centers, source, target) {
  const dx = Number(target?.left) - Number(source?.left);
  const dy = Number(target?.top) - Number(source?.top);
  // Keep crossing indices fixed; only uncrossed along words can be reversed safely.
  const orientAlongFromLeft = placement?.orientation === "along"
    && !placement.crossing_id
    && dx < 0;
  const angle = Math.atan2(dy, dx) * 180 / Math.PI
    + (placement?.orientation === "perpendicular" ? 90 : 0)
    + (orientAlongFromLeft ? 180 : 0);

  return {
    origin: orientAlongFromLeft ? centers.at(-1) : centers[0],
    angle,
  };
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
