import {
  initialSelectedCityIds,
  mergeSessionSnapshot,
  wordCenterOffset,
} from "./session_state.js";

(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const CELL_SIZE = 26;
  const app = document.getElementById("game-app");
  if (!app) return;

  const localizationNode = document.getElementById("i18n-data");
  if (!localizationNode) throw new Error("Missing localization data.");
  const localization = JSON.parse(localizationNode.textContent);
  const locale = localization.lang;
  if (locale !== "ru" && locale !== "en") throw new Error("Unsupported game language.");
  const translations = localization.js;
  const numberFormat = new Intl.NumberFormat(locale);
  const pluralRules = new Intl.PluralRules(locale);

  const el = {
    timer: document.getElementById("round-timer"),
    status: document.getElementById("round-status"),
    message: document.getElementById("game-message"),
    loading: document.getElementById("game-loading"),
    card: document.getElementById("player-card"),
    heading: document.getElementById("player-heading"),
    description: document.getElementById("player-description"),
    score: document.getElementById("player-score"),
    cities: document.getElementById("assigned-cities"),
    progress: document.getElementById("route-progress"),
    progressText: document.getElementById("route-progress-text"),
    autoRoute: document.getElementById("auto-route-button"),
    join: document.getElementById("join-button"),
    surrender: document.getElementById("surrender-button"),
    confirm: document.getElementById("confirm-route-button"),
    participantCount: document.getElementById("participant-count"),
    leaderboard: document.getElementById("leaderboard-list"),
    mapHelp: document.getElementById("map-help"),
    mapPanel: document.querySelector(".game-panel"),
    mapScroll: document.getElementById("map-scroll"),
    mapCanvas: document.getElementById("map-canvas"),
    mapScale: document.querySelector(".map-scale-control"),
    overlay: document.getElementById("map-overlay"),
    victoryOverlay: document.getElementById("victory-overlay"),
    victoryAudio: document.getElementById("victory-audio"),
    trainAudio: document.getElementById("train-audio"),
    wordDialog: document.getElementById("word-dialog"),
    wordTitle: document.getElementById("word-dialog-title"),
    definition: document.getElementById("word-definition"),
    wordLength: document.getElementById("word-length"),
    guessForm: document.getElementById("guess-form"),
    answer: document.getElementById("word-answer"),
    submitGuess: document.getElementById("submit-guess"),
    wordFeedback: document.getElementById("word-feedback"),
    hintFeedback: document.getElementById("hint-feedback"),
    firstHint: document.getElementById("first-letter-hint"),
    lastHint: document.getElementById("last-letter-hint"),
    closeDialog: document.getElementById("close-word-dialog"),
  };
  el.join.dataset.joinLabel = t("start_game", el.join.textContent.trim());

  const snapshot = { session: null, player: null, crossword: [] };
  const state = {
    cities: [],
    routes: [],
    selectedRouteIds: [],
    selectedCityIds: [],
    activePlacementId: null,
    busy: false,
    syncInFlight: false,
    epoch: 0,
    syncTimer: null,
    victoryTimer: null,
    victoryConfettiTimer: null,
  };

  const pendingOperations = new Map();
  let panState = null;

  function t(key, fallback = "") {
    return translations?.[key] || fallback || key;
  }

  function setFeedback(node, message, kind = "error") {
    node.textContent = message || "";
    node.dataset.kind = kind;
  }

  function showMessage(message, kind = "error") {
    el.message.textContent = message || "";
    el.message.dataset.kind = kind;
    el.message.hidden = !message;
  }

  function clearMessage() {
    el.message.textContent = "";
    el.message.hidden = true;
    delete el.message.dataset.kind;
  }

  function getCsrfToken() {
    const cookieName = document.querySelector('meta[name="csrf-cookie-name"]')?.content || "csrf_token";
    const prefix = `${cookieName}=`;
    const cookie = document.cookie.split(";").map((part) => part.trim())
      .find((part) => part.startsWith(prefix));
    return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : "";
  }

  async function apiRequest(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body) headers.set("Content-Type", "application/json");
    if (options.method && options.method !== "GET") headers.set("X-CSRF-Token", getCsrfToken());

    let response;
    try {
      response = await fetch(url, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    } catch {
      throw new Error(t("error_fetch"));
    }

    let body;
    try {
      body = await response.json();
    } catch {
      throw new Error(t("error_response"));
    }
    if (!response.ok) {
      const error = new Error(body?.error?.message || localErrorMessage(body?.error?.code));
      error.code = body?.error?.code;
      error.status = response.status;
      error.session = body?.session;
      error.player = body?.player;
      error.retryAfter = response.headers.get("Retry-After");
      throw error;
    }
    return body;
  }

  function localErrorMessage(code) {
    const messages = {
      insufficient_points: t("hint_denied"),
      route_already_solved: t("route_already_solved", t("error_request")),
      no_available_route: t("no_available_route", t("error_request")),
      crossword_generation_failed: t("error_words", t("error_request")),
    };
    return messages[code] || t("error_request");
  }

  function storageRead(key) {
    try { return window.sessionStorage.getItem(key); } catch { return pendingOperations.get(key) || null; }
  }

  function storageWrite(key, value) {
    pendingOperations.set(key, value);
    try { window.sessionStorage.setItem(key, value); } catch { /* The in-memory copy still supports a retry. */ }
  }

  function storageRemove(key) {
    pendingOperations.delete(key);
    try { window.sessionStorage.removeItem(key); } catch { /* Storage can be disabled by the browser. */ }
  }

  function createUuid() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
      const random = Math.floor(Math.random() * 16);
      return (char === "x" ? random : (random & 0x3) | 0x8).toString(16);
    });
  }

  function operationKey(action, sessionId = "") {
    const storageKey = `railway:${action}:${sessionId || "new"}`;
    let key = storageRead(storageKey);
    if (!key) {
      key = createUuid();
      storageWrite(storageKey, key);
    }
    return { key, storageKey };
  }

  function cityById(cityId) {
    return state.cities.find((city) => Number(city.id) === Number(cityId));
  }

  function routeById(routeId) {
    return state.routes.find((route) => Number(route.id) === Number(routeId));
  }

  function routeEndpoints(route) {
    return [Number(route.source_city_id), Number(route.target_city_id)];
  }

  function buildCityPath(sourceId, routeIds) {
    const path = [Number(sourceId)];
    for (const routeId of routeIds) {
      const route = routeById(routeId);
      if (!route) return [];
      const [source, target] = routeEndpoints(route);
      const tail = path.at(-1);
      const next = tail === source ? target : tail === target ? source : null;
      if (next === null || path.includes(next)) return [];
      path.push(next);
    }
    return path;
  }

  function selectable(route) {
    if (snapshot.player?.state !== "selecting_route" || !snapshot.player.source_city_id) return false;
    const existingIndex = state.selectedRouteIds.indexOf(Number(route.id));
    if (existingIndex >= 0) return true;
    if (!state.selectedCityIds.length) return false;
    const tail = state.selectedCityIds.at(-1);
    if (tail === Number(snapshot.player.target_city_id)) return false;
    const [source, target] = routeEndpoints(route);
    const next = tail === source ? target : tail === target ? source : null;
    return next !== null && !state.selectedCityIds.includes(next);
  }

  function saveDraftRoute() {
    const sessionId = snapshot.session?.id;
    if (!sessionId) return;
    storageWrite(`railway:draft-route:${sessionId}`, JSON.stringify(state.selectedRouteIds));
  }

  function restoreDraftRoute() {
    const sessionId = snapshot.session?.id;
    if (!sessionId || snapshot.player?.state !== "selecting_route") return;
    try {
      const stored = storageRead(`railway:draft-route:${sessionId}`);
      const routeIds = stored ? JSON.parse(stored) : [];
      const cityIds = buildCityPath(snapshot.player.source_city_id, routeIds);
      if (Array.isArray(routeIds) && routeIds.length && cityIds.length === routeIds.length + 1
        && cityIds.at(-1) !== Number(snapshot.player.source_city_id)) {
        state.selectedRouteIds = routeIds.map(Number);
        state.selectedCityIds = cityIds;
      }
    } catch {
      state.selectedRouteIds = [];
      state.selectedCityIds = initialSelectedCityIds(
        snapshot.player?.state,
        snapshot.player?.source_city_id,
      );
    }
  }

  function svgElement(tag, attributes = {}) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
    return node;
  }

  function routeIsSolved(routeId) {
    const words = snapshot.crossword.filter((placement) => Number(placement.route_id) === Number(routeId));
    return words.length > 0 && words.every((placement) => placement.is_guessed);
  }

  function drawCrossword() {
    const layer = svgElement("g", { "aria-label": t("crossword_cells") });
    for (const placement of snapshot.crossword) {
      const route = routeById(placement.route_id);
      const source = route && cityById(route.source_city_id);
      const target = route && cityById(route.target_city_id);
      if (!route || !source || !target) continue;

      const sourceX = Number(source.left);
      const sourceY = Number(source.top);
      const dx = Number(target.left) - sourceX;
      const dy = Number(target.top) - sourceY;
      const segmentLength = Math.hypot(dx, dy);
      const centerDistance = wordCenterOffset(placement.offset_px, placement.letter_count);
      if (!Number.isFinite(segmentLength) || segmentLength <= 0 || centerDistance > segmentLength) continue;

      const centerX = sourceX + dx * centerDistance / segmentLength;
      const centerY = sourceY + dy * centerDistance / segmentLength;
      let angle = Math.atan2(dy, dx) * 180 / Math.PI;
      if (angle > 90) angle -= 180;
      if (angle < -90) angle += 180;
      const canOpen = snapshot.player?.state === "playing" && !placement.is_guessed;
      const group = svgElement("g", {
        class: `crossword-word${placement.is_guessed ? " is-guessed" : ""}`,
        transform: `translate(${centerX} ${centerY}) rotate(${angle})`,
        "data-placement-id": placement.placement_id,
        role: "button",
        tabindex: canOpen ? "0" : "-1",
        "aria-label": `${placement.definition}. ${placement.letter_count} ${t("crossword_area_label")}`,
        "aria-pressed": placement.is_guessed ? "true" : "false",
      });
      const width = Number(placement.letter_count) * CELL_SIZE;
      const revealed = placement.is_guessed ? Array.from(placement.word_text || "") : [];
      for (let index = 0; index < placement.letter_count; index += 1) {
        const x = -width / 2 + index * CELL_SIZE;
        group.append(svgElement("rect", {
          x, y: -CELL_SIZE / 2, width: CELL_SIZE, height: CELL_SIZE, rx: 4, class: "crossword-cell",
        }));
        if (revealed[index]) {
          const label = svgElement("text", {
            x: x + CELL_SIZE / 2, y: -1.5, class: "crossword-cell-text",
          });
          label.style.fontSize = `${Math.min(20, CELL_SIZE * 0.8)}px`;
          label.textContent = revealed[index];
          group.append(label);
        }
      }
      layer.append(group);
    }
    return layer;
  }

  function drawMap() {
    el.overlay.replaceChildren();
    const routeLayer = svgElement("g", { "aria-label": t("route_aria_label") });
    const cityLayer = svgElement("g", { "aria-label": t("city_aria_label") });
    const displayRoutes = snapshot.player?.state === "selecting_route"
      ? state.selectedRouteIds
      : (snapshot.player?.route_ids || []);
    const visited = new Set(snapshot.player?.state === "selecting_route"
      ? state.selectedCityIds
      : buildCityPath(snapshot.player?.source_city_id, displayRoutes));

    for (const route of state.routes) {
      const source = cityById(route.source_city_id);
      const target = cityById(route.target_city_id);
      if (!source || !target) continue;
      const canSelect = selectable(route);
      const line = svgElement("line", {
        x1: source.left, y1: source.top, x2: target.left, y2: target.top,
        class: `map-route${displayRoutes.includes(Number(route.id)) ? " is-selected" : ""}${canSelect ? " is-selectable" : ""}${routeIsSolved(route.id) ? " is-solved" : ""}`,
        "data-route-id": route.id,
        tabindex: canSelect ? "0" : "-1",
        role: canSelect ? "button" : "img",
        "aria-label": `${t("line_aria_label")} ${source.name} — ${target.name}`,
      });
      line.addEventListener("keydown", (event) => {
        if (canSelect && (event.key === "Enter" || event.key === " ")) {
          event.preventDefault();
          selectRoute(Number(route.id));
        }
      });
      routeLayer.append(line);
    }

    for (const city of state.cities) {
      const left = Number(city.left);
      const top = Number(city.top);
      const classes = ["map-city"];
      if (Number(city.id) === Number(snapshot.player?.source_city_id)) classes.push("is-start");
      if (Number(city.id) === Number(snapshot.player?.target_city_id)) classes.push("is-target");
      if (visited.has(Number(city.id))) classes.push("is-visited");
      const group = svgElement("g", { "aria-label": city.name, "data-city-id": city.id });
      group.append(svgElement("circle", { cx: left, cy: top, r: 18, class: classes.join(" ") }));
      const label = svgElement("text", {
        x: left > 3600 ? left - 24 : left + 24,
        y: top < 70 ? top + 65 : top > 2900 ? top - 30 : top - 22,
        class: "map-city-label", "text-anchor": left > 3600 ? "end" : "start", "font-size": 14,
        "stroke-width": 4,
      });
      label.textContent = city.name;
      group.append(label);
      cityLayer.append(group);
    }
    el.overlay.append(routeLayer, drawCrossword(), cityLayer);
  }

  function formatWordLength(count) {
    const form = pluralRules.select(count);
    const template = t(`word_length_${form}`, t("word_length_other"));
    return template.replace("{count}", numberFormat.format(count));
  }

  function formatState(playerState) {
    const labels = {
      watching_stats: t("watching_stats"),
      selecting_route: t("selecting_route"),
      playing: t("playing"),
      finishing: t("finishing"),
      won: t("status_won"),
    };
    return labels[playerState] || t("status_unknown");
  }

  function formatSessionStatus(status) {
    const labels = {
      active: t("session_active"),
      finishing: t("session_finishing"),
      finished: t("session_finished_status"),
    };
    return labels[status] || t("status_unknown");
  }

  function renderLeaderboard(rows) {
    el.leaderboard.replaceChildren();
    if (!rows?.length) {
      const item = document.createElement("li");
      item.className = "leaderboard-empty";
      item.textContent = t("leaderboard_empty");
      el.leaderboard.append(item);
      return;
    }
    for (const row of rows) {
      const item = document.createElement("li");
      const content = document.createElement("span");
      const username = document.createElement("span");
      const score = document.createElement("span");
      content.className = "leaderboard-row";
      username.className = "leaderboard-username";
      score.className = "leaderboard-score";
      username.textContent = row.username;
      score.textContent = numberFormat.format(row.score);
      content.append(username, score);
      item.append(content);
      el.leaderboard.append(item);
    }
  }

  function renderAssignedCities() {
    const player = snapshot.player;
    el.cities.replaceChildren();
    const source = cityById(player?.source_city_id);
    const target = cityById(player?.target_city_id);
    el.cities.hidden = !snapshot.session || !source || !target;
    if (!source || !target) return;
    for (const [label, city] of [[t("start_city"), source], [t("end_city"), target]]) {
      const item = document.createElement("div");
      const name = document.createElement("span");
      const role = document.createElement("small");
      item.className = "assigned-city";
      name.textContent = city.name;
      role.textContent = label;
      item.append(name, role);
      el.cities.append(item);
    }
  }

  function updateRouteControls() {
    const selecting = snapshot.player?.state === "selecting_route";
    const pathComplete = selecting
      && state.selectedCityIds.at(-1) === Number(snapshot.player.target_city_id);
    el.progress.hidden = !selecting;
    el.autoRoute.hidden = !selecting;
    el.autoRoute.disabled = state.busy;
    el.progressText.textContent = pathComplete
      ? t("route_success")
      : `${t("route_paths")}: ${numberFormat.format(state.selectedRouteIds.length)}. ${t("route_select")}`;
    el.confirm.hidden = !selecting;
    el.confirm.disabled = !pathComplete || state.busy;
    el.surrender.hidden = snapshot.player?.state !== "playing";
    el.join.hidden = Boolean(snapshot.session && snapshot.player?.state !== "won");
    el.join.textContent = snapshot.player?.state === "won" ? t("new_game") : el.join.dataset.joinLabel;
  }

  function renderPlayer() {
    const player = snapshot.player || { state: "watching_stats" };
    const session = snapshot.session;
    el.card.hidden = false;
    el.heading.textContent = player.state === "won" ? t("status_won") : t("statistics_title");
    el.description.textContent = player.state === "won"
      ? t("message_won")
      : t(`help_${player.state}`, t("help_watching_stats"));
    el.timer.textContent = formatState(player.state);
    el.status.textContent = session ? formatSessionStatus(session.status) : t("session_none");
    el.status.dataset.state = player.state;
    const score = Math.max(0, Number(player.session_score) || 0);
    el.score.hidden = !session;
    el.score.textContent = `${t("session_score")}: ${numberFormat.format(score)}`;
    if (player.state === "won") {
      showMessage(t("message_won"), "victory");
    } else if (el.message.dataset.kind === "victory") {
      clearMessage();
    }
    renderAssignedCities();
    updateRouteControls();
    el.mapHelp.textContent = player.state === "playing"
      ? t("map_help_playing")
      : player.state === "watching_stats" || player.state === "won"
        ? t("map_help_stat")
        : t("help_selecting_route");
    drawMap();
    if (el.wordDialog.open) renderActivePlacement();
  }

  function resetLocalSession() {
    snapshot.session = null;
    snapshot.player = { state: "watching_stats", session_score: 0 };
    snapshot.crossword = [];
    state.selectedRouteIds = [];
    state.selectedCityIds = [];
    state.activePlacementId = null;
    if (el.wordDialog.open) el.wordDialog.close();
    el.victoryOverlay.hidden = true;
    clearVictoryTimers();
    renderPlayer();
  }

  function clearVictoryTimers() {
    if (state.victoryTimer !== null) window.clearTimeout(state.victoryTimer);
    if (state.victoryConfettiTimer !== null) window.clearInterval(state.victoryConfettiTimer);
    state.victoryTimer = null;
    state.victoryConfettiTimer = null;
    el.trainAudio.pause();
    el.trainAudio.currentTime = 0;
    el.victoryAudio.pause();
    el.victoryAudio.currentTime = 0;
  }

  function celebrateVictory() {
    clearVictoryTimers();
    el.victoryOverlay.hidden = false;
    const play = el.victoryAudio.play();
    if (play?.catch) play.catch(() => {});
    const throwConfetti = () => {
      if (typeof window.confetti !== "function") return;
      for (const [originX, angle] of [[0.08, 60], [0.92, 120]]) {
        window.confetti({
          angle, spread: 55, startVelocity: 40, particleCount: 90, ticks: 320,
          decay: 0.94, gravity: 0.6, origin: { x: originX, y: 1 },
        });
      }
    };
    throwConfetti();
    state.victoryConfettiTimer = window.setInterval(throwConfetti, 6500);
    state.victoryTimer = window.setTimeout(() => {
      el.victoryOverlay.hidden = true;
      clearVictoryTimers();
    }, 30000);
  }

  function applySnapshot(incoming, expectedEpoch, options = {}) {
    if (expectedEpoch !== state.epoch) return false;
    const previousPlayerState = snapshot.player?.state;
    if (incoming && !incoming.session && snapshot.session && options.clearMissingSession) {
      const expectedId = options.expectedSessionId;
      if (!expectedId || String(expectedId) !== String(snapshot.session.id)) return false;
      resetLocalSession();
      return true;
    }
    const merged = mergeSessionSnapshot(snapshot, incoming, options);
    if (!merged) return false;
    snapshot.session = merged.session || null;
    snapshot.player = merged.player || { state: "watching_stats", session_score: 0 };
    snapshot.crossword = merged.crossword || [];
    if (incoming?.player?.state === "selecting_route" && !Array.isArray(incoming.crossword)) {
      snapshot.crossword = [];
    }

    if (incoming?.player?.state === "selecting_route" && previousPlayerState !== "selecting_route") {
      state.selectedRouteIds = [];
      state.selectedCityIds = initialSelectedCityIds(
        incoming.player.state,
        incoming.player.source_city_id,
      );
    }
    if (snapshot.player.state !== "selecting_route") {
      state.selectedRouteIds = [];
      state.selectedCityIds = [];
      if (snapshot.session?.id) storageRemove(`railway:draft-route:${snapshot.session.id}`);
    }
    renderPlayer();
    if (snapshot.player.state === "won" && previousPlayerState === "playing" && options.live !== false) {
      celebrateVictory();
    }
    if (snapshot.player.state === "won" && previousPlayerState !== "won" && options.live !== false) {
      void refreshStats();
    }
    if (["playing", "finishing"].includes(snapshot.player.state)) startSyncPolling();
    else stopSyncPolling();
    return true;
  }

  function applyErrorProjection(error, expectedEpoch, sessionId) {
    if (!error.session && !error.player) return true;
    if (!error.session) {
      if (sessionId || snapshot.session) return false;
      snapshot.player = error.player;
      renderPlayer();
      return true;
    }
    if (!sessionId || String(error.session.id) !== String(sessionId)) return false;
    if (!error.player) return true;
    return applySnapshot({ session: error.session, player: error.player }, expectedEpoch);
  }

  function applyMapScale() {
    const selected = el.mapScale.querySelector('input[name="map-scale"]:checked');
    if (!selected) return;
    const mobileScale = window.matchMedia("(max-width: 900px)").matches ? 0.85 : 1;
    el.mapCanvas.style.zoom = String(Number(selected.value) * mobileScale);
  }

  function formatLeaderboardStats(stats) {
    renderLeaderboard(stats.leaderboard);
    el.participantCount.textContent = `${t("participants")}: ${numberFormat.format(stats.participant_count || 0)}`;
  }

  async function loadGame() {
    const requestEpoch = state.epoch;
    const expectedSessionId = snapshot.session?.id || null;
    el.loading.hidden = false;
    clearMessage();
    try {
      const [mapData, stats] = await Promise.all([
        apiRequest("/game/routes/"),
        apiRequest("/game/stats/"),
      ]);
      if (requestEpoch !== state.epoch) return;
      state.cities = mapData.cities || [];
      state.routes = mapData.routes || [];
      formatLeaderboardStats(stats);
      if (!mapData.session) {
        if (!snapshot.session) {
          snapshot.player = mapData.player || { state: "watching_stats", session_score: 0 };
          snapshot.crossword = [];
          state.selectedRouteIds = [];
          state.selectedCityIds = [];
          renderPlayer();
        } else if (expectedSessionId) {
          applySnapshot({ session: null, player: mapData.player }, requestEpoch, {
            clearMissingSession: true, expectedSessionId,
          });
        }
      } else {
        const accepted = applySnapshot(mapData, requestEpoch, { live: false });
        if (accepted && mapData.player?.state === "selecting_route") restoreDraftRoute();
        if (accepted && snapshot.player?.state === "selecting_route") renderPlayer();
      }
      if (mapData.session) storageRemove("railway:join:new");
      el.loading.hidden = true;
      if (snapshot.player?.state === "playing" || snapshot.player?.state === "finishing") {
        startSyncPolling();
      } else {
        stopSyncPolling();
      }
    } catch (error) {
      if (requestEpoch !== state.epoch) return;
      el.loading.hidden = true;
      showMessage(error.message);
      el.status.textContent = t("error_connection");
    }
  }

  function startSyncPolling() {
    if (state.syncTimer !== null) return;
    const configured = Number(app.dataset.syncInterval);
    const interval = Math.max(1000, Number.isFinite(configured) ? configured * 1000 : 2000);
    state.syncTimer = window.setInterval(synchronizeGame, interval);
  }

  function stopSyncPolling() {
    if (state.syncTimer !== null) window.clearInterval(state.syncTimer);
    state.syncTimer = null;
  }

  async function synchronizeGame() {
    const sessionId = snapshot.session?.id;
    if (!sessionId || state.syncInFlight
      || !["playing", "finishing"].includes(snapshot.player?.state)) return;
    const requestEpoch = state.epoch;
    state.syncInFlight = true;
    const query = new URLSearchParams({ session_id: String(sessionId) });
    if (Number.isInteger(snapshot.session.state_version)) {
      query.set("after_version", String(snapshot.session.state_version));
    }
    try {
      const result = await apiRequest(`/game/sync/?${query}`);
      if (requestEpoch !== state.epoch || String(result.session?.id) !== String(sessionId)) return;
      applySnapshot(result, requestEpoch);
      if (snapshot.player?.state === "won") stopSyncPolling();
    } catch (error) {
      if (requestEpoch !== state.epoch) return;
      if (error.status === 404) {
        const current = await apiRequest("/game/routes/").catch(() => null);
        if (current && requestEpoch === state.epoch) {
          state.cities = current.cities || state.cities;
          state.routes = current.routes || state.routes;
          if (!current.session) {
            applySnapshot({ session: null, player: current.player }, requestEpoch, {
              clearMissingSession: true, expectedSessionId: sessionId,
            });
            stopSyncPolling();
          }
        }
      } else if (error.status >= 500 || !error.status) {
        showMessage(error.message, "error");
      }
    } finally {
      state.syncInFlight = false;
    }
  }

  function selectRoute(routeId) {
    if (snapshot.player?.state !== "selecting_route" || state.busy) return;
    const existingIndex = state.selectedRouteIds.indexOf(routeId);
    if (existingIndex >= 0) {
      state.selectedRouteIds = state.selectedRouteIds.slice(0, existingIndex);
      state.selectedCityIds = buildCityPath(snapshot.player.source_city_id, state.selectedRouteIds);
    } else {
      const route = routeById(routeId);
      if (!route || !selectable(route)) return;
      const [source, target] = routeEndpoints(route);
      const tail = state.selectedCityIds.at(-1);
      const next = tail === source ? target : source;
      state.selectedRouteIds.push(routeId);
      state.selectedCityIds.push(next);
    }
    saveDraftRoute();
    renderPlayer();
  }

  function handleMapClick(event) {
    if (event.ctrlKey || event.metaKey) return;
    const word = event.target.closest("[data-placement-id]");
    if (word) {
      openPlacementDialog(word.getAttribute("data-placement-id"));
      return;
    }
    const line = event.target.closest("[data-route-id]");
    if (line) selectRoute(Number(line.getAttribute("data-route-id")));
  }

  function renderActivePlacement() {
    const placement = snapshot.crossword.find((item) => item.placement_id === state.activePlacementId);
    if (!placement) return;
    el.wordTitle.textContent = t("word_dialog_title");
    el.definition.textContent = placement.definition;
    el.wordLength.textContent = formatWordLength(placement.letter_count);
    const active = snapshot.player?.state === "playing" && !placement.is_guessed;
    el.answer.disabled = !active || state.busy;
    el.submitGuess.disabled = !active || state.busy;
    el.firstHint.hidden = !active;
    el.lastHint.hidden = !active;
    el.firstHint.disabled = state.busy;
    el.lastHint.disabled = state.busy;
    el.firstHint.textContent = placement.hint_types_used.includes("first_letter")
      ? `${t("show_again")}: ${t("hint_first").toLocaleLowerCase(locale)}` : t("hint_first");
    el.lastHint.textContent = placement.hint_types_used.includes("last_letter")
      ? `${t("show_again")}: ${t("hint_last").toLocaleLowerCase(locale)}` : t("hint_last");
    if (placement.is_guessed && placement.word_text) {
      setFeedback(el.wordFeedback, `${t("answer")}: ${placement.word_text}`, "success");
    }
    const hints = (placement.hints || []).map((hint) => {
      const label = hint.hint_type === "first_letter" ? t("hint_first") : t("hint_last");
      return `${label}: ${hint.value}`;
    });
    if (hints.length || el.hintFeedback.dataset.kind !== "error") {
      setFeedback(el.hintFeedback, hints.join(" · "), "success");
    }
  }

  function openPlacementDialog(placementId) {
    const placement = snapshot.crossword.find((item) => item.placement_id === placementId);
    if (!placement || placement.is_guessed || snapshot.player?.state !== "playing") return;
    state.activePlacementId = placementId;
    el.answer.value = "";
    el.answer.placeholder = "_".repeat(placement.letter_count);
    setFeedback(el.wordFeedback, "", "success");
    setFeedback(el.hintFeedback, "", "success");
    renderActivePlacement();
    el.wordDialog.showModal();
    el.answer.focus();
  }

  async function joinGame() {
    if (state.busy) return;
    const oldSessionId = snapshot.session?.id || null;
    const requestEpoch = ++state.epoch;
    const operation = operationKey("join");
    state.busy = true;
    el.join.disabled = true;
    clearMessage();
    try {
      const result = await apiRequest("/game/join/", {
        method: "POST",
        headers: { "Idempotency-Key": operation.key },
      });
      if (requestEpoch !== state.epoch) return;
      if (!result.session || !result.player) throw new Error(t("error_response"));
      storageRemove(operation.storageKey);
      if (oldSessionId) storageRemove(`railway:draft-route:${oldSessionId}`);
      state.selectedRouteIds = [];
      state.selectedCityIds = [];
      clearVictoryTimers();
      el.victoryOverlay.hidden = true;
      if (!applySnapshot(result, requestEpoch, { allowSessionReplacement: true, live: false })) {
        await reloadSessionSnapshot(requestEpoch, result.session.id);
        return;
      }
      showMessage(t("help_route_start"), "success");
    } catch (error) {
      if (requestEpoch !== state.epoch) return;
      if (error.status && error.status < 500 && error.code !== "join_result_unavailable"
        && error.code !== "victory_finishing") storageRemove(operation.storageKey);
      if (!applyErrorProjection(error, requestEpoch, oldSessionId)) return;
      showMessage(error.message);
      if (error.code === "city_pairs_exhausted") renderPlayer();
    } finally {
      if (requestEpoch === state.epoch) {
        state.busy = false;
        el.join.disabled = false;
        updateRouteControls();
      }
    }
  }

  async function autoRoute() {
    const sessionId = snapshot.session?.id;
    if (!sessionId || snapshot.player?.state !== "selecting_route" || state.busy) return;
    state.busy = true;
    updateRouteControls();
    clearMessage();
    const requestEpoch = state.epoch;
    try {
      const result = await apiRequest("/game/route/auto/", {
        method: "POST", body: JSON.stringify({ session_id: sessionId }),
      });
      if (requestEpoch !== state.epoch || String(result.session?.id) !== String(sessionId)) return;
      if (!applySnapshot(result, requestEpoch)) {
        await reloadSessionSnapshot(requestEpoch, sessionId);
        return;
      }
      const cityPath = buildCityPath(result.player.source_city_id, result.route_ids || []);
      if (cityPath.length !== result.route_ids.length + 1
        || cityPath.at(-1) !== Number(result.player.target_city_id)) {
        throw new Error(t("error_route_shape"));
      }
      state.selectedRouteIds = result.route_ids.map(Number);
      state.selectedCityIds = cityPath;
      saveDraftRoute();
      renderPlayer();
    } catch (error) {
      if (requestEpoch === state.epoch) {
        if (!applyErrorProjection(error, requestEpoch, sessionId)) {
          void reloadSessionSnapshot(requestEpoch, sessionId).catch(() => {});
          return;
        }
        showMessage(error.message);
      }
    } finally {
      if (requestEpoch === state.epoch) {
        state.busy = false;
        updateRouteControls();
      }
    }
  }

  async function confirmRoute() {
    const sessionId = snapshot.session?.id;
    if (!sessionId || snapshot.player?.state !== "selecting_route" || state.busy
      || state.selectedCityIds.at(-1) !== Number(snapshot.player.target_city_id)) return;
    const routeIds = [...state.selectedRouteIds];
    const requestEpoch = state.epoch;
    state.busy = true;
    clearMessage();
    updateRouteControls();
    try {
      const result = await apiRequest("/game/route/", {
        method: "POST", body: JSON.stringify({ session_id: sessionId, route_ids: routeIds }),
      });
      if (requestEpoch !== state.epoch || String(result.session?.id) !== String(sessionId)) return;
      storageRemove(`railway:draft-route:${sessionId}`);
      if (!applySnapshot(result, requestEpoch)) {
        await reloadSessionSnapshot(requestEpoch, sessionId);
        return;
      }
      if (!Array.isArray(result.crossword) || result.player.state === "won") {
        await reloadSessionSnapshot(requestEpoch, sessionId);
      }
      showMessage(t("route_confirmed"), "success");
    } catch (error) {
      if (requestEpoch === state.epoch) {
        if (!applyErrorProjection(error, requestEpoch, sessionId)) {
          void reloadSessionSnapshot(requestEpoch, sessionId).catch(() => {});
          return;
        }
        if (error.code === "route_already_solved") {
          state.selectedRouteIds = [];
          state.selectedCityIds = initialSelectedCityIds(
            snapshot.player?.state,
            snapshot.player?.source_city_id,
          );
          storageRemove(`railway:draft-route:${sessionId}`);
          renderPlayer();
        }
        showMessage(error.message);
      }
    } finally {
      if (requestEpoch === state.epoch) {
        state.busy = false;
        updateRouteControls();
      }
    }
  }

  async function reloadSessionSnapshot(requestEpoch, sessionId) {
    const result = await apiRequest("/game/routes/");
    if (requestEpoch !== state.epoch || String(result.session?.id) !== String(sessionId)) return;
    state.cities = result.cities || state.cities;
    state.routes = result.routes || state.routes;
    applySnapshot(result, requestEpoch, { live: false });
    if (["playing", "finishing"].includes(snapshot.player?.state)) startSyncPolling();
  }

  async function submitGuess(event) {
    event.preventDefault();
    const placement = snapshot.crossword.find((item) => item.placement_id === state.activePlacementId);
    const sessionId = snapshot.session?.id;
    if (!placement || !sessionId || state.busy || snapshot.player?.state !== "playing") return;
    const requestEpoch = state.epoch;
    state.busy = true;
    el.submitGuess.textContent = t("checking");
    renderActivePlacement();
    try {
      const result = await apiRequest("/game/guess/", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, placement_id: placement.placement_id, answer: el.answer.value }),
      });
      if (requestEpoch !== state.epoch || String(result.session?.id) !== String(sessionId)) return;
      if (!applySnapshot(result, requestEpoch)) {
        await reloadSessionSnapshot(requestEpoch, sessionId);
        return;
      }
      if (result.guess?.result === "incorrect") {
        setFeedback(el.wordFeedback, t("incorrect"), "error");
      } else if (result.guess?.revealed_word) {
        const current = snapshot.crossword.find((item) => item.placement_id === placement.placement_id);
        if (current) {
          current.is_guessed = true;
          current.guessed_by_me = result.guess.result === "correct";
          current.word_text = result.guess.revealed_word;
        }
        drawMap();
        setFeedback(el.wordFeedback, `${t("answer")}: ${result.guess.revealed_word}`, "success");
        if (result.guess.result === "already_solved") showMessage(t("already_solved"), "success");
      }
      if (["correct", "already_solved"].includes(result.guess?.result)) {
        state.activePlacementId = null;
        if (el.wordDialog.open) el.wordDialog.close();
      }
      if (["won", "finishing"].includes(result.player?.state)) {
        await reloadSessionSnapshot(requestEpoch, sessionId);
        if (result.player.state === "finishing") startSyncPolling();
      }
    } catch (error) {
      if (requestEpoch === state.epoch) {
        if (!applyErrorProjection(error, requestEpoch, sessionId)) {
          void reloadSessionSnapshot(requestEpoch, sessionId).catch(() => {});
          return;
        }
        setFeedback(el.wordFeedback, error.message, "error");
      }
    } finally {
      if (requestEpoch === state.epoch) {
        state.busy = false;
        el.submitGuess.textContent = t("submit_guess");
        renderActivePlacement();
      }
    }
  }

  async function requestHint(hintType) {
    const placement = snapshot.crossword.find((item) => item.placement_id === state.activePlacementId);
    const sessionId = snapshot.session?.id;
    if (!placement || !sessionId || state.busy || snapshot.player?.state !== "playing") return;
    state.busy = true;
    const requestEpoch = state.epoch;
    renderActivePlacement();
    try {
      const result = await apiRequest("/game/hint/", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, placement_id: placement.placement_id, hint_type: hintType }),
      });
      if (requestEpoch !== state.epoch || String(result.session?.id) !== String(sessionId)) return;
      if (!applySnapshot(result, requestEpoch)) {
        await reloadSessionSnapshot(requestEpoch, sessionId);
        return;
      }
      const current = snapshot.crossword.find((item) => item.placement_id === placement.placement_id);
      if (result.hint?.hint_type !== "unknown" && current) {
        current.hint_types_used = [...new Set([...(current.hint_types_used || []), result.hint.hint_type])];
        const hints = current.hints || [];
        const existing = hints.find((hint) => hint.hint_type === result.hint.hint_type);
        if (existing) existing.value = result.hint.value;
        else hints.push({ hint_type: result.hint.hint_type, value: result.hint.value });
        current.hints = hints;
        const charged = Math.max(0, Number(result.hint.points_charged) || 0);
        setFeedback(el.hintFeedback, charged
          ? `${t("deducted")}: ${numberFormat.format(charged)}`
          : t("hint_received"), "success");
      } else {
        setFeedback(el.hintFeedback, t("hint_unavailable"), "error");
      }
    } catch (error) {
      if (requestEpoch === state.epoch) {
        if (!applyErrorProjection(error, requestEpoch, sessionId)) {
          void reloadSessionSnapshot(requestEpoch, sessionId).catch(() => {});
          return;
        }
        setFeedback(el.hintFeedback, error.message, "error");
      }
    } finally {
      if (requestEpoch === state.epoch) {
        state.busy = false;
        renderActivePlacement();
      }
    }
  }

  async function surrender() {
    const sessionId = snapshot.session?.id;
    if (!sessionId || state.busy || !["selecting_route", "playing"].includes(snapshot.player?.state)) return;
    const requestEpoch = state.epoch;
    const operation = operationKey("surrender", sessionId);
    state.busy = true;
    clearMessage();
    updateRouteControls();
    try {
      const result = await apiRequest("/game/surrender/", {
        method: "POST",
        headers: { "Idempotency-Key": operation.key },
        body: JSON.stringify({ session_id: sessionId }),
      });
      if (requestEpoch !== state.epoch) return;
      if (result.session && result.player) {
        storageRemove(operation.storageKey);
        if (!applySnapshot(result, requestEpoch)) {
          await reloadSessionSnapshot(requestEpoch, sessionId);
          return;
        }
        if (result.player.state === "won") await reloadSessionSnapshot(requestEpoch, sessionId);
      } else if (String(result.surrender?.session_id) === String(sessionId)
        && result.surrender.result === "surrendered") {
        storageRemove(operation.storageKey);
        state.busy = false;
        state.epoch += 1;
        resetLocalSession();
        stopSyncPolling();
        showMessage(t("surrendered"), "success");
        void refreshStats();
      } else {
        throw new Error(t("error_response"));
      }
    } catch (error) {
      if (requestEpoch === state.epoch) {
        if (!applyErrorProjection(error, requestEpoch, sessionId)) {
          void reloadSessionSnapshot(requestEpoch, sessionId).catch(() => {});
          return;
        }
        showMessage(error.message);
        if (error.code === "victory_finishing") startSyncPolling();
      }
    } finally {
      if (requestEpoch === state.epoch) {
        state.busy = false;
        updateRouteControls();
      }
    }
  }

  async function refreshStats() {
    const requestEpoch = state.epoch;
    try {
      const stats = await apiRequest("/game/stats/");
      if (requestEpoch === state.epoch) formatLeaderboardStats(stats);
    } catch {
      // Leaderboard updates do not affect the current session snapshot.
    }
  }

  function initMapPanning() {
    el.mapScroll.addEventListener("mousedown", (event) => {
      if (event.button !== 0 || (!event.ctrlKey && !event.metaKey)) return;
      event.preventDefault();
      panState = {
        x: event.clientX, y: event.clientY,
        left: el.mapScroll.scrollLeft, top: el.mapScroll.scrollTop,
      };
      el.mapScroll.classList.add("is-panning");
    });
    window.addEventListener("mousemove", (event) => {
      if (!panState) return;
      el.mapScroll.scrollLeft = panState.left - (event.clientX - panState.x);
      el.mapScroll.scrollTop = panState.top - (event.clientY - panState.y);
    });
    window.addEventListener("mouseup", () => {
      panState = null;
      el.mapScroll.classList.remove("is-panning");
    });
    window.addEventListener("keydown", (event) => {
      if (event.key === "Control" || event.key === "Meta") el.mapScroll.classList.add("can-pan");
    });
    window.addEventListener("keyup", (event) => {
      if (event.key === "Control" || event.key === "Meta") el.mapScroll.classList.remove("can-pan");
    });
  }

  el.mapScale.addEventListener("change", (event) => {
    if (event.target.name === "map-scale") applyMapScale();
  });
  el.overlay.addEventListener("click", handleMapClick);
  el.overlay.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && event.target.matches("[data-placement-id]")) {
      event.preventDefault();
      openPlacementDialog(event.target.getAttribute("data-placement-id"));
    }
  });
  el.join.addEventListener("click", joinGame);
  el.surrender.addEventListener("click", surrender);
  el.confirm.addEventListener("click", confirmRoute);
  el.autoRoute.addEventListener("click", autoRoute);
  el.guessForm.addEventListener("submit", submitGuess);
  el.firstHint.addEventListener("click", () => requestHint("first_letter"));
  el.lastHint.addEventListener("click", () => requestHint("last_letter"));
  el.closeDialog.addEventListener("click", () => el.wordDialog.close());
  el.wordDialog.addEventListener("click", (event) => {
    if (event.target === el.wordDialog) el.wordDialog.close();
  });
  window.addEventListener("resize", applyMapScale);

  initMapPanning();
  applyMapScale();
  loadGame();
})();
