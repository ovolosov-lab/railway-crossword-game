(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const app = document.getElementById("game-app");
  if (!app) return;

  const elements = {
    timer: document.getElementById("round-timer"),
    timerBlock: document.getElementById("timer-block"),
    roundStatus: document.getElementById("round-status"),
    message: document.getElementById("game-message"),
    loading: document.getElementById("game-loading"),
    playerCard: document.getElementById("player-card"),
    playerHeading: document.getElementById("player-heading"),
    playerDescription: document.getElementById("player-description"),
    playerScore: document.getElementById("player-score"),
    assignedCities: document.getElementById("assigned-cities"),
    routeProgress: document.getElementById("route-progress"),
    joinButton: document.getElementById("join-button"),
    confirmButton: document.getElementById("confirm-route-button"),
    playWithTrain: document.getElementById("play-with-train"),
    participantCount: document.getElementById("participant-count"),
    leaderboard: document.getElementById("leaderboard-list"),
    mapHelp: document.getElementById("map-help"),
    gamePanel: document.querySelector(".game-panel"),
    mapScroll: document.getElementById("map-scroll"),
    mapCanvas: document.getElementById("map-canvas"),
    mapScaleControl: document.querySelector(".map-scale-control"),
    mapOverlay: document.getElementById("map-overlay"),
    victoryTrainOverlay: document.getElementById("victory-train-overlay"),
    victoryOverlay: document.getElementById("victory-overlay"),
    victoryTrophy: document.querySelector(".victory-trophy"),
    victoryLocomotive: document.querySelector(".victory-locomotive"),
    victoryAudio: document.getElementById("victory-audio"),
    trainAudio: document.getElementById("train-audio"),
    wordDialog: document.getElementById("word-dialog"),
    wordDialogTitle: document.getElementById("word-dialog-title"),
    wordDefinition: document.getElementById("word-definition"),
    wordLength: document.getElementById("word-length"),
    guessForm: document.getElementById("guess-form"),
    answer: document.getElementById("word-answer"),
    submitGuess: document.getElementById("submit-guess"),
    wordFeedback: document.getElementById("word-feedback"),
    hintFeedback: document.getElementById("hint-feedback"),
    firstHint: document.getElementById("first-letter-hint"),
    lastHint: document.getElementById("last-letter-hint"),
    closeWordDialog: document.getElementById("close-word-dialog"),
  };

  const state = {
    round: null,
    player: null,
    cities: [],
    routes: [],
    selectedRouteIds: [],
    selectedCityIds: [],
    crossword: [],
    crosswordLoaded: false,
    victoryCrossword: null,
    victoryAnimationActive: false,
    activePlacementId: null,
    hintValues: new Map(),
    unavailableHints: new Set(),
    busy: false,
    retryRouteIds: null,
    syncInFlight: false,
    syncTimer: null,
    terminalRoundId: null,
    guessBlockedUntil: null,
    shouldCelebrateVictory: false,
    victoryFrame: null,
    victoryTimer: null,
    victoryConfettiTimer: null,
  };

  const numberFormat = new Intl.NumberFormat("ru-RU");
  let clickX = '35%';
  let clickY = '10%';

  function showMessage(message, kind = "error") {
    elements.message.innerHTML = message;
    elements.message.dataset.kind = kind;
    elements.message.hidden = false;
  }

  function clearMessage() {
    elements.message.textContent = "";
    elements.message.hidden = true;
    delete elements.message.dataset.kind;
    delete elements.message.dataset.source;
  }

  function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-cookie-name"]');
    const cookieName = meta?.content || "csrf_token";
    const prefix = `${cookieName}=`;
    const cookie = document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(prefix));
    return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : "";
  }

  async function apiRequest(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body) headers.set("Content-Type", "application/json");
    if (options.method && options.method !== "GET") {
      headers.set("X-CSRF-Token", getCsrfToken());
    }

    let response;
    try {
      response = await fetch(url, {
        ...options,
        headers,
        credentials: "same-origin",
      });
    } catch {
      throw new Error("Не удалось связаться с сервером. Проверьте подключение и повторите попытку.");
    }

    let body;
    try {
      body = await response.json();
    } catch {
      throw new Error("Сервер вернул некорректный ответ. Попробуйте обновить страницу.");
    }
    if (!response.ok) {
      const error = new Error(body?.error?.message || "Не удалось выполнить запрос.");
      error.code = body?.error?.code;
      error.status = response.status;
      error.retryAfter = response.headers.get("Retry-After");
      error.round = body?.round;
      error.player = body?.player;
      throw error;
    }
    return body;
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds)) return "—";
    const safeSeconds = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(safeSeconds / 3600);
    const minutes = Math.floor((safeSeconds % 3600) / 60);
    const remaining = safeSeconds % 60;
    if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}`;
    return `${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}`;
  }

  function updateTimer() {
    if (!state.round) return;
    elements.timer.textContent = formatTime(state.round.remaining_seconds);
    if (state.round.status === "active" && state.round.remaining_seconds > 0) {
      state.round.remaining_seconds -= 1;
    }
    updateJoinAvailability();
    if (elements.wordDialog.open) renderActivePlacement();
  }

  function updateRound(round) {
    if (state.round?.id && round.id && state.round.id !== round.id) {
      state.crossword = [];
      state.crosswordLoaded = false;
      state.hintValues.clear();
      state.unavailableHints.clear();
      state.terminalRoundId = null;
      state.guessBlockedUntil = null;
      state.activePlacementId = null;
      hidePlayWithTrainButton();
      if (elements.wordDialog.open) elements.wordDialog.close();
    }
    state.round = { ...round };
    const statusLabels = {
      active: "Раунд идёт",
      finished: "Раунд завершён",
      being_created: "Новый раунд скоро",
    };
    elements.roundStatus.textContent = statusLabels[round.status] || "Статус неизвестен";
    elements.roundStatus.dataset.state = round.status;
    updateTimer();
  }

  function hasWinningMap() {
    const player = state.player;
    const routeIds = player?.route_ids || [];
    if (
      player?.state !== "won"
      || !cityById(player.source_city_id)
      || !cityById(player.target_city_id)
      || routeIds.length === 0
      || !state.crossword.length
    ) return false;

    const cityPath = buildCityPath(player.source_city_id, routeIds);
    return (
      cityPath.length === routeIds.length + 1
      && cityPath.at(-1) === player.target_city_id
    );
  }

  function hidePlayWithTrainButton() {
    elements.playWithTrain.classList.add("hidden");
    elements.playWithTrain.disabled = true;
  }

  function updatePlayerScore() {
    const score = state.player?.round_score;
    elements.playerScore.hidden = score === null || score === undefined;
    if (score !== null && score !== undefined) {
      elements.playerScore.textContent = `Очки в раунде: ${numberFormat.format(score)}`;
    }
  }

  function isCurrentRoundResponse(round, version = round?.state_version) {
    if (!round?.id || String(round.id) !== String(state.round?.id)) return false;
    const currentVersion = state.round?.state_version;
    const isTerminalRecovery = round.status === "finished" && !Number.isInteger(version);
    return (
      isTerminalRecovery
      || !Number.isInteger(currentVersion)
      || !Number.isInteger(version)
      || version >= currentVersion
    );
  }

  function renderLeaderboard(rows) {
    elements.leaderboard.replaceChildren();
    if (!rows.length) {
      const item = document.createElement("li");
      item.className = "leaderboard-empty";
      item.textContent = "Пока нет зафиксированных побед.";
      elements.leaderboard.append(item);
      return;
    }

    for (const row of rows) {
      const item = document.createElement("li");
      const content = document.createElement("span");
      const username = document.createElement("span");
      const score = document.createElement("span");
      content.className = "leaderboard-row";
      score.className = "leaderboard-score";
      username.textContent = row.username;
      score.textContent = numberFormat.format(row.score);
      content.append(username, score);
      item.append(content);
      elements.leaderboard.append(item);
    }
  }

  function cityById(cityId) {
    return state.cities.find((city) => city.id === cityId);
  }

  function routeById(routeId) {
    return state.routes.find((route) => route.id === routeId);
  }

  function routeEndpoints(route) {
    return [route.source_city_id, route.target_city_id];
  }

  function isRouteSelectable(route) {
    if (state.player?.state !== "selecting_route" || !state.player.source_city_id) return false;
    if (state.retryRouteIds) return false;
    if (state.selectedRouteIds.includes(route.id)) return true;
    if (state.selectedCityIds.length === 0) return false;
    const tailCity = state.selectedCityIds[state.selectedCityIds.length - 1];
    if (tailCity === state.player.target_city_id) return false;
    const [sourceId, targetId] = routeEndpoints(route);
    const nextCity = sourceId === tailCity ? targetId : targetId === tailCity ? sourceId : null;
    return nextCity !== null && !state.selectedCityIds.includes(nextCity);
  }

  function svgElement(tagName, attributes = {}) {
    const element = document.createElementNS(SVG_NS, tagName);
    for (const [name, value] of Object.entries(attributes)) {
      element.setAttribute(name, String(value));
    }
    return element;
  }

  function drawCrossword() {
    const layer = svgElement("g", { "aria-label": "Клетки кроссворда" });
    const mapScale = elements.mapOverlay.getBoundingClientRect().width / 4000;
    if (!mapScale) return layer;

    const placementsByRoute = new Map();
    const visibleCrossword = state.victoryAnimationActive
      ? state.victoryCrossword
      : state.crossword;
    for (const placement of visibleCrossword || []) {
      const placements = placementsByRoute.get(placement.route_id) || [];
      placements.push(placement);
      placementsByRoute.set(placement.route_id, placements);
    }

    for (const [routeId, routePlacements] of placementsByRoute) {
      const route = routeById(routeId);
      const source = route && cityById(route.source_city_id);
      const target = route && cityById(route.target_city_id);
      if (!route || !source || !target || !route.length_in_letters) continue;

      const sourceX = Number(source.left);
      const sourceY = Number(source.top);
      const targetX = Number(target.left);
      const targetY = Number(target.top);
      const dx = targetX - sourceX;
      const dy = targetY - sourceY;
      const segmentLength = Math.hypot(dx, dy);
      const sortedPlacements = [...routePlacements].sort(
        (left, right) => left.position_index - right.position_index,
      );
      if (sortedPlacements.some(
        (placement) => placement.position_index + placement.letter_count > route.length_in_letters,
      )) {
        console.error("Crossword placement exceeds route capacity.", routeId);
        continue;
      }
      const totalLetterCount = sortedPlacements.reduce(
        (total, placement) => total + placement.letter_count,
        0,
      );
      const cityClearance = 18 + 5 + 7 + 8;
      const usableLength = segmentLength - cityClearance * 2;
      if (usableLength <= 0) {
        console.error("No room remains between city markers for crossword cells.", routeId);
        continue;
      }
      const minimumGap = 6;
      const spaceForCells = usableLength - minimumGap * (sortedPlacements.length + 1);
      if (spaceForCells <= 0) {
        console.error("No room remains between city markers for crossword cells.", routeId);
        continue;
      }
      const cellSize = Math.min(25, spaceForCells / totalLetterCount);
      const totalWordLength = totalLetterCount * cellSize;
      const freeSpace = usableLength - totalWordLength;
      const gap = Math.max(
        minimumGap,
        freeSpace / (sortedPlacements.length + 1),
      );
      let lettersBefore = 0;

      let angle = Math.atan2(dy, dx) * 180 / Math.PI;
      if (angle > 90) angle -= 180;
      if (angle < -90) angle += 180;

      for (const [index, placement] of sortedPlacements.entries()) {
        const wordStart = cityClearance + gap * (index + 1) + lettersBefore * cellSize;
        const wordCenter = wordStart + placement.letter_count * cellSize / 2;
        const fraction = wordCenter / segmentLength;
        const centerX = sourceX + dx * fraction;
        const centerY = sourceY + dy * fraction;
        const group = svgElement("g", {
          class: `crossword-word${placement.is_guessed ? " is-guessed" : ""}`,
          transform: `translate(${centerX} ${centerY}) rotate(${angle})`,
          "data-placement-id": placement.placement_id,
          role: "button",
          tabindex: "0",
          "aria-label": `${placement.definition}. ${placement.letter_count} клеток${placement.is_guessed ? ". Слово разгадано" : ""}`,
          "aria-pressed": placement.is_guessed ? "true" : "false",
        });
        const totalWidth = placement.letter_count * cellSize;
        const revealedCharacters = placement.is_guessed && placement.revealed_word
          ? Array.from(placement.revealed_word)
          : [];
        for (let cellIndex = 0; cellIndex < placement.letter_count; cellIndex += 1) {
          const x = -totalWidth / 2 + cellIndex * cellSize;
          group.append(svgElement("rect", {
            x,
            y: -cellSize / 2,
            width: cellSize,
            height: cellSize,
            rx: 4,
            class: "crossword-cell",
          }));
          const character = revealedCharacters[cellIndex];
          if (character) {
            const label = svgElement("text", {
              x: x + cellSize / 2,
              y: -1.5,
              class: "crossword-cell-text",
            });
            label.style.fontSize = `${Math.min(20, cellSize * 0.8)}px`;
            label.textContent = character;
            group.append(label);
          }
        }
        layer.append(group);
        lettersBefore += placement.letter_count;
      }
    }

    return layer;
  }

  function handleRouteClick(event) {
    const clickedPlacement = event.target.closest("[data-placement-id]");

    if (clickedPlacement) {
      if (state.player?.state === "playing") {
        clickX = event.pageX;
        clickY = event.pageY;
      }
      openPlacementDialog(clickedPlacement.getAttribute("data-placement-id"));
      return;
    }
    const clickedRoute = event.target.closest("[data-route-id]");
    if (!clickedRoute) return;
    selectRoute(Number(clickedRoute.getAttribute("data-route-id")));
  }

  function handleMapKeydown(event) {
    if (
      (event.key === "Enter" || event.key === " ")
      && event.target.matches("[data-placement-id]")
    ) {
      event.preventDefault();
      openPlacementDialog(event.target.getAttribute("data-placement-id"));
    }
  }

  function drawMap() {
    elements.mapOverlay.replaceChildren();
    const routeLayer = svgElement("g", { "aria-label": "Участки железных дорог" });
    const cityLayer = svgElement("g", { "aria-label": "Города" });
    const mapScale = elements.mapOverlay.getBoundingClientRect().width / 4000;

    for (const route of state.routes) {
      const source = cityById(route.source_city_id);
      const target = cityById(route.target_city_id);
      if (!source || !target) continue;

      const line = svgElement("line", {
        x1: source.left,
        y1: source.top,
        x2: target.left,
        y2: target.top,
        class: `map-route${state.selectedRouteIds.includes(route.id) ? " is-selected" : ""}${isRouteSelectable(route) ? " is-selectable" : ""}`,
        "data-route-id": route.id,
        tabindex: isRouteSelectable(route) ? "0" : "-1",
        role: isRouteSelectable(route) ? "button" : "img",
        "aria-label": `Участок ${source.name} — ${target.name}`,
      });
      if (isRouteSelectable(route)) {
        line.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectRoute(route.id);
          }
        });
      }
      const routePlacements = state.crossword.filter(
        (placement) => placement.route_id === route.id,
      );
      if (
        state.crosswordLoaded
        && state.player?.route_ids?.includes(route.id)
        && routePlacements.every((placement) => placement.is_guessed)
      ) {
        line.classList.add("is-solved");
      }
      routeLayer.append(line);
    }

    const visited = new Set(state.selectedCityIds);
    for (const city of state.cities) {
      const left = Number(city.left);
      const top = Number(city.top);
      const group = svgElement("g", {
        "aria-label": city.name,
        "data-city-id": city.id,
      });
      const classes = ["map-city"];
      if (city.id === state.player?.source_city_id) classes.push("is-start");
      if (city.id === state.player?.target_city_id) classes.push("is-target");
      if (visited.has(city.id)) classes.push("is-visited");
      group.append(
        svgElement("circle", {
          cx: left,
          cy: top,
          r: 18,
          class: classes.join(" "),
        }),
      );
      const label = svgElement("text", {
        x: left > 3600 ? left - 24 : left + 24,
        y: top < 70 ? top + 65 : top > 2900 ? top - 30 : top - 22,
        class: "map-city-label",
        "text-anchor": left > 3600 ? "end" : "start",
        "font-size": 14,
        "stroke-width": 4,
      });
      label.textContent = city.name;
      group.append(label);
      cityLayer.append(group);
    }
    elements.mapOverlay.append(routeLayer, drawCrossword(), cityLayer);
  }

  function activePlacement() {
    return state.crossword.find(
      (placement) => placement.placement_id === state.activePlacementId,
    );
  }

  function setFeedback(element, message, kind = "error") {
    element.innerHTML = message;
    element.dataset.kind = kind;
  }

  function renderActivePlacement() {
    const placement = activePlacement();
    if (!placement) return;

    elements.wordDialogTitle.textContent = placement.is_guessed
      ? "Слово разгадано"
      : "Определение";
    elements.wordDefinition.textContent = placement.definition;
    elements.wordLength.textContent = `Клеток: ${placement.letter_count}`;
    elements.answer.disabled = (
      placement.is_guessed
      || state.player?.state !== "playing"
      || state.round?.status !== "active"
    );
    elements.submitGuess.disabled = (
      state.busy
      || placement.is_guessed
      || state.player?.state !== "playing"
      || state.round?.status !== "active"
      || (state.guessBlockedUntil && Date.now() < state.guessBlockedUntil)
    );

    const canUseHints = (
      !placement.is_guessed
      && state.player?.state === "playing"
      && state.round?.status === "active"
    );
    elements.firstHint.hidden = !canUseHints;
    elements.lastHint.hidden = !canUseHints;
    for (const [button, hintType, label] of [
      [elements.firstHint, "first_letter", "Первая буква"],
      [elements.lastHint, "last_letter", "Последняя буква"],
    ]) {
      const alreadyUsed = placement.hint_types_used.includes(hintType);
      button.textContent = alreadyUsed ? `Показать снова: ${label.toLowerCase()}` : label;
      button.disabled = state.busy || state.unavailableHints.has(
        `${placement.placement_id}:${hintType}`,
      );
    }

    if (placement.is_guessed && placement.revealed_word) {
      setFeedback(elements.wordFeedback, `Ответ: ${placement.revealed_word}`, "success");
    }

    const hints = state.hintValues.get(placement.placement_id) || {};
    const hintMessages = [];
    if (hints.first_letter) hintMessages.push(`Первая буква: <b style='color:#c709fc;'>${hints.first_letter}</b>`);
    if (hints.last_letter) hintMessages.push(`Последняя буква: <b style='color:#c709fc;'>${hints.last_letter}</b>`);
    if (hintMessages.length && elements.hintFeedback.dataset.kind !== "error") {
      setFeedback(elements.hintFeedback, hintMessages.join(" · "), "success");
    }
  }

  function openPlacementDialog(placementId) {
    const placement = state.crossword.find(
      (item) => item.placement_id === placementId,
    );
    if (!placement || placement.is_guessed) return;
    state.activePlacementId = placementId;
    elements.answer.value = "";
    setFeedback(elements.wordFeedback, "", "success");
    setFeedback(elements.hintFeedback, "", "success");
    renderActivePlacement();
    elements.wordDialog.showModal();
    if (!placement.is_guessed && state.player?.state === "playing") {
      elements.answer.focus();
    } else {
      elements.closeWordDialog.focus();
    }
  }

  function showTerminalOutcome(round, player) {
    if (round.status !== "finished" || !round.id) return;
    const roundId = String(round.id);
    if (state.terminalRoundId === roundId) return;
    state.terminalRoundId = roundId;
    if (state.syncTimer !== null) {
      window.clearInterval(state.syncTimer);
      state.syncTimer = null;
    }
    if (elements.wordDialog.open) elements.wordDialog.close();

    const result = player.state === "won"
      ? "Поздравляем! Вы победили!"
      : round.finish_reason === "timeout"
        ? "Время раунда истекло. Победителя нет."
        : "Раунд завершён победой другого игрока.";
    // console.log(player.state);
    elements.assignedCities.hidden = true; 
    elements.assignedCities.replaceChildren();       
    showMessage(result, player.state === "won" ? "victory" : "error");
    if (player.state === "won" && state.shouldCelebrateVictory) {
      state.shouldCelebrateVictory = false;
      elements.timerBlock.classList.add("hidden");
      if (hasWinningMap()) {
        elements.playWithTrain.classList.remove("hidden");
        elements.playWithTrain.disabled = false;
      } else {
        hidePlayWithTrainButton();
      }
      startVictoryCelebration();
    }
    void refreshLeaderboard();
  }

  function clearVictoryPresentation() {
    if (state.victoryFrame !== null) {
      window.cancelAnimationFrame(state.victoryFrame);
      state.victoryFrame = null;
    }
    if (state.victoryTimer !== null) {
      window.clearTimeout(state.victoryTimer);
      state.victoryTimer = null;
    }
    if (state.victoryConfettiTimer !== null) {
      window.clearInterval(state.victoryConfettiTimer);
      state.victoryConfettiTimer = null;
    }
    elements.victoryTrainOverlay.replaceChildren();
    elements.victoryOverlay.hidden = true;
    elements.trainAudio.pause();
    elements.trainAudio.currentTime = 0;
    elements.victoryAudio.pause();
    elements.victoryAudio.currentTime = 0;
    state.victoryCrossword = null;
    state.victoryAnimationActive = false;
  }

  function finishVictoryTrain(showTrophy) {
    state.victoryFrame = null;
    state.victoryAnimationActive = false;
    state.victoryCrossword = null;
    drawMap();
    if (showTrophy) {
      showVictoryTrophy();
    }
  }

  function positionVictoryImages() {
    if (elements.victoryOverlay.hidden) return;
    if (window.matchMedia("(max-width: 900px)").matches) {
      elements.victoryTrophy.style.left = "";
      elements.victoryTrophy.style.top = "";
      elements.victoryLocomotive.style.left = "";
      elements.victoryLocomotive.style.top = "";
      elements.victoryLocomotive.style.width = "";
      elements.victoryLocomotive.style.height = "";
      return;
    }

    const panelBounds = elements.gamePanel.getBoundingClientRect();
    const mapBounds = elements.mapScroll.getBoundingClientRect();
    elements.victoryTrophy.style.left = `${panelBounds.left + panelBounds.width / 2}px`;
    elements.victoryTrophy.style.top = `${panelBounds.top + panelBounds.height / 2}px`;
    elements.victoryLocomotive.style.left = `${mapBounds.left + mapBounds.width / 2}px`;
    elements.victoryLocomotive.style.top = `${mapBounds.top + mapBounds.height / 2}px`;
    const aspectRatio = elements.victoryLocomotive.naturalWidth
      / elements.victoryLocomotive.naturalHeight || 1;
    const imageHeight = Math.min(
      mapBounds.height * 0.9,
      mapBounds.width * 0.9 / aspectRatio,
    );
    elements.victoryLocomotive.style.width = `${imageHeight * aspectRatio}px`;
    elements.victoryLocomotive.style.height = `${imageHeight}px`;
  }

  function showVictoryTrophy() {
    elements.victoryOverlay.hidden = false;
    positionVictoryImages();
    if (typeof window.confetti !== "function") {
      console.error("Victory confetti library is unavailable.");
    } else {
      let burstCount = 0;
      const launchConfetti = () => {
        for (const [originX, angle] of [[0.08, 60], [0.92, 120]]) {
          window.confetti({
            angle,
            spread: 55,
            startVelocity: 40,
            particleCount: 100,
            ticks: 360,
            decay: 0.94,
            gravity: 0.6,
            origin: { x: originX, y: 1 },
          });
        }
        burstCount += 1;
        if (burstCount === 5 && state.victoryConfettiTimer !== null) {
          window.clearInterval(state.victoryConfettiTimer);
          state.victoryConfettiTimer = null;
        }
      };
      launchConfetti();
      state.victoryConfettiTimer = window.setInterval(launchConfetti, 900);
    }
    state.victoryTimer = window.setTimeout(() => {
      elements.victoryOverlay.hidden = true;
      state.victoryTimer = null;
    }, 8000);
  }

  function animateVictoryTrain(showTrophy) {
    const cityPath = buildCityPath(
      state.player?.source_city_id,
      state.selectedRouteIds,
    );
    const segments = [];
    for (let index = 0; index < state.selectedRouteIds.length; index += 1) {
      const source = cityById(cityPath[index]);
      const target = cityById(cityPath[index + 1]);
      if (!source || !target) continue;
      const sourceX = Number(source.left);
      const sourceY = Number(source.top);
      const targetX = Number(target.left);
      const targetY = Number(target.top);
      const length = Math.hypot(targetX - sourceX, targetY - sourceY);
      if (length > 0) {
        segments.push({ sourceX, sourceY, targetX, targetY, length });
      }
    }
    const mapScale = elements.mapOverlay.getBoundingClientRect().width / 4000;
    if (!segments.length || !mapScale) {
      finishVictoryTrain(showTrophy);
      return;
    }

    const train = svgElement("text", {
      class: "victory-train",
      "font-size": 54,
      "text-anchor": "middle",
      "dominant-baseline": "central",
      "aria-hidden": "true",
    });
    //train.textContent = "🚂";
    train.textContent = "🚃🚃";
    elements.victoryTrainOverlay.replaceChildren(train);

    const totalLength = segments.reduce((sum, segment) => sum + segment.length, 0);
    const duration = Math.max(3300, Math.min(7500, segments.length * 1050));
    const sideOffset = -10;
    let startTime = null;
    const moveTrain = (timestamp) => {
      if (startTime === null) startTime = timestamp;
      const progress = Math.min(1, (timestamp - startTime) / duration);
      let distance = totalLength * progress;
      let segment = segments[segments.length - 1];
      for (const candidate of segments) {
        if (distance <= candidate.length) {
          segment = candidate;
          break;
        }
        distance -= candidate.length;
      }

      const segmentProgress = Math.min(1, distance / segment.length);
      const dx = segment.targetX - segment.sourceX;
      const dy = segment.targetY - segment.sourceY;
      const centerX = segment.sourceX + dx * segmentProgress;
      const centerY = segment.sourceY + dy * segmentProgress;
      const angle = Math.atan2(dy, dx) * 180 / Math.PI;
      const x = centerX - dy / segment.length * sideOffset;
      const y = centerY + dx / segment.length * sideOffset;
      train.setAttribute("transform", `translate(${x} ${y}) rotate(${angle})`);

      if (progress < 1) {
        state.victoryFrame = window.requestAnimationFrame(moveTrain);
      } else {
        train.remove();
        finishVictoryTrain(showTrophy);
      }
    };
    state.victoryFrame = window.requestAnimationFrame(moveTrain);
  }

  function startVictoryCelebration() {
    clearVictoryPresentation();
    state.victoryCrossword = state.crossword.map((placement) => ({ ...placement }));
    state.victoryAnimationActive = true;
    const playback = elements.trainAudio.play();
    if (playback && typeof playback.catch === "function") {
      playback.catch((error) => {
        console.warn("Could not play the train sound.", error);
      });
    }
    animateVictoryTrain(true);
    playback = elements.victoryAudio.play();
    if (playback && typeof playback.catch === "function") {
      playback.catch((error) => {
        console.warn("Could not play the victory sound.", error);
      });
    }
  }
  
  function startToyTrain() {
    if (!hasWinningMap()) {
      hidePlayWithTrainButton();
      return;
    }
    elements.trainAudio.play();
    animateVictoryTrain(false);
    setTimeout(() => {
      elements.trainAudio.pause();       
      elements.trainAudio.currentTime = 0;
    }, 4000); 
  }

  async function refreshLeaderboard() {
    try {
      const stats = await apiRequest("/game/stats/");
      renderLeaderboard(stats.leaderboard);
      elements.participantCount.textContent = `Участников: ${numberFormat.format(stats.participant_count)}`;
      if (stats.round.id && String(stats.round.id) !== String(state.round?.id)) {
        const terminalMessage = elements.message.textContent;
        const terminalKind = elements.message.dataset.kind;
        updateRound(stats.round);
        state.player = stats.player;
        state.crossword = [];
        state.crosswordLoaded = false;
        renderPlayer();
        showMessage(terminalMessage, terminalKind);
        startSyncPolling();
      }
    } catch (error) {
      showMessage(`Раунд завершён. Не удалось обновить таблицу победителей: ${error.message}`);
    }
  }

  function startSyncPolling() {
    if (state.syncTimer !== null) return;
    const intervalSeconds = Math.max(
      1,
      Number.parseFloat(app.dataset.syncInterval) || 2,
    );
    state.syncTimer = window.setInterval(
      () => synchronizeGame(),
      intervalSeconds * 1000,
    );
  }

  function applySyncSnapshot(snapshot, requestedRoundId) {
    if (
      String(snapshot.round.id) !== String(requestedRoundId)
      || !isCurrentRoundResponse(snapshot.round)
    ) return false;

    // console.log("applySyncSnapshot");
    updateRound(snapshot.round);
    const previousPlayer = state.player;
    const playerChanged = (
      previousPlayer?.state !== snapshot.player.state
      || previousPlayer?.source_city_id !== snapshot.player.source_city_id
      || previousPlayer?.target_city_id !== snapshot.player.target_city_id
    );
    state.player = snapshot.player;
    state.shouldCelebrateVictory = (
      previousPlayer?.state !== "won"
      && snapshot.player.state === "won"
    );
    for (const placement of snapshot.crossword) {
      const cachedHints = state.hintValues.get(placement.placement_id) || {};
      if (!placement.hint_types_used.includes("first_letter")) {
        delete cachedHints.first_letter;
      }
      if (!placement.hint_types_used.includes("last_letter")) {
        delete cachedHints.last_letter;
      }
      state.hintValues.set(placement.placement_id, cachedHints);
    }
    const crosswordChanged = (
      !state.crosswordLoaded
      || JSON.stringify(state.crossword) !== JSON.stringify(snapshot.crossword)
    );
    state.crossword = snapshot.crossword;
    state.crosswordLoaded = true;
    if (!hasWinningMap()) hidePlayWithTrainButton();
    if (elements.wordDialog.open && activePlacement()?.is_guessed) {
      elements.wordDialog.close();
      state.activePlacementId = null;
    }

    if (playerChanged) {
      renderPlayer();
    } else {
      updatePlayerScore();
      updateJoinAvailability();
      updateRouteControls();
    }
    if (crosswordChanged && !playerChanged) drawMap();
    if (elements.wordDialog.open) {
      renderActivePlacement();
    }
    if (snapshot.round.status === "finished") {
      showTerminalOutcome(snapshot.round, snapshot.player);
    }
    return true;
  }

  async function synchronizeGame(roundId = state.round?.id) {
    if (!roundId || state.syncInFlight) return;
    const requestedRoundId = String(roundId);
    const version = state.round?.state_version;
    const query = new URLSearchParams({ round_id: requestedRoundId });
    if (Number.isInteger(version)) query.set("after_version", String(version));
    state.syncInFlight = true;
    let reloadCurrentRound = false;
    try {
      const snapshot = await apiRequest(`/game/sync/?${query.toString()}`);
      if (applySyncSnapshot(snapshot, requestedRoundId)) {
        if (elements.message.dataset.source === "sync") clearMessage();
      }
    } catch (error) {
      if (error.code === "stale_round" || error.code === "round_id_mismatch") {
        reloadCurrentRound = true;
      } else if (elements.message.dataset.source !== "sync") {
        showMessage(`Не удалось синхронизировать игру: ${error.message}`);
        elements.message.dataset.source = "sync";
      }
    } finally {
      state.syncInFlight = false;
    }
    if (reloadCurrentRound) await loadGame();
  }

  async function requestHint(hintType) {
    const placement = activePlacement();
    if (
      !placement
      || state.busy
      || state.player?.state !== "playing"
      || state.round?.status !== "active"
    ) return;

    state.busy = true;
    renderActivePlacement();
    try {
      const response = await apiRequest("/game/hint/", {
        method: "POST",
        body: JSON.stringify({
          round_id: state.round.id,
          placement_id: placement.placement_id,
          hint_type: hintType,
        }),
      });
      if (!isCurrentRoundResponse(response.round)) {
        await synchronizeGame(response.round.id);
        return;
      }
      updateRound(response.round);
      state.player = response.player;
      if (response.hint.hint_type === "unknown") {
        state.unavailableHints.add(`${placement.placement_id}:${hintType}`);
        setFeedback(elements.hintFeedback, "Для этого слова такая подсказка недоступна.", "error");
      } else {
        const values = state.hintValues.get(placement.placement_id) || {};
        values[hintType] = response.hint.value;
        state.hintValues.set(placement.placement_id, values);
        const charge = response.hint.points_charged
          ? ` Списано очков: ${response.hint.points_charged}.`
          : response.hint.already_used
            ? " Повторное получение бесплатно."
            : "";
        setFeedback(
          elements.hintFeedback,
          `${hintType === "first_letter" ? "Первая" : "Последняя"} буква: ${response.hint.value}.${charge}`,
          "success",
        );
      }
      updatePlayerScore();
      await synchronizeGame(response.round.id);
    } catch (error) {
      if (error.round?.id && String(error.round.id) === String(state.round?.id)) {
        applyProjectionError(error);
      }
      setFeedback(elements.hintFeedback, error.code === "insufficient_score"
        ? "Недостаточно очков для этой подсказки."
        : error.message);
    } finally {
      state.busy = false;
      renderActivePlacement();
      updateRouteControls();
    }
  }

  async function submitGuess(event) {
    event.preventDefault();
    const placement = activePlacement();
    const answer = elements.answer.value;
    if (
      !placement
      || !answer
      || state.busy
      || state.player?.state !== "playing"
      || state.round?.status !== "active"
    ) return;

    const requestedRoundId = String(state.round.id);
    state.busy = true;
    renderActivePlacement();
    setFeedback(elements.wordFeedback, "Проверяем ответ…", "success");
    try {
      const response = await apiRequest("/game/guess/", {
        method: "POST",
        body: JSON.stringify({
          round_id: requestedRoundId,
          placement_id: placement.placement_id,
          answer,
        }),
      });
      if (
        String(response.round.id) !== requestedRoundId
        || !isCurrentRoundResponse(
          response.round,
          response.state_version ?? response.round.state_version,
        )
      ) return;
      updateRound({
        ...response.round,
        state_version: response.state_version ?? response.round.state_version,
      });
      state.shouldCelebrateVictory = (
        state.player?.state !== "won"
        && response.player.state === "won"
      );
      state.player = response.player;
      updatePlayerScore();

      if (response.guess?.result === "correct") {
        if (response.player.state === "won") {
          state.crossword = state.crossword.map((item) => (
            item.placement_id === placement.placement_id
              ? {
                ...item,
                is_guessed: true,
                guessed_by_me: true,
                revealed_word: response.guess.revealed_word,
              }
              : item
          ));
        } else {
          showEmojiBlock();
        }

        setFeedback(
          elements.wordFeedback,
          `Верно! Начислено очков: ${numberFormat.format(response.guess.points_awarded)}.`,
          "success",
        );
      } else if (response.guess?.result === "already_solved") {
        setFeedback(elements.wordFeedback, "Это слово уже разгадано; ответ открыт.", "success");
      } else if (response.guess?.result === "incorrect") {
        setFeedback(elements.wordFeedback, "Неверный ответ. Попробуйте ещё раз.", "error");
      }
      if (response.round.status === "finished") {
        renderPlayer();
        showTerminalOutcome(response.round, response.player);
      } else {
        await synchronizeGame(requestedRoundId);
      }
      if (response.guess?.result === "correct") elements.answer.value = "";
    } catch (error) {
      if (error.round?.id && String(error.round.id) === requestedRoundId) {
        applyProjectionError(error);
      }
      if (error.status === 429) {
        const seconds = Number.parseInt(error.retryAfter, 10);
        state.guessBlockedUntil = Date.now() + Math.max(1, seconds || 1) * 1000;
        setFeedback(
          elements.wordFeedback,
          `Слишком много попыток. Повторите через ${Math.max(1, seconds || 1)} сек.`,
          "error",
        );
      } else {
        setFeedback(elements.wordFeedback, error.status >= 500
          ? `Сервис временно недоступен: ${error.message}`
          : error.message);
      }
    } finally {
      state.busy = false;
      renderActivePlacement();
      updateRouteControls();
    }
  }

  function canConfirmRoute() {
    return (
      state.player?.state === "selecting_route"
      && state.selectedRouteIds.length > 0
      && state.selectedCityIds[state.selectedCityIds.length - 1] === state.player.target_city_id
      && !state.busy
    );
  }

  function updateRouteControls() {
    // console.log("updateRouteControls");
    const selecting = state.player?.state === "selecting_route";
    if (selecting) {
      elements.confirmButton.style.display = 'inline-flex';
    } else {
      elements.confirmButton.style.display = 'none';
    }
    elements.confirmButton.disabled = !canConfirmRoute();
    if (!selecting) {
      elements.mapHelp.textContent = state.player?.state === "playing"
        ? "Кликните на клетки чайнворда"
        : ["won", "lost"].includes(state.player?.state)
          ? "Раунд завершён"
        : "Режим просмотра";
      elements.routeProgress.hidden = true;
      return;
    }
    const selectedCount = state.selectedRouteIds.length;
    const reachesTarget = state.selectedCityIds.at(-1) === state.player.target_city_id;
    elements.mapHelp.textContent = reachesTarget
      ? "Путь достиг города назначения"
      : "Выбирайте соседний участок";
    elements.routeProgress.hidden = false;
    elements.routeProgress.textContent = reachesTarget
      ? `Путь готов: ${selectedCount} ${selectedCount === 1 ? "участок" : "участка"}. Можно подтвердить маршрут.`
      : `Выбрано участков: ${selectedCount}. Проложите непрерывный путь до города назначения.`;
  }

  function updateJoinAvailability() {
    const canJoin = (state.player?.state === "watching_stats"
      && state.round?.status === "active"
      && state.round.remaining_seconds > 60
      && !state.busy) || state.player?.state === "won";
    if (canJoin) {
      elements.joinButton.style.display = 'inline-flex';
      elements.joinButton.disabled = false;    
      elements.joinButton.hidden = false;      
    } else {
      elements.joinButton.style.display = 'none';
      elements.joinButton.disabled = true;    
      elements.joinButton.hidden = true;      
    }
    elements.joinButton.disabled = !canJoin;
    if (state.player?.state === "watching_stats" && state.round?.status === "active" && state.round.remaining_seconds <= 60) {
      elements.playerDescription.textContent = "Присоединиться к этому раунду уже нельзя: до конца осталось не более минуты.";
    }
  }

  function renderPlayer() {
    // console.log("renderPlayer");
    const player = state.player || { state: "watching_stats" };
    const states = {
      watching_stats: ["Перед началом", "Следите за временем - раунд длится не более 6 часов. Как только один из игроков успешно завершает свой чайнворд - раунд завершается. Сразу же стартует новый раунд."],
      selecting_route: ["Выбор маршрута", "Выберите непрерывную цепочку участков от города отправления до города назначения."],
      playing: ["Игра стартовала", "Кликните на клетке чайнворда, чтобы открыть описание и ввести ответ."],
      won: ["Раунд завершён", ""],
      lost: ["Раунд завершён", "Другой игрок завершил раунд первым. В следующий раз повезет вам!"],
    };
    const [heading, description] = states[player.state] || states.watching_stats;
    elements.playerHeading.textContent = heading;
    elements.playerDescription.textContent = description;
    elements.playerCard.hidden = false;

    const start = cityById(player.source_city_id);
    const target = cityById(player.target_city_id);
    const hasAssignment = Boolean(start && target);
    elements.assignedCities.hidden = !hasAssignment;
    elements.assignedCities.replaceChildren();
    if (hasAssignment) {
      for (const [label, city] of [["Отправление", start], ["Назначение", target]]) {
        const wrapper = document.createElement("span");
        const caption = document.createElement("small");
        const name = document.createElement("span");
        wrapper.className = "assigned-city";
        caption.textContent = label;
        name.textContent = city.name;
        wrapper.append(caption, name);
        elements.assignedCities.append(wrapper);
      }
    }

    elements.joinButton.hidden = player.state !== "watching_stats";
    if (player.state == "watching_stats") {
      elements.joinButton.style.display = 'inline-flex';
      elements.joinButton.disabled = false;
    } else {
      elements.joinButton.style.display = 'none';
      elements.joinButton.disabled = true;
    }

    if (player.state == "selecting_route") {
      elements.confirmButton.style.display = 'inline-flex';  
    } else {
      elements.confirmButton.style.display = 'none';  
      elements.confirmButton.disabled = true;
    }
  
    state.player = player;
    updatePlayerScore();
    if (player.state === "selecting_route") {
      if (player.route_ids?.length) {
        state.selectedRouteIds = [...player.route_ids];
        state.selectedCityIds = buildCityPath(player.source_city_id, state.selectedRouteIds);
      } else {
        state.selectedRouteIds = [];
        state.selectedCityIds = player.source_city_id ? [player.source_city_id] : [];
      }
    } else {
      state.selectedRouteIds = [...(player.route_ids || [])];
      state.selectedCityIds = hasAssignment
        ? buildCityPath(player.source_city_id, state.selectedRouteIds)
        : [];
    }
    updateJoinAvailability();
    updateRouteControls();
    drawMap();
    if (state.round?.status === "finished") {
      showTerminalOutcome(state.round, player);
    }
  }

  function buildCityPath(startCityId, routeIds) {
    const cities = startCityId ? [startCityId] : [];
    for (const routeId of routeIds) {
      const route = routeById(routeId);
      if (!route) break;
      const tailCity = cities[cities.length - 1];
      const [sourceId, targetId] = routeEndpoints(route);
      const nextCity = sourceId === tailCity ? targetId : targetId === tailCity ? sourceId : null;
      if (nextCity === null) break;
      cities.push(nextCity);
    }
    return cities;
  }

  function selectRoute(routeId) {
    if (state.busy || state.player?.state !== "selecting_route") return;
    const selectedIndex = state.selectedRouteIds.indexOf(routeId);
    if (selectedIndex >= 0) {
      state.selectedRouteIds = state.selectedRouteIds.slice(0, selectedIndex);
      state.selectedCityIds = state.selectedCityIds.slice(0, selectedIndex + 1);
      updateRouteControls();
      drawMap();
      return;
    }

    const route = routeById(routeId);
    if (!route || !isRouteSelectable(route)) return;
    const tailCity = state.selectedCityIds[state.selectedCityIds.length - 1];
    const [sourceId, targetId] = routeEndpoints(route);
    const nextCity = sourceId === tailCity ? targetId : sourceId;
    state.selectedRouteIds.push(routeId);
    state.selectedCityIds.push(nextCity);
    updateRouteControls();
    drawMap();
  }

  function applyProjectionError(error) {
    if (error.round?.id && !isCurrentRoundResponse(error.round)) return;
    if (error.round) updateRound(error.round);
    if (error.player) {
      if (error.code === "city_pairs_exhausted") {
        error.player = { state: "watching_stats" };
      }
      renderPlayerWithProjection(error.player);
    }
  }

  function renderPlayerWithProjection(player) {
    state.player = player;
    renderPlayer();
  }

  async function loadGame() {
    elements.loading.hidden = false;
    clearMessage();
    try {
      const [mapData, stats] = await Promise.all([
        apiRequest("/game/routes/"),
        apiRequest("/game/stats/"),
      ]);
      if (mapData.round.id && stats.round.id && mapData.round.id !== stats.round.id) {
        throw new Error("Состояние раунда изменилось во время загрузки. Обновите страницу.");
      }
      state.cities = mapData.cities;
      state.routes = mapData.routes;
      updateRound(mapData.round);
      renderLeaderboard(stats.leaderboard);
      elements.participantCount.textContent = `Участников: ${numberFormat.format(stats.participant_count)}`;
      state.player = mapData.player;
      if (mapData.player.state !== "playing") {
        state.crossword = [];
        state.crosswordLoaded = false;
      }
      renderPlayer();
      elements.loading.hidden = true;
      if (mapData.round.status === "being_created") {
        showMessage("Сейчас создаётся следующий раунд. Обновите страницу чуть позже.", "success");
      } else if (
        mapData.round.id
        && ["playing", "won", "lost"].includes(mapData.player.state)
      ) {
        await synchronizeGame(mapData.round.id);
      }
    } catch (error) {
      elements.loading.hidden = true;
      showMessage(error.message);
      elements.playerCard.hidden = true;
      elements.timer.textContent = "—";
      elements.roundStatus.textContent = "Нет соединения";
    }
  }

  async function joinGame() {
    hidePlayWithTrainButton();
    elements.leaderboard.classList.add("hidden");
    elements.timerBlock.classList.remove("hidden");
    elements.confirmButton.style.display = 'none';  
    elements.confirmButton.disabled = true;
    if (elements.joinButton.disabled || state.busy) return;
    state.busy = true;
    elements.joinButton.disabled = true;
    elements.joinButton.style.display = 'none';

    clearMessage();
    try {
      const result = await apiRequest("/game/join/", { method: "POST" });
      if (state.round?.id && result.round.id !== state.round.id) {
        await loadGame();
        return;
      }
      if (!isCurrentRoundResponse(result.round)) return;
      updateRound(result.round);
      state.player = result.player;
      renderPlayer();
      if (result.player.state === "watching_stats") {
        const message = result.round.status === "being_created"
          ? "Сейчас создаётся следующий раунд. Попробуйте присоединиться чуть позже."
          : result.round.status === "finished"
            ? "Раунд уже завершён. Обновите страницу для просмотра текущего состояния."
            : "Не удалось присоединиться к раунду. Попробуйте ещё раз позже.";
        showMessage(message, "error");
      } else {
        showMessage("Города назначены. Теперь проложите путь на карте.", "success");
      }
    } catch (error) {
      applyProjectionError(error);
      showMessage(error.code === "city_pairs_exhausted"
        ? "Все допустимые пары городов уже назначены. Попробуйте присоединиться к следующему раунду."
        : error.message);
    } finally {
      state.busy = false;
      updateJoinAvailability();
      updateRouteControls();
    }
  }

  async function confirmRoute() {
    if (!canConfirmRoute() || state.busy) return;
    state.busy = true;
    state.retryRouteIds = [...state.selectedRouteIds];
    elements.confirmButton.style.display = 'none';  
    elements.confirmButton.disabled = true;
    elements.confirmButton.hidden = true;
    elements.leaderboard.classList.remove("hidden");
    clearMessage();
    try {
      const result = await apiRequest("/game/route/", {
        method: "POST",
        body: JSON.stringify({
          round_id: state.round.id,
          route_ids: state.retryRouteIds || state.selectedRouteIds,
        }),
      });
      if (result.round.id !== state.round.id) {
        state.retryRouteIds = null;
        showMessage("Ответ относится к завершённому раунду и не был применён. Обновите страницу.");
        await loadGame();
        return;
      }
      if (!isCurrentRoundResponse(result.round, result.state_version)) {
        state.retryRouteIds = null;
        await synchronizeGame();
        return;
      }
      updateRound({
        ...result.round,
        state_version: result.state_version,
      });
      state.player = result.player;
      state.crossword = result.crossword;
      state.crosswordLoaded = true;
      renderPlayer();
      state.retryRouteIds = null;
      showMessage("Маршрут подтверждён. Кроссворд готов — нажмите на клетки слова.", "success");
    } catch (error) {
      applyProjectionError(error);
      if (error.status && error.status < 500) state.retryRouteIds = null;
      if (
        error.code === "round_id_mismatch"
        || (error.round?.id && state.round?.id && error.round.id !== state.round.id)
      ) {
        state.retryRouteIds = null;
        await loadGame();
        showMessage("Состояние раунда изменилось. Данные страницы обновлены.");
        return;
      }
      if (error.code === "crossword_generation_failed") {
        state.retryRouteIds = null;
        if (error.player?.state === "watching_stats") {
          state.selectedRouteIds = [];
          state.selectedCityIds = [];
          state.player = error.player;
          renderPlayer();
          showMessage("Для выбранного пути не удалось подготовить слова. Присоединитесь к раунду и выберите другой маршрут.");
        } else {
          showMessage(error.message);
          updateRouteControls();
          drawMap();
        }
      } else if (error.code === "round_not_active") {
        state.retryRouteIds = null;
        await loadGame();
        showMessage("Раунд завершён. Данные страницы обновлены.");
      } else {
        showMessage(state.retryRouteIds
          ? `${error.message} Повторите подтверждение того же маршрута.`
          : error.message);
      }
    } finally {
      state.busy = false;
      updateJoinAvailability();
      updateRouteControls();
    }
  }

  function showEmojiBlock() {
    const emojiBlock = document.getElementById('emojiBlock');
    const images = ['👏', '👌', '👍', '👋', '🏆'];

    const randomIndex = Math.floor(Math.random() * images.length);
    emojiBlock.innerHTML = images[randomIndex];
    emojiBlock.style.left = `${clickX}px`;
    emojiBlock.style.top = `${clickY}px`;
    emojiBlock.classList.remove('hidden');

    setTimeout(() => {
        emojiBlock.classList.add('hidden');
    }, 2300);
  }

  elements.joinButton.addEventListener("click", joinGame);
  elements.confirmButton.addEventListener("click", confirmRoute);
  elements.mapOverlay.addEventListener("click", handleRouteClick);
  elements.mapOverlay.addEventListener("keydown", handleMapKeydown);
  function applyMapScale() {
    const selectedScale = elements.mapScaleControl.querySelector(
      'input[name="map-scale"]:checked',
    );
    if (!selectedScale) return;
    const mobileScale = window.matchMedia("(max-width: 900px)").matches
      ? 0.85
      : 1;
    elements.mapCanvas.style.zoom = String(Number(selectedScale.value) * mobileScale); 
  }

  elements.mapScaleControl.addEventListener("change", (event) => {
    if (event.target.name === "map-scale") applyMapScale();
  });
  elements.guessForm.addEventListener("submit", submitGuess);
  elements.firstHint.addEventListener("click", () => requestHint("first_letter"));
  elements.lastHint.addEventListener("click", () => requestHint("last_letter"));
  elements.closeWordDialog.addEventListener("click", () => elements.wordDialog.close());
  elements.playWithTrain.addEventListener("click", startToyTrain);
  elements.wordDialog.addEventListener("click", (event) => {
    if (event.target === elements.wordDialog) elements.wordDialog.close();
  });
  window.addEventListener("resize", () => {
    positionVictoryImages();
    applyMapScale();
  });
  window.addEventListener("scroll", positionVictoryImages, true);
  elements.confirmButton.style.display = 'none';
  elements.confirmButton.disabled = true;
  applyMapScale();
  window.setInterval(updateTimer, 1000);
  startSyncPolling();
  loadGame();
})();
