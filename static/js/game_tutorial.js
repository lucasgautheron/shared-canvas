/* global performance */
(function (root) {
  "use strict";

  var STEP_COPY = {
    movement: {
      title: "Learn to move your car",
      instruction: "Use the arrow keys or WASD to drive. Follow the 3D arrow and move away from your starting point.",
    },
    airdrop: {
      title: "Collect an airdrop",
      instruction: "The transparent yellow cylinder marks an airdrop. Follow the arrows and drive into it.",
    },
    overview_map: {
      title: "Use the overview map",
      instruction: "The overview map in the upper right shows a wider area around your car. It will display airplane sightings, potential landing zones, airdrops, and other drivers.",
    },
    airplane_sighting: {
      title: "Drive to an airplane sighting",
      instruction: "Colored squares on the overview map are airplane sightings. The scale below runs from low to high strength; stronger sightings may help you locate an airdrop. Follow the arrow and drive to the highlighted square.",
    },
    landing_zone_medium: {
      title: "Drive to a medium-chance landing zone",
      instruction: "Yellow polygons are potential landing zones with a medium chance of receiving an airdrop. Follow the arrow and drive inside the highlighted yellow polygon.",
    },
    landing_zone_high: {
      title: "Drive to a higher-chance landing zone",
      instruction: "Red polygons are potential landing zones with a higher chance of receiving an airdrop. Follow the arrow and drive inside the highlighted red polygon.",
    },
    other_car: {
      title: "Watch for other cars",
      instruction: "Other cars may move through the same space. The arrows point to a demonstration car driving around you.",
    },
  };

  function createGameTutorial(options) {
    var config = options.config || {};
    var steps = (config.steps || []).slice();
    var stepIndex = 0;
    var stepStartedAt = performance.now();
    var completedSteps = [];
    var stepElapsedMs = {};
    var introDurationMs = Number(config.intro_duration_ms || 10000);
    var finalCarDurationMs = Number(config.final_car_duration_ms || 15000);
    var panel = document.getElementById("tutorial-panel");
    var progress = document.getElementById("tutorial-progress");
    var title = document.getElementById("tutorial-title");
    var instruction = document.getElementById("tutorial-instruction");
    var timer = document.getElementById("tutorial-timer");
    var sightingScale = document.getElementById("tutorial-sighting-scale");
    var continueButton = document.getElementById("tutorial-continue");

    function currentStep() {
      return steps[stepIndex] || null;
    }

    function elapsed(now) {
      return Math.max(0, now - stepStartedAt);
    }

    function isObserving(now) {
      return currentStep() != null && elapsed(now) < introDurationMs;
    }

    function renderCopy() {
      var step = currentStep();
      var copy = STEP_COPY[step];
      if (!copy) return;
      if (panel) {
        panel.dataset.step = step;
        panel.dataset.condition = options.condition;
      }
      if (progress) {
        progress.textContent =
          "Step " + String(stepIndex + 1) + " of " + String(steps.length);
      }
      if (title) title.textContent = copy.title;
      if (instruction) instruction.textContent = copy.instruction;
      if (sightingScale) {
        sightingScale.classList.toggle(
          "hidden",
          step !== "airplane_sighting"
        );
      }
      if (continueButton) continueButton.classList.add("hidden");
    }

    function update(now) {
      var step = currentStep();
      var stepElapsed = elapsed(now);
      var observing = isObserving(now);
      if (panel) panel.classList.toggle("observing", observing);
      options.canvas.dataset.tutorialFrozen = String(observing);
      if (timer) {
        if (step === "other_car") {
          var finalRemaining = Math.max(
            0,
            Math.ceil((finalCarDurationMs - stepElapsed) / 1000)
          );
          timer.textContent =
            "Tutorial ends in " + String(finalRemaining) + " s";
          timer.classList.remove("hidden");
        } else if (observing) {
          var introRemaining = Math.max(
            0,
            Math.ceil((introDurationMs - stepElapsed) / 1000)
          );
          timer.textContent =
            "Observe: " +
            String(introRemaining) +
            " s";
          timer.classList.remove("hidden");
        } else {
          timer.classList.add("hidden");
        }
      }
      if (options.onFocusChange) {
        options.onFocusChange(step, observing);
      }
      if (step === "other_car" && stepElapsed >= finalCarDurationMs) {
        return "advance";
      }
      if (step === "overview_map" && !observing) {
        return "advance";
      }
      return null;
    }

    function advance(now) {
      var step = currentStep();
      if (!step) return;
      completedSteps.push(step);
      stepElapsedMs[step] = Math.max(0, Math.round(elapsed(now)));
      stepIndex += 1;
      stepStartedAt = now;
      if (options.onStepChanged) {
        options.onStepChanged(currentStep(), now);
      }
      if (stepIndex >= steps.length) {
        if (panel) panel.dataset.step = "complete";
        if (options.onComplete) options.onComplete(now);
        return;
      }
      renderCopy();
    }

    renderCopy();

    return {
      advance: advance,
      completedSteps: function () {
        return completedSteps.slice();
      },
      currentStep: currentStep,
      elapsed: elapsed,
      isObserving: isObserving,
      stepElapsedMs: function () {
        return Object.assign({}, stepElapsedMs);
      },
      update: update,
    };
  }

  root.GameTutorial = {
    create: createGameTutorial,
  };
})(window);
