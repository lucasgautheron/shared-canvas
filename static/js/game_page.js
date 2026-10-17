/* global Game3D, THREE */

export function activate({ vars, psynet }) {
  const config = vars.canvas_config;
  const startsImmediately = config.mode === "lobby" || config.mode === "tutorial";
  if (!startsImmediately && !config.world_url) {
    throw new Error("Missing world map URL.");
  }
  const worldPromise = config.world_url
    ? fetch(config.world_url).then((response) => {
        if (!response.ok) {
          throw new Error("Failed to load world map (" + response.status + ").");
        }
        return response.json();
      })
    : Promise.resolve(null);

  function mergeWorld(world) {
    if (!world) {
      return;
    }
    Object.assign(config, world);
    const trackCount = Number(config.storm_chaser_track_count || 16);
    if (Array.isArray(config.chaser_tracks)) {
      config.chaser_tracks = config.experiment_condition === "social"
        ? config.chaser_tracks.slice(0, trackCount)
        : [];
    }
  }

  function start() {
    if (typeof THREE === "undefined" || !window.Game3D) {
      throw new Error("Three.js airdrop game client failed to load.");
    }
    worldPromise.then(mergeWorld).then(() => {
      Game3D.start(config);
    });
  }

  if (startsImmediately) {
    start();
    return;
  }

  psynet.trial.onEvent("liveSessionInit", start);
}
