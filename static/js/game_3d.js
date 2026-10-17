/* global GameTutorial, THREE, psynet */
(function (root) {
  "use strict";

  var CAR_LENGTH = 18;
  var CAR_WIDTH = 10;
  var CAR_HEIGHT = 5;
  var CAR_ARROW_TARGET_HEIGHT = 22;
  var STEER_RATE = 2.6;
  var ACCELERATION = 900;
  var BRAKE = 1100;
  var COAST_FRICTION = 0.92;
  var WORLD_SCALE = 48;
  var GRID_DIVISIONS = 640;
  var MAX_MAP_STORMS = 1200;
  var OVERVIEW_HEIGHT_RATIO = 0.54;
  var BORDER_MARGIN_MILES = 25;
  var REWARD_CYLINDER_HEIGHT = 42;
  var REWARD_MAP_PULSE_DURATION_MS = 4000;
  var REWARD_MAP_PULSE_PERIOD_MS = 1000;
  var REWARD_MAP_PULSE_RADIUS_MULTIPLIER = 4;
  var COIN_SOUND_DURATION_MS = 3981;
  var COIN_SOUND_URL = "/static/coin_fall.wav";
  var TUTORIAL_ARROW_BOB_PERIOD_MS = 1600;
  var TUTORIAL_ARROW_BOB_WORLD_AMPLITUDE = 4;
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
    return "rgba(255, 204, 51, 0.26)";
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
    var bodyMaterial = new THREE.MeshStandardMaterial({
      color: color,
      roughness: 0.34,
      metalness: 0.32,
    });
    var glassMaterial = new THREE.MeshStandardMaterial({
      color: 0x17283a,
      roughness: 0.18,
      metalness: 0.25,
    });
    var tireMaterial = new THREE.MeshStandardMaterial({
      color: 0x111827,
      roughness: 0.9,
    });
    var metalMaterial = new THREE.MeshStandardMaterial({
      color: 0xaeb8c4,
      roughness: 0.3,
      metalness: 0.75,
    });
    var headlightMaterial = new THREE.MeshStandardMaterial({
      color: 0xfff4c2,
      emissive: 0xffd86b,
      emissiveIntensity: 0.65,
    });
    var tailLightMaterial = new THREE.MeshStandardMaterial({
      color: 0xc81e1e,
      emissive: 0x7f1010,
      emissiveIntensity: 0.55,
    });
    var beaconMaterial = new THREE.MeshStandardMaterial({
      color: 0xffb000,
      emissive: 0xff7a00,
      emissiveIntensity: 0.55,
    });

    function addBox(width, height, depth, x, y, z, material) {
      var mesh = new THREE.Mesh(
        new THREE.BoxGeometry(width * scale, height * scale, depth * scale),
        material
      );
      mesh.position.set(x * scale, y * scale, z * scale);
      group.add(mesh);
      return mesh;
    }

    var wheelRadius = 2.35;
    var wheelY = wheelRadius;
    addBox(CAR_LENGTH, CAR_HEIGHT * 0.72, CAR_WIDTH, 0, 4.1, 0, bodyMaterial);
    addBox(CAR_LENGTH * 0.34, CAR_HEIGHT * 0.34, CAR_WIDTH * 0.92, 5.7, 6.3, 0, bodyMaterial);
    addBox(CAR_LENGTH * 0.24, CAR_HEIGHT * 0.32, CAR_WIDTH * 0.94, -6.8, 6.2, 0, bodyMaterial);

    // Colored cabin shell with inset windows leaves a visible roof and pillars.
    addBox(CAR_LENGTH * 0.47, CAR_HEIGHT * 0.92, CAR_WIDTH * 0.82, -0.5, 8.2, 0, bodyMaterial);
    addBox(CAR_LENGTH * 0.35, 0.55, CAR_WIDTH * 0.72, -0.5, 10.75, 0, bodyMaterial);
    addBox(0.18, 2.65, CAR_WIDTH * 0.58, 3.8, 8.25, 0, glassMaterial);
    addBox(0.18, 2.65, CAR_WIDTH * 0.58, -4.8, 8.25, 0, glassMaterial);
    addBox(CAR_LENGTH * 0.34, 2.65, 0.18, -0.5, 8.25, CAR_WIDTH * 0.415, glassMaterial);
    addBox(CAR_LENGTH * 0.34, 2.65, 0.18, -0.5, 8.25, -CAR_WIDTH * 0.415, glassMaterial);
    addBox(0.45, 3.0, CAR_WIDTH * 0.86, -0.5, 8.25, 0, bodyMaterial);

    [-5.8, 5.8].forEach(function (x) {
      [-CAR_WIDTH * 0.53, CAR_WIDTH * 0.53].forEach(function (z) {
        var wheel = new THREE.Mesh(
          new THREE.CylinderGeometry(
            wheelRadius * scale,
            wheelRadius * scale,
            1.45 * scale,
            20
          ),
          tireMaterial
        );
        wheel.rotation.x = Math.PI / 2;
        wheel.position.set(x * scale, wheelY * scale, z * scale);
        group.add(wheel);
        var hub = new THREE.Mesh(
          new THREE.CylinderGeometry(
            wheelRadius * 0.46 * scale,
            wheelRadius * 0.46 * scale,
            1.58 * scale,
            16
          ),
          metalMaterial
        );
        hub.rotation.x = Math.PI / 2;
        hub.position.copy(wheel.position);
        group.add(hub);
      });
    });

    [-3.15, 3.15].forEach(function (z) {
      addBox(0.28, 1.25, 2.15, CAR_LENGTH * 0.51, 4.7, z, headlightMaterial);
      addBox(0.28, 1.25, 2.15, -CAR_LENGTH * 0.51, 4.7, z, tailLightMaterial);
    });
    addBox(0.4, 0.7, CAR_WIDTH * 0.82, CAR_LENGTH * 0.51, 2.9, 0, metalMaterial);
    addBox(0.4, 0.7, CAR_WIDTH * 0.82, -CAR_LENGTH * 0.51, 2.9, 0, metalMaterial);
    addBox(0.3, 1.15, 2.5, -CAR_LENGTH * 0.52, 4.1, 0, metalMaterial);

    // A roof-mounted amber light bar gives the storm-chaser car a clear silhouette.
    addBox(1.8, 0.28, 5.8, -0.5, 11.15, 0, tireMaterial);
    addBox(1.55, 0.58, 5.35, -0.5, 11.55, 0, beaconMaterial);
    return group;
  }

  function headingFromVelocity(vx, vy, fallback) {
    if (Math.hypot(vx, vy) < 0.01) return fallback || 0;
    return Math.atan2(vy, vx);
  }

  root.Game3D = {
    start: function startGame3D(cfg) {
      var isLobby = cfg.mode === "lobby";
      var isTutorial = cfg.mode === "tutorial";
      var experimentCondition = String(cfg.experiment_condition || "live");
      var usesReplayCars = !isLobby && !isTutorial && experimentCondition === "social";
      var usesLivePeers = !isLobby && !isTutorial && experimentCondition === "live";
      var participantId = String(
        cfg.participant_id ||
        (psynet.session && psynet.session.participant_id) ||
        ""
      );
      var canvas = document.getElementById("game-canvas");
      var waitingOverlay = document.getElementById("waiting-overlay");
      var overviewCanvas = document.getElementById("overview-map");
      var overviewCtx = overviewCanvas ? overviewCanvas.getContext("2d") : null;
      var zoomSlider = document.getElementById("zoom-slider");
      var zoomLabel = document.getElementById("zoom-label");
      var bonusPill = document.querySelector(".bonus-pill");
      var gameTimeLeft = document.getElementById("game-time-left");
      var speedGauge = document.getElementById("speed-gauge");
      var speedGaugeFill = document.getElementById("speed-gauge-fill");
      var speedGaugeValue = document.getElementById("speed-gauge-value");
      var canvasWidth = Number(cfg.canvas_width || 960);
      var canvasHeight = Number(cfg.canvas_height || 540);
      var canvasSize = Number(cfg.canvas_size || 640);
      var pxPerMile = Number((cfg.projection || {}).px_per_mile || 1);
      var viewportMiles = zoomSlider
        ? Number(zoomSlider.value || 750)
        : (isTutorial ? 40 : 750);
      var initialPlayers = cfg.initial_players || {};
      var players = {};
      var replayPlayers = {};
      var stormPoints = (cfg.storm_points || []).slice();
      var stormCursor = 0;
      var activePotential = [];
      var lastPotentialTime = 0;
      var chaserTracks = usesReplayCars ? (cfg.chaser_tracks || []).slice() : [];
      var warnings = cfg.warnings || [];
      var rewardEvents = cfg.reward_events || [];
      var timing = cfg.timing || {};
      var tutorialCfg = cfg.tutorial || {};
      var tutorial = null;
      var tutorialLastMovementPosition = null;
      var tutorialMovementDistance = 0;
      var tutorialArrow = null;
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
      tutorialLastMovementPosition = { x: own.x, y: own.y };
      var bonus = 0;
      var displayedBonus = 0;
      var bonusAnimationTimer = null;
      var bonusAnimationTargetCents = 0;
      var coinFallAudio = null;
      var lastDrawAt = performance.now();
      var submitted = false;
      var gameStarted = isLobby || isTutorial;
      var renderer = null;
      var scene = null;
      var camera = null;
      var mapCamera = { x: 0, y: 0, initialized: false };
      var overviewSize = 540;
      var carMeshes = {};
      var rewardMeshes = [];
      var zoneMeshes = [];
      var tutorialFocusStep = null;
      var tutorialFocusObserving = null;
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

      if (isTutorial) {
        if (!root.GameTutorial) {
          throw new Error("Tutorial controller failed to load.");
        }
        tutorial = root.GameTutorial.create({
          bonus: cfg.coin_bonus,
          canvas: canvas,
          condition: experimentCondition,
          config: tutorialCfg,
          onComplete: submitTutorialAnswer,
          onFocusChange: updateTutorialSceneFocus,
          onStepChanged: function (step, now) {
            tutorialLastMovementPosition = { x: own.x, y: own.y };
            tutorialMovementDistance = 0;
            own.speed = 0;
            own.vx = 0;
            own.vy = 0;
            own.client_time = now;
            own.received_at = now;
            players[participantId] = Object.assign({}, own);
            keys = {};
            lastDrawAt = now;
            placeTutorialTarget(step);
          },
        });
      }

      function currentTutorialStep() {
        return tutorial ? tutorial.currentStep() : null;
      }

      function isTutorialObserving(now) {
        return Boolean(tutorial && tutorial.isObserving(now));
      }

      function tutorialCueForObject(object) {
        var current = object;
        while (current) {
          if (current.userData && current.userData.tutorialCue) {
            return current.userData.tutorialCue;
          }
          current = current.parent;
        }
        return null;
      }

      function setMaterialMuted(material, muted, whiteWhenMuted) {
        if (!material) return;
        material.userData = material.userData || {};
        if (material.color) {
          if (material.userData.tutorialOriginalColor == null) {
            material.userData.tutorialOriginalColor = material.color.getHex();
          }
          if (muted) {
            if (whiteWhenMuted) {
              material.color.setHex(0xffffff);
              return;
            }
            var originalColor = new THREE.Color(
              material.userData.tutorialOriginalColor
            );
            var luminance =
              originalColor.r * 0.2126 +
              originalColor.g * 0.7152 +
              originalColor.b * 0.0722;
            material.color.setRGB(
              luminance * 0.68,
              luminance * 0.68,
              luminance * 0.68
            );
          } else {
            material.color.setHex(material.userData.tutorialOriginalColor);
          }
        }
        if (material.emissive) {
          if (material.userData.tutorialOriginalEmissive == null) {
            material.userData.tutorialOriginalEmissive =
              material.emissive.getHex();
          }
          if (muted) {
            var originalEmissive = new THREE.Color(
              material.userData.tutorialOriginalEmissive
            );
            var emissiveLuminance =
              originalEmissive.r * 0.2126 +
              originalEmissive.g * 0.7152 +
              originalEmissive.b * 0.0722;
            material.emissive.setRGB(
              emissiveLuminance * 0.3,
              emissiveLuminance * 0.3,
              emissiveLuminance * 0.3
            );
          } else {
            material.emissive.setHex(
              material.userData.tutorialOriginalEmissive
            );
          }
        }
      }

      function setVertexColorsWhiteWhenMuted(object, muted) {
        if (!object.userData.tutorialWhiteWhenMuted || !object.geometry) return;
        var colorAttribute = object.geometry.getAttribute("color");
        if (!colorAttribute) return;
        if (!object.userData.tutorialOriginalVertexColors) {
          object.userData.tutorialOriginalVertexColors =
            colorAttribute.array.slice();
        }
        if (muted) {
          colorAttribute.array.fill(1);
        } else {
          colorAttribute.array.set(
            object.userData.tutorialOriginalVertexColors
          );
        }
        colorAttribute.needsUpdate = true;
      }

      function updateTutorialSceneFocus(step, observing) {
        if (!scene) return;
        canvas.dataset.tutorialFocus = observing ? String(step) : "none";
        if (
          tutorialFocusStep === step &&
          tutorialFocusObserving === observing
        ) {
          return;
        }
        tutorialFocusStep = step;
        tutorialFocusObserving = observing;
        scene.traverse(function (object) {
          setVertexColorsWhiteWhenMuted(object, observing);
          if (!object.material) return;
          var cue = tutorialCueForObject(object);
          var keepColor =
            cue === "always" ||
            cue === step;
          var materials = Array.isArray(object.material)
            ? object.material
            : [object.material];
          materials.forEach(function (material) {
            setMaterialMuted(
              material,
              observing && !keepColor,
              Boolean(object.userData.tutorialWhiteWhenMuted)
            );
          });
        });
        var backgroundColor = observing ? 0x777777 : 0x6f90b0;
        if (scene.background) scene.background.setHex(backgroundColor);
        if (scene.fog && scene.fog.color) {
          scene.fog.color.setHex(backgroundColor);
        }
      }

      function submitTutorialAnswer(now) {
        if (submitted) return;
        submitted = true;
        stopLoops();
        psynet.nextPage({
          completed_tutorial: true,
          experiment_condition: experimentCondition,
          participant_id: Number(participantId),
          completed_steps: tutorial.completedSteps(),
          step_elapsed_ms: tutorial.stepElapsedMs(),
          final_position: { x: own.x, y: own.y, vx: own.vx, vy: own.vy },
          completed_at_ms: Math.round(now),
        });
      }

      function advanceTutorial(now) {
        if (!tutorial || submitted) return;
        tutorial.advance(now);
      }

      function tutorialTargetFromCurrentPosition(distance) {
        var margin = Math.max(20, Number(cfg.player_radius || 12));
        var heading = Number.isFinite(own.heading) ? own.heading : 0;
        var directions = [
          heading,
          heading + Math.PI / 2,
          heading - Math.PI / 2,
          heading + Math.PI,
        ];
        for (var i = 0; i < directions.length; i++) {
          var candidate = {
            x: own.x + Math.cos(directions[i]) * distance,
            y: own.y + Math.sin(directions[i]) * distance,
          };
          if (
            candidate.x >= margin &&
            candidate.x <= canvasSize - margin &&
            candidate.y >= margin &&
            candidate.y <= canvasSize - margin
          ) {
            return candidate;
          }
        }
        return {
          x: clamp(
            own.x + Math.cos(heading) * distance,
            margin,
            canvasSize - margin
          ),
          y: clamp(
            own.y + Math.sin(heading) * distance,
            margin,
            canvasSize - margin
          ),
        };
      }

      function moveTutorialReward(target) {
        var reward = rewardEvents.find(function (event) {
          return String(event.id) === String(tutorialCfg.reward_id);
        });
        if (!reward) return;
        reward.x = target.x;
        reward.y = target.y;
        rewardMeshes.forEach(function (mesh) {
          if (String(mesh.userData.id) !== String(reward.id)) return;
          mesh.userData.x = target.x;
          mesh.userData.y = target.y;
          mesh.position.set(worldX(target.x), 0, worldZ(target.y));
        });
      }

      function moveTutorialSighting(target) {
        tutorialCfg.airplane_sighting_hint =
          Object.assign(tutorialCfg.airplane_sighting_hint || {}, target);
        if (stormPoints.length) {
          stormPoints[0][2] = target.x;
          stormPoints[0][3] = target.y;
        }
      }

      function moveTutorialZone(target, highPriority) {
        var cue = highPriority
          ? "landing_zone_high"
          : "landing_zone_medium";
        var zone = warnings.find(function (warning) {
          return zoneIsHighPriority(warning) === highPriority;
        });
        if (!zone || !(zone.polygons || []).length) return;
        var polygon = zone.polygons[0];
        var center = polygon.reduce(function (sum, point) {
          return {
            x: sum.x + Number(point[0]) / polygon.length,
            y: sum.y + Number(point[1]) / polygon.length,
          };
        }, { x: 0, y: 0 });
        var dx = target.x - center.x;
        var dy = target.y - center.y;
        polygon.forEach(function (point) {
          point[0] = Number(point[0]) + dx;
          point[1] = Number(point[1]) + dy;
        });
        zoneMeshes.forEach(function (mesh) {
          if (mesh.userData.tutorialCue !== cue) return;
          var originalCenter = mesh.userData.tutorialOriginalCenter;
          mesh.position.set(
            worldX(target.x - originalCenter.x),
            0,
            worldZ(target.y - originalCenter.y)
          );
        });
        var hintKey = highPriority
          ? "high_zone_hint"
          : "medium_zone_hint";
        tutorialCfg[hintKey] = Object.assign(
          tutorialCfg[hintKey] || {},
          target
        );
      }

      function placeTutorialTarget(step) {
        if (step === "airdrop") {
          moveTutorialReward(tutorialTargetFromCurrentPosition(
            Number(tutorialCfg.reward_target_distance || 8)
          ));
        } else if (step === "airplane_sighting") {
          moveTutorialSighting(tutorialTargetFromCurrentPosition(
            Number(tutorialCfg.sighting_target_distance || 12)
          ));
        } else if (step === "landing_zone_medium") {
          moveTutorialZone(tutorialTargetFromCurrentPosition(
            Number(tutorialCfg.zone_target_distance || 14)
          ), false);
        } else if (step === "landing_zone_high") {
          moveTutorialZone(tutorialTargetFromCurrentPosition(
            Number(tutorialCfg.zone_target_distance || 14)
          ), true);
        }
      }

      function gameTime(now) {
        if (serverStartedAt == null) return 0;
        return Math.min(cfg.trial_seconds * 1000, Math.max(0, now - serverStartedAt));
      }

      function updateCountdown(now) {
        if (!gameTimeLeft || isTutorial) return;
        var remainingSeconds = gameEndsAt == null
          ? Math.max(0, Math.ceil(Number(cfg.trial_seconds || 0)))
          : Math.max(0, Math.ceil((gameEndsAt - now) / 1000));
        var minutes = Math.floor(remainingSeconds / 60);
        var seconds = remainingSeconds % 60;
        gameTimeLeft.textContent =
          String(minutes).padStart(2, "0") +
          ":" +
          String(seconds).padStart(2, "0");
        gameTimeLeft.dateTime = "PT" + String(remainingSeconds) + "S";
      }

      function updateSpeedGauge() {
        if (!speedGauge) return;
        var maxSpeed = Number(cfg.max_player_speed || 0);
        var speedLimitMph = Number(cfg.speed_limit_mph || 60);
        var speedRatio = maxSpeed > 0
          ? Math.min(1, Math.abs(Number(own.speed) || 0) / maxSpeed)
          : 0;
        var speedMph = Math.round(speedRatio * speedLimitMph);
        speedGauge.setAttribute("aria-valuemax", String(speedLimitMph));
        speedGauge.setAttribute("aria-valuenow", String(speedMph));
        if (speedGaugeFill) {
          speedGaugeFill.style.width = String(Math.round(speedRatio * 100)) + "%";
        }
        if (speedGaugeValue) {
          speedGaugeValue.textContent = String(speedMph) + " mph";
        }
      }

      function updateWaitingOverlay(readyIds) {
        if (!waitingOverlay) return;
        if (isTutorial) {
          waitingOverlay.classList.add("hidden");
          return;
        }
        if (isLobby) {
          waitingOverlay.classList.remove("hidden");
          waitingOverlay.textContent = experimentCondition === "live"
            ? "Waiting for other players"
            : "Preparing your airdrop session...";
          return;
        }
        waitingOverlay.classList.toggle("hidden", gameStarted);
        if (!gameStarted) {
          var readyCount = Array.isArray(readyIds) ? readyIds.length : 0;
          if (usesLivePeers) {
            waitingOverlay.textContent = readyCount > 0
              ? "Waiting for other participants... (" + readyCount + " ready)"
              : "Waiting for other participants...";
          } else {
            waitingOverlay.textContent = "Starting your airdrop session...";
          }
        }
      }

      function applyServerStartTime(serverStartRaw, now, allowLargeJump) {
        if (!serverStartRaw) return;
        if (serverStartedAt != null && !allowLargeJump) return;
        var serverStartMs = Date.parse(serverStartRaw);
        var trialMs = Number(cfg.trial_seconds || 0) * 1000;
        if (Number.isFinite(serverStartMs)) {
          var elapsed = Math.max(0, Date.now() - serverStartMs);
          if (trialMs > 0 && elapsed > trialMs + 2000) {
            serverStartedAt = now;
          } else {
            serverStartedAt = now - elapsed;
          }
        } else {
          serverStartedAt = now;
        }
        gameStarted = true;
        gameEndsAt = serverStartedAt + trialMs;
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
        if (submitted || isTutorial) return;
        var type = payload.type;
        if (!type) return;
        var message = Object.assign({}, payload);
        delete message.type;
        psynet.websocket.send(type, message);
      }

      function renderBonus(value) {
        displayedBonus = Number(value || 0);
        var bonusEl = document.getElementById("coin-bonus");
        if (bonusEl) bonusEl.textContent = displayedBonus.toFixed(2);
        var performanceReward = document.getElementById("performance-reward");
        var totalReward = document.getElementById("total-reward");
        var rewardDetails = document.getElementById("reward-details");
        if (performanceReward) performanceReward.textContent = displayedBonus.toFixed(2);
        if (totalReward) {
          var timeRewardEl = document.getElementById("time-reward");
          var timeReward = timeRewardEl ? Number(timeRewardEl.textContent || 0) : 0;
          totalReward.textContent = (timeReward + displayedBonus).toFixed(2);
        }
        if (rewardDetails) rewardDetails.style.display = "";
      }

      function stopBonusAnimation() {
        if (bonusAnimationTimer != null) {
          clearTimeout(bonusAnimationTimer);
          bonusAnimationTimer = null;
        }
        if (bonusPill) bonusPill.classList.remove("reward-collecting");
      }

      function updateBonus(value) {
        var nextBonus = Number(value || 0);
        var nextCents = Math.round(nextBonus * 100);
        bonus = nextBonus;
        if (
          bonusAnimationTimer != null &&
          nextCents === bonusAnimationTargetCents
        ) {
          return;
        }
        stopBonusAnimation();
        bonusAnimationTargetCents = nextCents;
        renderBonus(nextCents / 100);
      }

      function animateBonus(value, onComplete) {
        var nextBonus = Number(value || 0);
        var currentCents = Math.round(displayedBonus * 100);
        var targetCents = Math.round(nextBonus * 100);
        bonus = nextBonus;
        stopBonusAnimation();
        bonusAnimationTargetCents = targetCents;
        if (targetCents <= currentCents) {
          renderBonus(targetCents / 100);
          if (onComplete) onComplete();
          return;
        }
        if (bonusPill) bonusPill.classList.add("reward-collecting");
        var soundDurationMs =
          coinFallAudio && Number.isFinite(coinFallAudio.duration)
            ? coinFallAudio.duration * 1000
            : COIN_SOUND_DURATION_MS;
        var stepDelay = soundDurationMs / (targetCents - currentCents);
        function incrementDisplayedBonus() {
          currentCents += 1;
          renderBonus(currentCents / 100);
          if (currentCents < targetCents) {
            bonusAnimationTimer = setTimeout(
              incrementDisplayedBonus,
              stepDelay
            );
          } else {
            bonusAnimationTimer = null;
            if (bonusPill) bonusPill.classList.remove("reward-collecting");
            if (onComplete) onComplete();
          }
        }
        bonusAnimationTimer = setTimeout(incrementDisplayedBonus, stepDelay);
      }

      function ensureCoinFallAudio() {
        if (!coinFallAudio) {
          coinFallAudio = new Audio(COIN_SOUND_URL);
          coinFallAudio.preload = "auto";
        }
        return coinFallAudio;
      }

      function playCoinFallSound() {
        var audio = ensureCoinFallAudio();
        audio.pause();
        audio.currentTime = 0;
        var playback = audio.play();
        if (playback && typeof playback.catch === "function") {
          playback.catch(function () {});
        }
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
          var bonuses = msg.bonuses || {};
          var nextBonus = bonuses[participantId] != null
            ? bonuses[participantId]
            : bonus + Number(cfg.coin_bonus || 0);
          playCoinFallSound();
          animateBonus(nextBonus);
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
        if (isTutorialObserving(now)) {
          own.speed = 0;
          own.vx = 0;
          own.vy = 0;
          keys = {};
          players[participantId] = Object.assign({}, own);
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

      function headingFromTrackPoint(value, fallback) {
        if (value == null || value === "") return fallback || 0;
        var degrees = Number(value);
        if (!Number.isFinite(degrees)) return fallback || 0;
        return (degrees - 90) * Math.PI / 180;
      }

      function replayPlayerAt(track, trackIndex, t, now) {
        var points = track.points || [];
        if (points.length < 2) return null;
        if (t < Number(points[0][0]) || t > Number(points[points.length - 1][0])) {
          return null;
        }

        var low = 0;
        var high = points.length - 1;
        while (low < high - 1) {
          var mid = Math.floor((low + high) / 2);
          if (Number(points[mid][0]) <= t) low = mid;
          else high = mid;
        }
        var pointA = points[low];
        var pointB = points[Math.min(low + 1, points.length - 1)];
        if (Number(pointB[0]) < t && low + 2 < points.length) {
          pointB = points[low + 2];
        }
        var duration = Math.max(1, Number(pointB[0]) - Number(pointA[0]));
        var amount = clamp((t - Number(pointA[0])) / duration, 0, 1);
        var x = Number(pointA[1]) + (Number(pointB[1]) - Number(pointA[1])) * amount;
        var y = Number(pointA[2]) + (Number(pointB[2]) - Number(pointA[2])) * amount;
        var vx = (Number(pointB[1]) - Number(pointA[1])) / (duration / 1000);
        var vy = (Number(pointB[2]) - Number(pointA[2])) / (duration / 1000);
        var fallbackHeading = headingFromTrackPoint(
          pointA[4] != null ? pointA[4] : pointB[4],
          0
        );
        return {
          participant_id: "replay:" + String(track.id || trackIndex),
          label: "Recorded driver " + String(trackIndex + 1),
          color: CHASER_TRACK_COLORS[trackIndex % CHASER_TRACK_COLORS.length],
          x: x,
          y: y,
          vx: vx,
          vy: vy,
          heading: headingFromVelocity(vx, vy, fallbackHeading),
          received_at: now,
        };
      }

      function updateReplayPlayers(t, now) {
        var nextPlayers = {};
        if (usesReplayCars) {
          chaserTracks.forEach(function (track, trackIndex) {
            var player = replayPlayerAt(track, trackIndex, t, now);
            if (player) nextPlayers[player.participant_id] = player;
          });
        }
        Object.keys(replayPlayers).forEach(function (id) {
          if (nextPlayers[id] || !carMeshes[id]) return;
          scene.remove(carMeshes[id]);
          delete carMeshes[id];
        });
        replayPlayers = nextPlayers;
      }

      function updateTutorialDemoCar(now) {
        if (!isTutorial) return;
        var demo = tutorialCfg.demo_car || {};
        var id = String(demo.participant_id || "tutorial:other-car");
        if (currentTutorialStep() !== "other_car") {
          canvas.dataset.tutorialDemoCarVisible = "false";
          delete replayPlayers[id];
          if (carMeshes[id]) {
            scene.remove(carMeshes[id]);
            delete carMeshes[id];
          }
          return;
        }
        var radius = Number(demo.orbit_radius || 3.5);
        var periodMs = Math.max(1000, Number(demo.orbit_seconds || 8) * 1000);
        var angle = tutorial.elapsed(now) / periodMs * Math.PI * 2;
        var angularSpeed = Math.PI * 2 / (periodMs / 1000);
        var vx = -Math.sin(angle) * radius * angularSpeed;
        var vy = Math.cos(angle) * radius * angularSpeed;
        replayPlayers[id] = {
          participant_id: id,
          label: demo.label || "Another driver",
          color: demo.color || "#d62728",
          x: own.x + Math.cos(angle) * radius,
          y: own.y + Math.sin(angle) * radius,
          vx: vx,
          vy: vy,
          heading: headingFromVelocity(vx, vy, 0),
          received_at: now,
        };
        canvas.dataset.tutorialDemoCarVisible = "true";
      }

      function tutorialArrowTarget() {
        var step = currentTutorialStep();
        if (step === "movement") {
          return {
            x: own.x + Math.cos(own.heading) * 4,
            y: own.y + Math.sin(own.heading) * 4,
          };
        }
        if (step === "airdrop") {
          return rewardEvents.find(function (event) {
            return String(event.id) === String(tutorialCfg.reward_id);
          }) || null;
        }
        if (step === "airplane_sighting") {
          return tutorialCfg.airplane_sighting_hint || null;
        }
        if (step === "landing_zone_medium") {
          return tutorialCfg.medium_zone_hint || null;
        }
        if (step === "landing_zone_high") {
          return tutorialCfg.high_zone_hint || null;
        }
        if (step === "other_car") {
          var demoId = String((tutorialCfg.demo_car || {}).participant_id || "tutorial:other-car");
          return replayPlayers[demoId] || null;
        }
        return null;
      }

      function tutorialArrowBob(now, amplitude) {
        var omega = Math.PI * 2 / TUTORIAL_ARROW_BOB_PERIOD_MS;
        return Math.sin(omega * now) * amplitude;
      }

      function updateTutorialArrow(now) {
        if (!tutorialArrow) return;
        var target = tutorialArrowTarget();
        if (!target) {
          tutorialArrow.visible = false;
          canvas.dataset.tutorialArrowVisible = "false";
          return;
        }
        var targetHeight =
          currentTutorialStep() === "other_car" ? CAR_ARROW_TARGET_HEIGHT : 4;
        var verticalShift = tutorialArrowBob(
          now == null ? performance.now() : now,
          TUTORIAL_ARROW_BOB_WORLD_AMPLITUDE
        );
        var targetVector = new THREE.Vector3(
          worldX(target.x),
          targetHeight + verticalShift,
          worldZ(target.y)
        );
        var origin = new THREE.Vector3(
          targetVector.x,
          64 + verticalShift,
          targetVector.z
        );
        var direction = targetVector.clone().sub(origin);
        var distance = direction.length();
        if (distance < 0.001) {
          tutorialArrow.visible = false;
          canvas.dataset.tutorialArrowVisible = "false";
          return;
        }
        direction.normalize();
        tutorialArrow.position.copy(origin);
        tutorialArrow.setDirection(direction);
        tutorialArrow.setLength(distance, 18, 10);
        tutorialArrow.visible = true;
        canvas.dataset.tutorialArrowVisible = "true";
      }

      function reachedTutorialHint(hint, radius) {
        if (!hint) return false;
        var x = Number(hint.x);
        var y = Number(hint.y);
        if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
        return Math.hypot(own.x - x, own.y - y) <= radius;
      }

      function pointInPolygon(x, y, polygon) {
        var inside = false;
        for (var i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
          var xi = Number(polygon[i][0]);
          var yi = Number(polygon[i][1]);
          var xj = Number(polygon[j][0]);
          var yj = Number(polygon[j][1]);
          var crosses = (yi > y) !== (yj > y) &&
            x < (xj - xi) * (y - yi) / (yj - yi) + xi;
          if (crosses) inside = !inside;
        }
        return inside;
      }

      function reachedTutorialZone(highPriority) {
        return warnings.some(function (warning) {
          var isHighPriority =
            String(warning.event || "").toLowerCase().indexOf("tornado") >= 0;
          if (isHighPriority !== highPriority) return false;
          return (warning.polygons || []).some(function (polygon) {
            return polygon.length >= 3 && pointInPolygon(own.x, own.y, polygon);
          });
        });
      }

      function updateTutorial(now) {
        if (!isTutorial || submitted) return;
        var step = currentTutorialStep();
        var tutorialAction = tutorial.update(now);
        updateTutorialDemoCar(now);
        updateTutorialArrow(now);
        if (tutorialAction === "advance") {
          advanceTutorial(now);
          return;
        }
        if (isTutorialObserving(now)) return;
        if (step === "movement") {
          tutorialMovementDistance += Math.hypot(
            own.x - tutorialLastMovementPosition.x,
            own.y - tutorialLastMovementPosition.y
          );
          tutorialLastMovementPosition = { x: own.x, y: own.y };
          var movementPracticeElapsed =
            tutorial.elapsed(now) -
            Number(tutorialCfg.intro_duration_ms || 10000);
          if (
            movementPracticeElapsed >=
              Number(tutorialCfg.movement_min_duration_ms || 5000) &&
            tutorialMovementDistance >=
              Number(tutorialCfg.movement_threshold || 1.5)
          ) {
            advanceTutorial(now);
          }
        } else if (step === "airdrop") {
          var reward = rewardEvents.find(function (event) {
            return String(event.id) === String(tutorialCfg.reward_id);
          });
          if (
            reward &&
            collectedCoinIds.indexOf(String(reward.id)) < 0 &&
            distanceToRewardEvent(reward) <= Number(reward.radius || cfg.coin_radius)
          ) {
            collectedCoinIds.push(String(reward.id));
            playCoinFallSound();
            animateBonus(Number(cfg.coin_bonus || 0), function () {
              advanceTutorial(performance.now());
            });
          }
        } else if (
          step === "airplane_sighting" &&
          reachedTutorialHint(
            tutorialCfg.airplane_sighting_hint,
            Number(tutorialCfg.sighting_reach_radius || 3)
          )
        ) {
          advanceTutorial(now);
        } else if (
          step === "landing_zone_medium" &&
          reachedTutorialZone(false)
        ) {
          advanceTutorial(now);
        } else if (
          step === "landing_zone_high" &&
          reachedTutorialZone(true)
        ) {
          advanceTutorial(now);
        }
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
        if (isTutorial) return;
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

      function zoneIsHighPriority(zone) {
        return String(zone.event || "").toLowerCase().indexOf("tornado") >= 0;
      }

      function createZoneMesh(zone, polygon) {
        var highPriority = zoneIsHighPriority(zone);
        var color = highPriority ? 0xe41a1c : 0xffcc33;
        var cue = highPriority
          ? "landing_zone_high"
          : "landing_zone_medium";
        var shape = new THREE.Shape();
        polygon.forEach(function (point, index) {
          var x = worldX(point[0]);
          var y = -worldZ(point[1]);
          if (index === 0) shape.moveTo(x, y);
          else shape.lineTo(x, y);
        });
        shape.closePath();

        var polygonCenter = polygon.reduce(function (sum, point) {
          return {
            x: sum.x + Number(point[0]) / polygon.length,
            y: sum.y + Number(point[1]) / polygon.length,
          };
        }, { x: 0, y: 0 });
        var group = new THREE.Group();
        group.userData = {
          zone: zone,
          tutorialCue: cue,
          tutorialOriginalCenter: polygonCenter,
        };
        var fill = new THREE.Mesh(
          new THREE.ShapeGeometry(shape),
          new THREE.MeshBasicMaterial({
            color: color,
            transparent: true,
            opacity: 0.16,
            depthWrite: false,
            side: THREE.DoubleSide,
            polygonOffset: true,
            polygonOffsetFactor: -2,
            polygonOffsetUnits: -2,
          })
        );
        fill.rotation.x = -Math.PI / 2;
        fill.position.y = 0.12;
        group.add(fill);

        var outlinePoints = polygon.map(function (point) {
          return new THREE.Vector3(worldX(point[0]), 0.18, worldZ(point[1]));
        });
        if (outlinePoints.length) outlinePoints.push(outlinePoints[0].clone());
        group.add(new THREE.Line(
          new THREE.BufferGeometry().setFromPoints(outlinePoints),
          new THREE.LineBasicMaterial({
            color: color,
            transparent: true,
            opacity: 0.82,
            depthWrite: false,
          })
        ));
        group.visible = false;
        return group;
      }

      function createRewardMesh(event) {
        var group = new THREE.Group();
        var radius = Math.max(0.5, Number(event.radius || cfg.coin_radius)) * WORLD_SCALE;
        var cylinderHeight = REWARD_CYLINDER_HEIGHT;
        var rewardArea = new THREE.Mesh(
          new THREE.CylinderGeometry(radius, radius, cylinderHeight, 48, 1, true),
          new THREE.MeshBasicMaterial({
            color: 0xf4c430,
            transparent: true,
            opacity: 0.1,
            depthWrite: false,
            side: THREE.DoubleSide,
          })
        );
        rewardArea.position.y = cylinderHeight / 2;
        group.add(rewardArea);
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
        group.userData = Object.assign({}, event, {
          tutorialCue: "airdrop",
        });
        return group;
      }

      function ensureCarMesh(id, player, isOwn) {
        if (carMeshes[id]) return carMeshes[id];
        var mesh = makeCar(hexToInt(player.color, isOwn ? 0x1f77b4 : 0xd62728), isOwn ? 1 : 0.9);
        if (isTutorial) {
          mesh.userData.tutorialCue = isOwn ? "movement" : "other_car";
        }
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

        var grid = new THREE.GridHelper(
          worldSize,
          GRID_DIVISIONS,
          0x7ea35f,
          0x5d8a45
        );
        grid.userData.tutorialWhiteWhenMuted = true;
        grid.position.set(worldSize / 2, 0.05, worldSize / 2);
        scene.add(grid);

        if (!isLobby) {
          rewardEvents.forEach(function (event) {
            var mesh = createRewardMesh(event);
            rewardMeshes.push(mesh);
            scene.add(mesh);
          });
          warnings.forEach(function (zone) {
            (zone.polygons || []).forEach(function (polygon) {
              if (polygon.length < 3) return;
              var mesh = createZoneMesh(zone, polygon);
              zoneMeshes.push(mesh);
              scene.add(mesh);
            });
          });
        }
        if (isTutorial) {
          tutorialArrow = new THREE.ArrowHelper(
            new THREE.Vector3(1, 0, 0),
            new THREE.Vector3(0, 18, 0),
            70,
            0xfff04a,
            18,
            10
          );
          tutorialArrow.userData.tutorialCue = "always";
          tutorialArrow.visible = false;
          scene.add(tutorialArrow);
        }

        layoutRenderer();
      }

      function updateRewards(t) {
        rewardMeshes.forEach(function (mesh) {
          var event = mesh.userData;
          var eventId = String(event.id);
          var active = Number(event.start_ms) <= t && Number(event.end_ms) >= t;
          if (isTutorial) active = currentTutorialStep() === "airdrop";
          mesh.visible = active && collectedCoinIds.indexOf(eventId) < 0;
        });
      }

      function updateZoneMeshes(t) {
        var tutorialStep = currentTutorialStep();
        var visibleCount = 0;
        zoneMeshes.forEach(function (mesh) {
          var zone = mesh.userData.zone || {};
          var active =
            Number(zone.eff_ms) <= t &&
            Number(zone.exp_ms) >= t;
          if (isTutorial) {
            active = mesh.userData.tutorialCue === tutorialStep;
          }
          mesh.visible = active;
          if (active) visibleCount += 1;
        });
        canvas.dataset.visibleZoneMeshCount = String(visibleCount);
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
        Object.keys(replayPlayers).forEach(function (id) {
          var player = replayPlayers[id];
          var mesh = ensureCarMesh(id, player, false);
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
        var follow = 48;
        var height = 24;
        var x = worldX(own.x);
        var z = worldZ(own.y);
        cameraOffset.set(
          x - Math.cos(own.heading) * follow,
          height,
          z - Math.sin(own.heading) * follow
        );
        // Keep a fixed follow distance. Position interpolation made the camera
        // lag farther behind as the car accelerated, which looked like zooming.
        camera.position.copy(cameraOffset);
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
        var tutorialStep = currentTutorialStep();
        if (
          isTutorial &&
          tutorialStep !== "landing_zone_medium" &&
          tutorialStep !== "landing_zone_high"
        ) return;
        warnings.forEach(function (zone) {
          if (Number(zone.eff_ms) > t || Number(zone.exp_ms) < t) return;
          if (isTutorial) {
            var isHighChance = String(zone.event || "").toLowerCase().indexOf("tornado") >= 0;
            if (tutorialStep === "landing_zone_medium" && isHighChance) return;
            if (tutorialStep === "landing_zone_high" && !isHighChance) return;
          }
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

      function drawMapPotential(t) {
        if (isTutorial && currentTutorialStep() !== "airplane_sighting") return;
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

      function drawMapRewards(t, now) {
        var activePulseCount = 0;
        if (overviewCanvas) {
          overviewCanvas.dataset.activeAirdropPulseCount = "0";
        }
        if (isTutorial && currentTutorialStep() !== "airdrop") return;
        var scale = mapScreenScale();
        rewardEvents.forEach(function (event) {
          if (Number(event.start_ms) > t || Number(event.end_ms) < t) return;
          if (collectedCoinIds.indexOf(String(event.id)) >= 0) return;
          var screen = worldToMap(event.x, event.y);
          var radius = Math.max(1, Number(event.radius || cfg.coin_radius) * scale);
          if (!isOnMap(
            screen,
            radius * REWARD_MAP_PULSE_RADIUS_MULTIPLIER + 4
          )) return;
          overviewCtx.save();
          var pulseAge = isTutorial
            ? tutorial.elapsed(now)
            : t - Number(event.start_ms);
          if (
            pulseAge >= 0 &&
            pulseAge < REWARD_MAP_PULSE_DURATION_MS
          ) {
            activePulseCount += 1;
            var pulsePhase =
              (pulseAge % REWARD_MAP_PULSE_PERIOD_MS) /
              REWARD_MAP_PULSE_PERIOD_MS;
            var pulseRadius = radius * (
              1 +
              (REWARD_MAP_PULSE_RADIUS_MULTIPLIER - 1) * pulsePhase
            );
            overviewCtx.beginPath();
            overviewCtx.arc(
              screen.x,
              screen.y,
              pulseRadius,
              0,
              Math.PI * 2
            );
            overviewCtx.strokeStyle =
              "rgba(244, 196, 48, " +
              String(0.35 + (1 - pulsePhase) * 0.65) +
              ")";
            overviewCtx.lineWidth = 4;
            overviewCtx.stroke();
          }
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
        if (overviewCanvas) {
          overviewCanvas.dataset.activeAirdropPulseCount =
            String(activePulseCount);
        }
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

      function drawMapPlayers(now, focusStep, observing) {
        var ownScreen = worldToMap(own.x, own.y);
        overviewCtx.save();
        overviewCtx.filter =
          observing &&
          focusStep !== "overview_map" &&
          focusStep !== "movement"
            ? "grayscale(1) brightness(0.68)"
            : "none";
        drawHeadingCone(ownScreen, own.heading || 0);
        overviewCtx.restore();
        var visiblePlayers = Object.assign({}, players, replayPlayers);
        Object.keys(visiblePlayers).forEach(function (id) {
          var isOwn = id === participantId;
          var isReplay = id.indexOf("replay:") === 0;
          var player = isOwn
            ? own
            : (isReplay ? visiblePlayers[id] : renderedPlayer(visiblePlayers[id], now));
          var screen = worldToMap(player.x, player.y);
          var markerRadius = isOwn
            ? Number(cfg.player_radius) * 0.5
            : Number(cfg.player_radius) * 0.75;
          if (!isOnMap(screen, markerRadius + 6)) return;
          overviewCtx.save();
          var keepColor =
            !observing ||
            focusStep === "overview_map" ||
            (focusStep === "movement" && isOwn) ||
            (focusStep === "other_car" && !isOwn);
          overviewCtx.filter = keepColor
            ? "none"
            : "grayscale(1) brightness(0.68)";
          overviewCtx.globalAlpha = 0.5;
          overviewCtx.beginPath();
          overviewCtx.arc(screen.x, screen.y, markerRadius, 0, Math.PI * 2);
          overviewCtx.fillStyle = player.color || (isOwn ? "#1f77b4" : "#d62728");
          overviewCtx.fill();
          overviewCtx.lineWidth = isOwn ? 4 : 2;
          overviewCtx.strokeStyle = isOwn ? "#111827" : "#ffffff";
          overviewCtx.stroke();
          overviewCtx.restore();
        });
      }

      function drawMapArrow(from, to, color) {
        var dx = to.x - from.x;
        var dy = to.y - from.y;
        var distance = Math.hypot(dx, dy);
        if (distance < 8) return false;
        var ux = dx / distance;
        var uy = dy / distance;
        var startX = from.x + ux * 12;
        var startY = from.y + uy * 12;
        var endX = to.x - ux * 9;
        var endY = to.y - uy * 9;
        overviewCtx.save();
        overviewCtx.strokeStyle = color;
        overviewCtx.fillStyle = color;
        overviewCtx.lineWidth = 3;
        overviewCtx.lineCap = "round";
        overviewCtx.beginPath();
        overviewCtx.moveTo(startX, startY);
        overviewCtx.lineTo(endX, endY);
        overviewCtx.stroke();
        overviewCtx.beginPath();
        overviewCtx.moveTo(to.x, to.y);
        overviewCtx.lineTo(
          endX - uy * 6,
          endY + ux * 6
        );
        overviewCtx.lineTo(
          endX + uy * 6,
          endY - ux * 6
        );
        overviewCtx.closePath();
        overviewCtx.fill();
        overviewCtx.restore();
        return true;
      }

      function drawTutorialMapArrows() {
        if (!isTutorial) return;
        var step = currentTutorialStep();
        var ownScreen = worldToMap(own.x, own.y);
        var arrowCount = 0;
        if (step === "airdrop") {
          var reward = rewardEvents.find(function (event) {
            return String(event.id) === String(tutorialCfg.reward_id);
          });
          if (reward) {
            if (
              drawMapArrow(
                ownScreen,
                worldToMap(reward.x, reward.y),
                "#b07d00"
              )
            ) {
              arrowCount += 1;
            }
          }
        } else if (step === "airplane_sighting") {
          var sightingHint = tutorialCfg.airplane_sighting_hint || {};
          if (
            drawMapArrow(
              ownScreen,
              worldToMap(sightingHint.x, sightingHint.y),
              "#7c3aed"
            )
          ) {
            arrowCount += 1;
          }
        } else if (step === "landing_zone_medium") {
          var mediumHint = tutorialCfg.medium_zone_hint || {};
          if (
            drawMapArrow(
              ownScreen,
              worldToMap(mediumHint.x, mediumHint.y),
              "#d97706"
            )
          ) {
            arrowCount += 1;
          }
        } else if (step === "landing_zone_high") {
          var highHint = tutorialCfg.high_zone_hint || {};
          if (
            drawMapArrow(
              ownScreen,
              worldToMap(highHint.x, highHint.y),
              "#dc2626"
            )
          ) {
            arrowCount += 1;
          }
        } else if (step === "other_car") {
          var demoId = String((tutorialCfg.demo_car || {}).participant_id || "tutorial:other-car");
          var demoPlayer = replayPlayers[demoId];
          if (demoPlayer) {
            if (
              drawMapArrow(
                ownScreen,
                worldToMap(demoPlayer.x, demoPlayer.y),
                "#dc2626"
              )
            ) {
              arrowCount += 1;
            }
          }
        }
        if (overviewCanvas) {
          overviewCanvas.dataset.tutorialArrowCount = String(arrowCount);
        }
      }

      function drawOverview(t, now) {
        if (isLobby || !overviewCtx) return;
        var focusStep = currentTutorialStep();
        var observing = isTutorialObserving(now);
        var muteMapBackground =
          observing && focusStep !== "overview_map";
        overviewCtx.clearRect(0, 0, overviewSize, overviewSize);
        overviewCtx.save();
        overviewCtx.filter = muteMapBackground
          ? "grayscale(1) brightness(0.68)"
          : "none";
        overviewCtx.fillStyle = "#f8fbff";
        overviewCtx.fillRect(0, 0, overviewSize, overviewSize);
        if (!gameStarted) {
          overviewCtx.restore();
          return;
        }
        drawMapGrid();
        overviewCtx.restore();
        overviewCtx.save();
        overviewCtx.filter = "none";
        drawMapZones(t);
        drawMapPotential(t);
        drawMapRewards(t, now);
        overviewCtx.restore();
        drawMapPlayers(now, focusStep, observing);
        overviewCtx.save();
        overviewCtx.filter = "none";
        drawTutorialMapArrows();
        overviewCtx.restore();
      }

      function renderFrame(t, now) {
        renderer.render(scene, camera);
        drawOverview(t || 0, now || performance.now());
      }

      function tick(now) {
        updateCountdown(now);
        if (!gameStarted) {
          updateSpeedGauge();
          lastDrawAt = now;
          renderFrame(0, now);
          return;
        }
        integrateOwnPlayer(now);
        updateSpeedGauge();
        var t = isLobby ? 0 : gameTime(now);
        if (!isLobby) {
          if (isTutorial) {
            updateTutorial(now);
          } else {
            checkRewardCollections(t, now);
            updateReplayPlayers(t, now);
          }
          updateRewards(t);
          updateZoneMeshes(t);
        }
        updateCars(now);
        updateCameras();
        renderFrame(t, now);
      }

      function sendPosition() {
        if (!gameStarted || isTutorial) return;
        var now = performance.now();
        if (isLobby) {
          wsSend({
            type: "lobby_position",
            x: own.x,
            y: own.y,
            vx: own.vx,
            vy: own.vy,
            client_time: now,
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
        if (submitted || !psynet.pageReady) return;
        submitted = true;
        Promise.resolve(psynet.nextPage())
          .then(function (passedValidation) {
            if (!passedValidation) submitted = false;
          })
          .catch(function () {
            submitted = false;
          });
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

      function stopLoops() {
        stopBonusAnimation();
        if (drawHandle != null) {
          window.cancelAnimationFrame(drawHandle);
          drawHandle = null;
        }
        if (sendInterval != null) {
          clearInterval(sendInterval);
          sendInterval = null;
        }
        if (completionInterval != null) {
          clearInterval(completionInterval);
          completionInterval = null;
        }
        if (pollInterval != null) {
          clearInterval(pollInterval);
          pollInterval = null;
        }
      }

      function showFinishingOverlay() {
        if (!waitingOverlay) return;
        waitingOverlay.classList.remove("hidden");
        waitingOverlay.textContent = isLobby
          ? "Matching you with other players..."
          : "Finishing...";
      }

      function submitFinalAnswer() {
        if (submitted) return;
        submitted = true;
        stopLoops();
        showFinishingOverlay();
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

      function handleCanvasClick() {
        ensureCoinFallAudio();
        focusCanvas();
      }

      function updateZoomLabel() {
        if (zoomLabel) zoomLabel.textContent = String(Math.round(viewportMiles));
      }

      function handleZoomChange() {
        viewportMiles = Number(zoomSlider.value || 200);
        updateZoomLabel();
        centerMapCameraOn(own);
      }

      function handleOverviewZoomKey(code) {
        if (!overviewCanvas) return false;
        var min = zoomSlider
          ? Number(zoomSlider.min)
          : (isTutorial ? 20 : 200);
        var max = zoomSlider
          ? Number(zoomSlider.max)
          : (isTutorial ? 100 : 1500);
        var step = zoomSlider
          ? Number(zoomSlider.step) || 1
          : (isTutorial ? 5 : 25);
        viewportMiles = clamp(
          viewportMiles + (code === "Digit1" ? step : -step),
          min,
          max
        );
        if (zoomSlider) zoomSlider.value = String(viewportMiles);
        updateZoomLabel();
        centerMapCameraOn(own);
        return true;
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
        ensureCoinFallAudio();
        if (
          isTutorialObserving(performance.now()) &&
          (
            event.code === "Digit1" ||
            event.code === "Digit2" ||
            driveKeyForEvent(event)
          )
        ) {
          event.preventDefault();
          return;
        }
        if (event.code === "Digit1" || event.code === "Digit2") {
          if (handleOverviewZoomKey(event.code)) {
            event.preventDefault();
            return;
          }
        }
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
      if (tutorial) tutorial.update(performance.now());
      updateTutorialArrow();
      if (isTutorial) centerMapCameraOn(own);
      updateCars(performance.now());
      updateCameras();
      renderFrame();
      updateWaitingOverlay();
      if (zoomSlider) {
        viewportMiles = Number(zoomSlider.value || 750);
        updateZoomLabel();
        psynet.addPageEventListener(zoomSlider, "input", handleZoomChange);
      }
      psynet.addPageEventListener(canvas, "click", handleCanvasClick);
      psynet.addPageEventListener(window, "keydown", handleKeyDown, true);
      psynet.addPageEventListener(window, "keyup", handleKeyUp, true);
      psynet.addPageEventListener(window, "blur", function () { keys = {}; });
      psynet.addPageEventListener(window, "resize", layoutRenderer);
      focusCanvas();

      var unsubscribeHandlers = [];
      if (isTutorial) {
        // Tutorial state is deliberately local: it neither joins nor mutates a live session.
      } else if (isLobby) {
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
          psynet.websocket.handle("coin_collected", function (msg) {
            applyCollection(msg);
          }),
          psynet.websocket.handle("collect_rejected", function (msg) {
            handleCollectRejected(msg);
          })
        );
        if (usesLivePeers) {
          unsubscribeHandlers.push(
            psynet.websocket.handle("position_update", function (msg) {
              applyPosition(msg.player);
            })
          );
        }
        psynet.session.ready();
      }

      var drawHandle = null;
      var sendInterval = null;
      var completionInterval = null;
      var pollInterval = null;
      function loop() {
        tick(performance.now());
        drawHandle = window.requestAnimationFrame(loop);
      }
      drawHandle = window.requestAnimationFrame(loop);
      sendInterval = isTutorial ? null : setInterval(sendPosition, cfg.send_interval_ms);
      completionInterval = (isLobby || isTutorial) ? null : setInterval(function () {
        if (gameEndsAt != null && performance.now() >= gameEndsAt) submitFinalAnswer();
      }, 200);
      pollInterval = isLobby ? setInterval(pollLobbyRelease, 1000) : null;
      if (isLobby) pollLobbyRelease();

      var cleanedUp = false;
      function cleanup() {
        if (cleanedUp) return;
        cleanedUp = true;
        stopLoops();
        unsubscribeHandlers.forEach(function (unsubscribe) {
          if (typeof unsubscribe === "function") unsubscribe();
        });
        if (coinFallAudio) {
          coinFallAudio.pause();
          coinFallAudio.currentTime = 0;
        }
        if (renderer) renderer.dispose();
      }

      psynet.addPageCleanupCallback(cleanup);
    },
  };
})(window);
