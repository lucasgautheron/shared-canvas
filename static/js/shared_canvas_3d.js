/* global THREE, psynet */
(function (root) {
  "use strict";

  var CAR_LENGTH = 18;
  var CAR_WIDTH = 10;
  var CAR_HEIGHT = 5;
  var STEER_RATE = 2.6;
  var ACCELERATION = 900;
  var BRAKE = 1100;
  var COAST_FRICTION = 0.92;
  var WORLD_SCALE = 24;
  var MAX_MAP_STORMS = 1200;
  var OVERVIEW_HEIGHT_RATIO = 0.54;
  var BORDER_MARGIN_MILES = 25;
  var CHASER_TRACK_COLORS = ["#2563eb", "#7c3aed", "#0891b2", "#db2777", "#059669", "#ea580c"];

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, value));
  }

  function hexToInt(hex, fallback) {
    if (!hex) return fallback;
    var cleaned = String(hex).replace("#", "");
    var parsed = parseInt(cleaned, 16);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function potentialCssColor(vil) {
    return interpolateCssColor([
      [0, "#377eb8"],
      [10, "#4daf4a"],
      [25, "#ffcc33"],
      [45, "#ff7f00"],
      [65, "#e41a1c"],
      [85, "#984ea3"],
    ], vil);
  }

  function zoneCssColor(eventName) {
    var event = String(eventName || "").toLowerCase();
    if (event.indexOf("tornado") >= 0) return "rgba(228, 26, 28, 0.30)";
    if (event.indexOf("severe") >= 0) return "rgba(255, 204, 51, 0.26)";
    return "rgba(55, 126, 184, 0.22)";
  }

  function hexToRgb(hex) {
    var n = parseInt(String(hex).replace("#", ""), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function interpolateCssColor(stops, value) {
    if (value == null || Number.isNaN(Number(value))) return "#377eb8";
    var numeric = Number(value);
    if (numeric <= stops[0][0]) return stops[0][1];
    for (var i = 1; i < stops.length; i++) {
      if (numeric <= stops[i][0]) {
        var amount = (numeric - stops[i - 1][0]) / Math.max(1e-9, stops[i][0] - stops[i - 1][0]);
        var c0 = hexToRgb(stops[i - 1][1]);
        var c1 = hexToRgb(stops[i][1]);
        return "rgb(" +
          Math.round(c0[0] + (c1[0] - c0[0]) * amount) + "," +
          Math.round(c0[1] + (c1[1] - c0[1]) * amount) + "," +
          Math.round(c0[2] + (c1[2] - c0[2]) * amount) + ")";
      }
    }
    return stops[stops.length - 1][1];
  }

  function makeCar(color, scale) {
    var group = new THREE.Group();
    var body = new THREE.Mesh(
      new THREE.BoxGeometry(CAR_LENGTH * scale, CAR_HEIGHT * scale, CAR_WIDTH * scale),
      new THREE.MeshStandardMaterial({ color: color, roughness: 0.45, metalness: 0.15 })
    );
    body.position.y = CAR_HEIGHT * scale * 0.55;
    var cabin = new THREE.Mesh(
      new THREE.BoxGeometry(CAR_LENGTH * 0.42 * scale, CAR_HEIGHT * 0.7 * scale, CAR_WIDTH * 0.82 * scale),
      new THREE.MeshStandardMaterial({ color: 0x1f2937, roughness: 0.3 })
    );
    cabin.position.set(CAR_LENGTH * 0.08 * scale, CAR_HEIGHT * scale * 1.15, 0);
    var nose = new THREE.Mesh(
      new THREE.BoxGeometry(CAR_LENGTH * 0.18 * scale, CAR_HEIGHT * 0.25 * scale, CAR_WIDTH * 0.55 * scale),
      new THREE.MeshStandardMaterial({ color: 0xf8fafc })
    );
    nose.position.set(CAR_LENGTH * 0.42 * scale, CAR_HEIGHT * scale * 0.7, 0);
    group.add(body);
    group.add(cabin);
    group.add(nose);
    return group;
  }

  function headingFromVelocity(vx, vy, fallback) {
    if (Math.hypot(vx, vy) < 0.01) return fallback || 0;
    return Math.atan2(vy, vx);
  }

  root.SharedCanvas3D = {
    start: function startSharedCanvas3D(cfg) {
      var isLobby = cfg.mode === "lobby";
      var participantId = String(
        cfg.participant_id ||
        (psynet.session && psynet.session.participant_id) ||
        ""
      );
      var canvas = document.getElementById("shared-canvas");
      var waitingOverlay = document.getElementById("waiting-overlay");
      var overviewCanvas = document.getElementById("overview-map");
      var overviewCtx = overviewCanvas ? overviewCanvas.getContext("2d") : null;
      var zoomSlider = document.getElementById("zoom-slider");
      var zoomLabel = document.getElementById("zoom-label");
      var canvasWidth = Number(cfg.canvas_width || 960);
      var canvasHeight = Number(cfg.canvas_height || 540);
      var canvasSize = Number(cfg.canvas_size || 640);
      var pxPerMile = Number((cfg.projection || {}).px_per_mile || 1);
      var viewportMiles = zoomSlider ? Number(zoomSlider.value || 750) : 750;
      var initialPlayers = cfg.initial_players || {};
      var players = {};
      var stormPoints = (cfg.storm_points || []).slice();
      var stormCursor = 0;
      var activePotential = [];
      var lastPotentialTime = 0;
      var chaserTracks = (cfg.chaser_tracks || []).slice();
      var chaserTrackVisibleMs = Number(cfg.chaser_track_visible_ms || 30 * 60 * 1000);
      var warnings = cfg.warnings || [];
      var rewardEvents = cfg.reward_events || [];
      var timing = cfg.timing || {};
      var serverStartedAt = null;
      var gameEndsAt = null;
      var collectedCoinIds = [];
      var pendingCollectionIds = {};
      var keys = {};
      var driveKeys = {
        ArrowLeft: true,
        ArrowRight: true,
        ArrowUp: true,
        ArrowDown: true,
        KeyA: true,
        KeyD: true,
        KeyW: true,
        KeyS: true,
      };
      var own = Object.assign(
        {
          participant_id: participantId,
          label: cfg.role,
          color: "#1f77b4",
          x: canvasSize / 2,
          y: canvasSize / 2,
          vx: 0,
          vy: 0,
          heading: 0,
          speed: 0,
          client_time: performance.now(),
          received_at: performance.now(),
        },
        cfg.initial_player || {}
      );
      own.heading = headingFromVelocity(own.vx, own.vy, 0);
      own.speed = Math.hypot(Number(own.vx) || 0, Number(own.vy) || 0);
      var bonus = 0;
      var lastDrawAt = performance.now();
      var submitted = false;
      var gameStarted = isLobby;
      var renderer = null;
      var scene = null;
      var camera = null;
      var mapCamera = { x: 0, y: 0, initialized: false };
      var overviewSize = 540;
      var carMeshes = {};
      var rewardMeshes = [];
      var cameraOffset = new THREE.Vector3();
      var lookTarget = new THREE.Vector3();

      function worldX(x) {
        return Number(x) * WORLD_SCALE;
      }

      function worldZ(y) {
        return Number(y) * WORLD_SCALE;
      }

      Object.keys(initialPlayers).forEach(function (id) {
        players[String(id)] = Object.assign({}, initialPlayers[id], {
          received_at: performance.now(),
          heading: headingFromVelocity(initialPlayers[id].vx, initialPlayers[id].vy, 0),
        });
      });
      players[participantId] = Object.assign({}, own);

      function gameTime(now) {
        if (serverStartedAt == null) return 0;
        return Math.min(cfg.trial_seconds * 1000, Math.max(0, now - serverStartedAt));
      }

      function rawTimeForGameTime(t) {
        var rawMin = Number(timing.raw_time_min_ms);
        var rawMax = Number(timing.raw_time_max_ms);
        var gameEnd = Number(timing.game_end_ms || cfg.trial_seconds * 1000);
        if (!Number.isFinite(rawMin) || !Number.isFinite(rawMax) || rawMax <= rawMin || gameEnd <= 0) {
          return null;
        }
        return rawMin + Math.max(0, Math.min(1, Number(t || 0) / gameEnd)) * (rawMax - rawMin);
      }

      function updateWaitingOverlay(readyIds) {
        if (!waitingOverlay) return;
        if (isLobby) {
          waitingOverlay.classList.remove("hidden");
          waitingOverlay.textContent = "Waiting for other players";
          return;
        }
        waitingOverlay.classList.toggle("hidden", gameStarted);
        if (!gameStarted) {
          var readyCount = Array.isArray(readyIds) ? readyIds.length : 0;
          waitingOverlay.textContent = readyCount > 0
            ? "Waiting for other participants... (" + readyCount + " ready)"
            : "Waiting for other participants...";
        }
      }

      function applyServerStartTime(serverStartRaw, now, allowLargeJump) {
        if (!serverStartRaw) return;
        if (serverStartedAt != null && !allowLargeJump) return;
        var serverStartMs = Date.parse(serverStartRaw);
        if (Number.isFinite(serverStartMs)) {
          serverStartedAt = now - Math.max(0, Date.now() - serverStartMs);
        } else {
          serverStartedAt = now;
        }
        gameStarted = true;
        gameEndsAt = serverStartedAt + cfg.trial_seconds * 1000;
        lastDrawAt = now;
        stormCursor = 0;
        activePotential = [];
        lastPotentialTime = 0;
        updateWaitingOverlay();
      }

      function viewportWorldWidth() {
        return Math.max(1, viewportMiles * pxPerMile);
      }

      function mapScreenScale() {
        return overviewSize / viewportWorldWidth();
      }

      function clampMapCamera() {
        var width = viewportWorldWidth();
        if (width >= canvasSize) {
          mapCamera.x = (canvasSize - width) / 2;
          mapCamera.y = (canvasSize - width) / 2;
          return;
        }
        mapCamera.x = clamp(mapCamera.x, 0, canvasSize - width);
        mapCamera.y = clamp(mapCamera.y, 0, canvasSize - width);
      }

      function centerMapCameraOn(player) {
        var width = viewportWorldWidth();
        mapCamera.x = Number(player.x || 0) - width / 2;
        mapCamera.y = Number(player.y || 0) - width / 2;
        mapCamera.initialized = true;
        clampMapCamera();
      }

      function updateMapCameraForPlayer(player) {
        if (!player) return;
        if (!mapCamera.initialized) {
          centerMapCameraOn(player);
          return;
        }
        var width = viewportWorldWidth();
        var margin = Math.min(width / 2, BORDER_MARGIN_MILES * pxPerMile);
        var x = Number(player.x || 0);
        var y = Number(player.y || 0);
        if (x < mapCamera.x + margin) mapCamera.x = x - margin;
        if (x > mapCamera.x + width - margin) mapCamera.x = x - width + margin;
        if (y < mapCamera.y + margin) mapCamera.y = y - margin;
        if (y > mapCamera.y + width - margin) mapCamera.y = y - width + margin;
        clampMapCamera();
      }

      function worldToMap(x, y) {
        var scale = mapScreenScale();
        return {
          x: (Number(x) - mapCamera.x) * scale,
          y: (Number(y) - mapCamera.y) * scale,
        };
      }

      function isOnMap(point, pad) {
        var padding = pad || 0;
        return point.x >= -padding && point.x <= overviewSize + padding &&
          point.y >= -padding && point.y <= overviewSize + padding;
      }

      function wsSend(payload) {
        if (submitted) return;
        var type = payload.type;
        if (!type) return;
        var message = Object.assign({}, payload);
        delete message.type;
        psynet.websocket.send(type, message);
      }

      function updateBonus(value) {
        bonus = Number(value || 0);
        var bonusEl = document.getElementById("coin-bonus");
        if (bonusEl) bonusEl.textContent = bonus.toFixed(2);
        var performanceReward = document.getElementById("performance-reward");
        var totalReward = document.getElementById("total-reward");
        var rewardDetails = document.getElementById("reward-details");
        if (performanceReward) performanceReward.textContent = bonus.toFixed(2);
        if (totalReward) {
          var timeRewardEl = document.getElementById("time-reward");
          var timeReward = timeRewardEl ? Number(timeRewardEl.textContent || 0) : 0;
          totalReward.textContent = (timeReward + bonus).toFixed(2);
        }
        if (rewardDetails) rewardDetails.style.display = "";
      }

      function applySnapshot(msg) {
        var state = msg.state || {};
        var now = performance.now();
        timing = msg.timing || timing;
        if (state.server_start_time) {
          timing.server_start_time = state.server_start_time;
        }
        var bonuses = state.bonuses || {};
        updateBonus(bonuses[participantId] || 0);
        collectedCoinIds = (state.collected_coins || [])
          .filter(function (collection) {
            return String(collection.participant_id) === participantId;
          })
          .map(function (collection) {
            return String(collection.coin_id);
          });
        if (msg.started) {
          applyServerStartTime(timing.server_start_time, now, true);
        }
        if (!mapCamera.initialized) {
          updateMapCameraForPlayer(own);
        }
        updateWaitingOverlay(msg.ready_participant_ids || []);
      }

      function applyPosition(player) {
        var id = String(player.participant_id);
        if (id === participantId) return;
        var next = Object.assign({}, players[id] || {}, player, {
          received_at: performance.now(),
        });
        next.heading = headingFromVelocity(next.vx, next.vy, next.heading || 0);
        players[id] = next;
      }

      function applyCollection(msg) {
        var collection = msg.collection || {};
        var targetId = String(collection.target_id || collection.coin_id || "");
        if (targetId) delete pendingCollectionIds[targetId];
        if (String(collection.participant_id) === participantId) {
          if (targetId && collectedCoinIds.indexOf(targetId) < 0) {
            collectedCoinIds.push(targetId);
          }
          updateBonus((msg.bonuses || {})[participantId] || (bonus + cfg.coin_bonus));
        } else if (msg.bonuses) {
          updateBonus(msg.bonuses[participantId] || bonus);
        }
      }

      function handleCollectRejected(msg) {
        if (msg.coin_id) delete pendingCollectionIds[msg.coin_id];
        return msg;
      }

      function integrateOwnPlayer(now) {
        if (!gameStarted) {
          lastDrawAt = now;
          return;
        }
        var dt = Math.min(0.08, (now - lastDrawAt) / 1000);
        var maxSpeed = Number(cfg.max_player_speed || 0);
        var steer = 0;
        var throttle = 0;
        if (keys.ArrowLeft || keys.KeyA) steer -= 1;
        if (keys.ArrowRight || keys.KeyD) steer += 1;
        if (keys.ArrowUp || keys.KeyW) throttle += 1;
        if (keys.ArrowDown || keys.KeyS) throttle -= 1;
        var speedRatio = maxSpeed > 0 ? Math.min(1, Math.abs(own.speed) / maxSpeed) : 0;
        own.heading += steer * STEER_RATE * dt * (0.25 + 0.75 * speedRatio);
        if (throttle > 0) {
          own.speed += ACCELERATION * dt;
        } else if (throttle < 0) {
          own.speed -= BRAKE * dt;
        } else {
          own.speed *= Math.pow(COAST_FRICTION, dt * 60);
        }
        own.speed = clamp(own.speed, -maxSpeed * 0.35, maxSpeed);
        own.vx = Math.cos(own.heading) * own.speed;
        own.vy = Math.sin(own.heading) * own.speed;
        var margin = Math.max(Number(cfg.player_radius || 12), 8);
        own.x = clamp(own.x + own.vx * dt, margin, canvasSize - margin);
        own.y = clamp(own.y + own.vy * dt, margin, canvasSize - margin);
        own.client_time = now;
        own.received_at = now;
        players[participantId] = Object.assign({}, own);
        updateMapCameraForPlayer(own);
        lastDrawAt = now;
      }

      function renderedPlayer(player, now) {
        var age = Math.min(160, now - (player.received_at || now)) / 1000;
        return Object.assign({}, player, {
          x: Number(player.x || 0) + Number(player.vx || 0) * age,
          y: Number(player.y || 0) + Number(player.vy || 0) * age,
          heading: headingFromVelocity(player.vx, player.vy, player.heading || 0),
        });
      }

      function distanceToSegment(px, py, ax, ay, bx, by) {
        var dx = bx - ax;
        var dy = by - ay;
        if (dx === 0 && dy === 0) return Math.hypot(px - ax, py - ay);
        var amount = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy);
        amount = clamp(amount, 0, 1);
        return Math.hypot(px - (ax + amount * dx), py - (ay + amount * dy));
      }

      function distanceToRewardEvent(event) {
        var distances = [Math.hypot(Number(event.x) - own.x, Number(event.y) - own.y)];
        var line = event.line || [];
        for (var i = 0; i < line.length - 1; i++) {
          distances.push(distanceToSegment(
            own.x,
            own.y,
            Number(line[i][0]),
            Number(line[i][1]),
            Number(line[i + 1][0]),
            Number(line[i + 1][1])
          ));
        }
        return Math.min.apply(null, distances);
      }

      function checkRewardCollections(t, now) {
        rewardEvents.forEach(function (event) {
          var eventId = String(event.id);
          if (pendingCollectionIds[eventId]) return;
          if (collectedCoinIds.indexOf(eventId) >= 0) return;
          if (Number(event.start_ms) > t || Number(event.end_ms) < t) return;
          var collectRadius = Number(event.radius || cfg.coin_radius);
          if (distanceToRewardEvent(event) > collectRadius) return;
          pendingCollectionIds[eventId] = true;
          wsSend({
            type: "collect",
            coin_id: eventId,
            x: own.x,
            y: own.y,
            client_time: now,
            game_time_ms: t,
          });
        });
      }

      function advancePotential(t) {
        if (t < lastPotentialTime) {
          stormCursor = 0;
          activePotential = [];
        }
        lastPotentialTime = t;
        while (stormCursor < stormPoints.length && Number(stormPoints[stormCursor][0]) <= t) {
          activePotential.push(stormPoints[stormCursor]);
          stormCursor += 1;
        }
        activePotential = activePotential.filter(function (point) {
          return Number(point[1]) >= t;
        });
      }

      function createRewardMesh(event) {
        var group = new THREE.Group();
        var radius = Math.max(4, Number(event.radius || cfg.coin_radius)) * WORLD_SCALE;
        var disk = new THREE.Mesh(
          new THREE.CylinderGeometry(radius, radius, 2.4, 48),
          new THREE.MeshStandardMaterial({
            color: 0xf4c430,
            emissive: 0xa27400,
            emissiveIntensity: 0.25,
            transparent: true,
            opacity: 0.28,
            depthWrite: false,
          })
        );
        disk.position.y = 1.2;
        group.add(disk);
        var coin = new THREE.Mesh(
          new THREE.CylinderGeometry(6, 6, 2.4, 16),
          new THREE.MeshStandardMaterial({ color: 0xffe566, emissive: 0xf4c430, emissiveIntensity: 0.4 })
        );
        coin.rotation.z = Math.PI / 2;
        coin.position.y = 8;
        group.add(coin);
        if (event.line && event.line.length >= 2) {
          var points = event.line.map(function (point) {
            return new THREE.Vector3(
              worldX(point[0]) - worldX(event.x),
              1.2,
              worldZ(point[1]) - worldZ(event.y)
            );
          });
          group.add(new THREE.Line(
            new THREE.BufferGeometry().setFromPoints(points),
            new THREE.LineBasicMaterial({ color: 0xf4c430 })
          ));
        }
        group.position.set(worldX(event.x), 0, worldZ(event.y));
        group.visible = false;
        group.userData = event;
        return group;
      }

      function ensureCarMesh(id, player, isOwn) {
        if (carMeshes[id]) return carMeshes[id];
        var mesh = makeCar(hexToInt(player.color, isOwn ? 0x1f77b4 : 0xd62728), isOwn ? 1 : 0.9);
        scene.add(mesh);
        carMeshes[id] = mesh;
        return mesh;
      }

      function layoutRenderer() {
        var stage = canvas.parentElement;
        var width = Math.max(320, stage ? stage.clientWidth : canvasWidth);
        var height = Math.max(180, Math.round(width * canvasHeight / canvasWidth));
        var pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
        canvas.style.width = width + "px";
        canvas.style.height = height + "px";
        renderer.setPixelRatio(pixelRatio);
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
        overviewSize = Math.round(height * OVERVIEW_HEIGHT_RATIO);
        if (!isLobby && overviewCanvas && overviewCtx) {
          overviewCanvas.style.width = overviewSize + "px";
          overviewCanvas.style.height = overviewSize + "px";
          overviewCanvas.width = Math.round(overviewSize * pixelRatio);
          overviewCanvas.height = Math.round(overviewSize * pixelRatio);
          overviewCtx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
        }
      }

      function setupScene() {
        var worldSize = canvasSize * WORLD_SCALE;
        scene = new THREE.Scene();
        scene.background = new THREE.Color(0x6f90b0);
        scene.fog = new THREE.Fog(0x6f90b0, worldSize * 0.45, worldSize);

        camera = new THREE.PerspectiveCamera(62, canvasWidth / canvasHeight, 0.5, worldSize * 1.2);

        renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true, alpha: false });
        renderer.setClearColor(0x87a7c3, 1);

        scene.add(new THREE.AmbientLight(0xffffff, 0.38));
        var sun = new THREE.DirectionalLight(0xfff4d6, 0.75);
        sun.position.set(worldSize * 0.2, 400, worldSize * 0.1);
        scene.add(sun);
        scene.add(new THREE.HemisphereLight(0x9ec0e6, 0x4f6f38, 0.28));

        var ground = new THREE.Mesh(
          new THREE.PlaneGeometry(worldSize, worldSize),
          new THREE.MeshStandardMaterial({ color: 0x4f7d3d, roughness: 1 })
        );
        ground.rotation.x = -Math.PI / 2;
        ground.position.set(worldSize / 2, 0, worldSize / 2);
        scene.add(ground);

        var grid = new THREE.GridHelper(worldSize, 80, 0x7ea35f, 0x5d8a45);
        grid.position.set(worldSize / 2, 0.05, worldSize / 2);
        scene.add(grid);

        if (!isLobby) {
          rewardEvents.forEach(function (event) {
            var mesh = createRewardMesh(event);
            rewardMeshes.push(mesh);
            scene.add(mesh);
          });
        }

        layoutRenderer();
      }

      function updateRewards(t) {
        rewardMeshes.forEach(function (mesh) {
          var event = mesh.userData;
          var eventId = String(event.id);
          var active = Number(event.start_ms) <= t && Number(event.end_ms) >= t;
          mesh.visible = active && collectedCoinIds.indexOf(eventId) < 0;
        });
      }

      function updateCars(now) {
        Object.keys(players).forEach(function (id) {
          var isOwn = id === participantId;
          var player = isOwn ? own : renderedPlayer(players[id], now);
          var mesh = ensureCarMesh(id, player, isOwn);
          mesh.position.set(worldX(player.x), 0, worldZ(player.y));
          mesh.rotation.y = -(player.heading || 0);
          mesh.visible = true;
        });
      }

      function syncWaiters(waiterIds) {
        var allowed = {};
        (waiterIds || []).forEach(function (id) {
          allowed[String(id)] = true;
        });
        allowed[participantId] = true;
        Object.keys(players).forEach(function (id) {
          if (allowed[id]) return;
          delete players[id];
          if (carMeshes[id]) {
            scene.remove(carMeshes[id]);
            delete carMeshes[id];
          }
        });
      }

      function updateCameras() {
        var follow = 22;
        var height = 13;
        var x = worldX(own.x);
        var z = worldZ(own.y);
        cameraOffset.set(
          x - Math.cos(own.heading) * follow,
          height,
          z - Math.sin(own.heading) * follow
        );
        camera.position.lerp(cameraOffset, 0.22);
        lookTarget.set(x + Math.cos(own.heading) * 48, 2, z + Math.sin(own.heading) * 48);
        camera.lookAt(lookTarget);
      }

      function drawMapGrid() {
        var step = Math.max(1, 50 * pxPerMile);
        var width = viewportWorldWidth();
        var xStart = Math.floor(mapCamera.x / step) * step;
        var yStart = Math.floor(mapCamera.y / step) * step;
        overviewCtx.strokeStyle = "#d8e2ef";
        overviewCtx.lineWidth = 1;
        for (var x = xStart; x <= mapCamera.x + width; x += step) {
          var sx = worldToMap(x, mapCamera.y).x;
          overviewCtx.beginPath();
          overviewCtx.moveTo(sx, 0);
          overviewCtx.lineTo(sx, overviewSize);
          overviewCtx.stroke();
        }
        for (var y = yStart; y <= mapCamera.y + width; y += step) {
          var sy = worldToMap(mapCamera.x, y).y;
          overviewCtx.beginPath();
          overviewCtx.moveTo(0, sy);
          overviewCtx.lineTo(overviewSize, sy);
          overviewCtx.stroke();
        }
      }

      function drawMapZones(t) {
        warnings.forEach(function (zone) {
          if (Number(zone.eff_ms) > t || Number(zone.exp_ms) < t) return;
          (zone.polygons || []).forEach(function (polygon) {
            if (!polygon.length) return;
            overviewCtx.beginPath();
            polygon.forEach(function (point, index) {
              var screen = worldToMap(point[0], point[1]);
              if (index === 0) overviewCtx.moveTo(screen.x, screen.y);
              else overviewCtx.lineTo(screen.x, screen.y);
            });
            overviewCtx.closePath();
            overviewCtx.fillStyle = zoneCssColor(zone.event);
            overviewCtx.fill();
            overviewCtx.strokeStyle = "rgba(40, 40, 40, 0.65)";
            overviewCtx.lineWidth = 2.5;
            overviewCtx.stroke();
          });
        });
      }

      function drawMapChaserTracks(t) {
        var currentRawMs = rawTimeForGameTime(t);
        var minRawMs = currentRawMs == null ? null : currentRawMs - chaserTrackVisibleMs;
        chaserTracks.forEach(function (track, trackIndex) {
          var visiblePoints = (track.points || []).filter(function (point) {
            var rawMs = Number(point[3]);
            if (Number(point[0]) > t) return false;
            if (minRawMs == null || !Number.isFinite(rawMs)) return true;
            return rawMs >= minRawMs && rawMs <= currentRawMs;
          });
          if (visiblePoints.length < 1) return;
          var color = CHASER_TRACK_COLORS[trackIndex % CHASER_TRACK_COLORS.length];
          overviewCtx.save();
          if (visiblePoints.length >= 2) {
            overviewCtx.beginPath();
            visiblePoints.forEach(function (point, index) {
              var screen = worldToMap(point[1], point[2]);
              if (index === 0) overviewCtx.moveTo(screen.x, screen.y);
              else overviewCtx.lineTo(screen.x, screen.y);
            });
            overviewCtx.strokeStyle = color;
            overviewCtx.lineWidth = 2.5;
            overviewCtx.globalAlpha = 0.78;
            overviewCtx.stroke();
          }
          var lastPoint = visiblePoints[visiblePoints.length - 1];
          var marker = worldToMap(lastPoint[1], lastPoint[2]);
          if (isOnMap(marker, 8)) {
            overviewCtx.globalAlpha = 1;
            overviewCtx.beginPath();
            overviewCtx.arc(marker.x, marker.y, 4, 0, Math.PI * 2);
            overviewCtx.fillStyle = color;
            overviewCtx.fill();
            overviewCtx.strokeStyle = "rgba(255, 255, 255, 0.9)";
            overviewCtx.lineWidth = 1.5;
            overviewCtx.stroke();
          }
          overviewCtx.restore();
        });
      }

      function drawMapPotential(t) {
        advancePotential(t);
        var visiblePotential = activePotential.map(function (point) {
          return { point: point, screen: worldToMap(point[2], point[3]) };
        }).filter(function (entry) {
          return isOnMap(entry.screen, 24);
        });
        if (visiblePotential.length > MAX_MAP_STORMS) {
          visiblePotential = visiblePotential.slice().sort(function (a, b) {
            return Number(b.point[4] || 0) - Number(a.point[4] || 0);
          }).slice(0, MAX_MAP_STORMS);
        }
        visiblePotential.forEach(function (entry) {
          var point = entry.point;
          var screen = entry.screen;
          var vil = point[4];
          var size = Math.max(8, Math.min(cfg.player_radius * 1.6, 7 + Number(vil || 0) * 0.12));
          var half = size / 2;
          overviewCtx.fillStyle = potentialCssColor(vil);
          overviewCtx.fillRect(screen.x - half, screen.y - half, size, size);
          overviewCtx.strokeStyle = "rgba(255, 255, 255, 0.75)";
          overviewCtx.lineWidth = 1;
          overviewCtx.strokeRect(screen.x - half, screen.y - half, size, size);
        });
      }

      function drawMapRewards(t) {
        var scale = mapScreenScale();
        rewardEvents.forEach(function (event) {
          if (Number(event.start_ms) > t || Number(event.end_ms) < t) return;
          if (collectedCoinIds.indexOf(String(event.id)) >= 0) return;
          var screen = worldToMap(event.x, event.y);
          var radius = Math.max(1, Number(event.radius || cfg.coin_radius) * scale);
          if (!isOnMap(screen, radius + 20)) return;
          overviewCtx.save();
          if (event.line && event.line.length >= 2) {
            overviewCtx.beginPath();
            event.line.forEach(function (point, index) {
              var linePoint = worldToMap(point[0], point[1]);
              if (index === 0) overviewCtx.moveTo(linePoint.x, linePoint.y);
              else overviewCtx.lineTo(linePoint.x, linePoint.y);
            });
            overviewCtx.strokeStyle = "rgba(244, 196, 48, 0.85)";
            overviewCtx.lineWidth = 4;
            overviewCtx.stroke();
          }
          overviewCtx.beginPath();
          overviewCtx.arc(screen.x, screen.y, radius, 0, Math.PI * 2);
          overviewCtx.fillStyle = "#f4c430";
          overviewCtx.fill();
          overviewCtx.strokeStyle = "#a27400";
          overviewCtx.lineWidth = 2;
          overviewCtx.stroke();
          overviewCtx.restore();
        });
      }

      function drawHeadingCone(screen, heading) {
        var range = Math.max(72, overviewSize * 0.3);
        var halfAngle = 0.48;
        overviewCtx.save();
        overviewCtx.translate(screen.x, screen.y);
        overviewCtx.rotate(heading);
        overviewCtx.beginPath();
        overviewCtx.moveTo(0, 0);
        overviewCtx.arc(0, 0, range, -halfAngle, halfAngle);
        overviewCtx.closePath();
        overviewCtx.fillStyle = "rgba(211, 211, 211, 0.42)";
        overviewCtx.fill();
        overviewCtx.strokeStyle = "rgba(170, 170, 170, 0.55)";
        overviewCtx.lineWidth = 1;
        overviewCtx.stroke();
        overviewCtx.restore();
      }

      function drawMapPlayers(now) {
        var ownScreen = worldToMap(own.x, own.y);
        drawHeadingCone(ownScreen, own.heading || 0);
        Object.keys(players).forEach(function (id) {
          var isOwn = id === participantId;
          var player = isOwn ? own : renderedPlayer(players[id], now);
          var screen = worldToMap(player.x, player.y);
          if (!isOnMap(screen, cfg.player_radius + 6)) return;
          overviewCtx.beginPath();
          overviewCtx.arc(screen.x, screen.y, cfg.player_radius, 0, Math.PI * 2);
          overviewCtx.fillStyle = player.color || (isOwn ? "#1f77b4" : "#d62728");
          overviewCtx.fill();
          overviewCtx.lineWidth = isOwn ? 4 : 2;
          overviewCtx.strokeStyle = isOwn ? "#111827" : "#ffffff";
          overviewCtx.stroke();
        });
      }

      function drawOverview(t, now) {
        if (isLobby || !overviewCtx) return;
        overviewCtx.clearRect(0, 0, overviewSize, overviewSize);
        overviewCtx.fillStyle = "#f8fbff";
        overviewCtx.fillRect(0, 0, overviewSize, overviewSize);
        if (!gameStarted) return;
        drawMapGrid();
        drawMapZones(t);
        drawMapChaserTracks(t);
        drawMapPotential(t);
        drawMapRewards(t);
        drawMapPlayers(now);
      }

      function renderFrame(t, now) {
        renderer.render(scene, camera);
        drawOverview(t || 0, now || performance.now());
      }

      function tick(now) {
        if (!gameStarted) {
          lastDrawAt = now;
          renderFrame(0, now);
          return;
        }
        integrateOwnPlayer(now);
        var t = isLobby ? 0 : gameTime(now);
        if (!isLobby) {
          checkRewardCollections(t, now);
          updateRewards(t);
        }
        updateCars(now);
        updateCameras();
        renderFrame(t, now);
      }

      function sendPosition() {
        if (!gameStarted) return;
        var now = performance.now();
        if (isLobby) {
          wsSend({
            type: "lobby_position",
            x: own.x,
            y: own.y,
            vx: own.vx,
            vy: own.vy,
            client_time: now,
            low_latency: true,
          });
          return;
        }
        wsSend({
          type: "position",
          x: own.x,
          y: own.y,
          vx: own.vx,
          vy: own.vy,
          client_time: now,
          game_time_ms: gameTime(now),
          low_latency: true,
        });
      }

      function lobbyUniqueId() {
        return String(
          psynet.uniqueId ||
          (window.psynetTemplateData && psynetTemplateData.uniqueId) ||
          ""
        );
      }

      function leaveLobby() {
        if (submitted) return;
        submitted = true;
        cleanup();
        psynet.nextPage();
      }

      function pollLobbyRelease() {
        if (!isLobby || submitted) return;
        var uniqueId = lobbyUniqueId();
        if (!uniqueId) return;
        var params = new URLSearchParams({
          participant_id: participantId,
          unique_id: uniqueId,
        });
        fetch("/lobby/waiting?" + params.toString(), { credentials: "same-origin" })
          .then(function (response) { return response.json(); })
          .then(function (data) {
            if (data && data.status === "success" && data.waiting === false) {
              leaveLobby();
            }
          })
          .catch(function () {});
      }

      function submitFinalAnswer() {
        if (submitted) return;
        submitted = true;
        cleanup();
        psynet.nextPage({
          completed_live_canvas_browser: true,
          client_collected_coin_ids: collectedCoinIds,
          client_coin_bonus: bonus,
          client_final_position: { x: own.x, y: own.y, vx: own.vx, vy: own.vy },
          world_id: cfg.world_id,
        });
      }

      function focusCanvas() {
        if (document.activeElement !== canvas) canvas.focus();
      }

      function updateZoomLabel() {
        if (zoomLabel) zoomLabel.textContent = String(Math.round(viewportMiles));
      }

      function handleZoomChange() {
        viewportMiles = Number(zoomSlider.value || 200);
        updateZoomLabel();
        centerMapCameraOn(own);
      }

      function driveKeyForEvent(event) {
        if (driveKeys[event.key]) return event.key;
        if (driveKeys[event.code]) return event.code;
        return null;
      }

      function isTextInput(target) {
        var tagName = target && target.tagName ? target.tagName.toLowerCase() : "";
        return tagName === "input" || tagName === "textarea" || tagName === "select" ||
          Boolean(target && target.isContentEditable);
      }

      function handleKeyDown(event) {
        if (isTextInput(event.target)) return;
        var key = driveKeyForEvent(event);
        if (!key) return;
        event.preventDefault();
        focusCanvas();
        if (!gameStarted) return;
        keys[key] = true;
      }

      function handleKeyUp(event) {
        var key = driveKeyForEvent(event);
        if (!key) return;
        keys[key] = false;
        event.preventDefault();
        if (gameStarted) sendPosition();
      }

      setupScene();
      updateCars(performance.now());
      updateCameras();
      renderFrame();
      updateWaitingOverlay();
      if (zoomSlider) {
        viewportMiles = Number(zoomSlider.value || 750);
        updateZoomLabel();
        psynet.addPageEventListener(zoomSlider, "input", handleZoomChange);
      }
      psynet.addPageEventListener(canvas, "click", focusCanvas);
      psynet.addPageEventListener(window, "keydown", handleKeyDown, true);
      psynet.addPageEventListener(window, "keyup", handleKeyUp, true);
      psynet.addPageEventListener(window, "blur", function () { keys = {}; });
      psynet.addPageEventListener(window, "resize", layoutRenderer);
      focusCanvas();

      var unsubscribeHandlers = [];
      if (isLobby) {
        unsubscribeHandlers.push(
          psynet.websocket.handle("lobby_position_update", function (msg) {
            if (msg.waiter_ids) syncWaiters(msg.waiter_ids);
            if (msg.player) applyPosition(msg.player);
          })
        );
      } else {
        unsubscribeHandlers.push(
          psynet.session.onFreshState(applySnapshot),
          psynet.session.onEnd(submitFinalAnswer),
          psynet.websocket.handle("position_update", function (msg) {
            applyPosition(msg.player);
          }),
          psynet.websocket.handle("coin_collected", function (msg) {
            applyCollection(msg);
          }),
          psynet.websocket.handle("collect_rejected", function (msg) {
            handleCollectRejected(msg);
          })
        );
        psynet.session.ready();
      }

      var drawHandle = null;
      function loop() {
        tick(performance.now());
        drawHandle = window.requestAnimationFrame(loop);
      }
      drawHandle = window.requestAnimationFrame(loop);
      var sendInterval = setInterval(sendPosition, cfg.send_interval_ms);
      var completionInterval = isLobby ? null : setInterval(function () {
        if (gameEndsAt != null && performance.now() >= gameEndsAt) submitFinalAnswer();
      }, 200);
      var pollInterval = isLobby ? setInterval(pollLobbyRelease, 1000) : null;
      if (isLobby) pollLobbyRelease();

      var cleanedUp = false;
      function cleanup() {
        if (cleanedUp) return;
        cleanedUp = true;
        if (drawHandle != null) window.cancelAnimationFrame(drawHandle);
        clearInterval(sendInterval);
        if (completionInterval != null) clearInterval(completionInterval);
        if (pollInterval != null) clearInterval(pollInterval);
        unsubscribeHandlers.forEach(function (unsubscribe) {
          if (typeof unsubscribe === "function") unsubscribe();
        });
        if (renderer) renderer.dispose();
      }

      psynet.addPageCleanupCallback(cleanup);
    },
  };
})(window);
