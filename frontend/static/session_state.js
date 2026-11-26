export function mergeSessionSnapshot(current, incoming, { allowSessionReplacement = false } = {}) {
  if (!incoming || !incoming.session) {
    return current.session ? null : { ...current, ...(incoming || {}) };
  }

  const currentId = current.session?.id;
  const incomingId = incoming.session.id;
  const sameSession = currentId && String(currentId) === String(incomingId);
  if (currentId && !sameSession && !allowSessionReplacement) return null;

  if (sameSession) {
    const currentVersion = Number(current.session.state_version);
    const incomingVersion = Number(incoming.session.state_version);
    if (Number.isFinite(currentVersion) && Number.isFinite(incomingVersion)
      && incomingVersion < currentVersion) return null;
    if (current.player?.state === "won" && incoming.player?.state !== "won") return null;
    const rank = { active: 0, finishing: 1, finished: 2 };
    if ((rank[incoming.session.status] ?? -1) < (rank[current.session.status] ?? -1)) return null;
  }

  return {
    session: incoming.session,
    player: sameSession && incoming.player
      ? { ...current.player, ...incoming.player }
      : incoming.player || current.player,
    crossword: Array.isArray(incoming.crossword)
      ? incoming.crossword
      : sameSession ? (current.crossword || []) : [],
  };
}

export function wordCenterOffset(offsetPx, letterCount) {
  return Number(offsetPx) + (Number(letterCount) - 1) * 13;
}

export function initialSelectedCityIds(playerState, sourceCityId) {
  const sourceId = Number(sourceCityId);
  return playerState === "selecting_route" && Number.isSafeInteger(sourceId) && sourceId > 0
    ? [sourceId]
    : [];
}
