/* global SharedCanvas3D, THREE */

export function activate({ vars, psynet }) {
  const config = vars.canvas_config;
  if (config.mode !== "lobby" && !config.world_url) {
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
    const trackCount = Number(config.storm_chaser_track_count || 8);
    if (Array.isArray(config.chaser_tracks)) {
      config.chaser_tracks = config.chaser_tracks.slice(0, trackCount);
    }
  }

  function start() {
    if (typeof THREE === "undefined" || !window.SharedCanvas3D) {
      throw new Error("Three.js storm-chase client failed to load.");
    }
    worldPromise.then(mergeWorld).then(() => {
      SharedCanvas3D.start(config);
    });
  }

  if (config.mode === "lobby") {
    start();
    return;
  }

  psynet.trial.onEvent("liveSessionInit", start);
}
