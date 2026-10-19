(function (root, factory) {
  "use strict";

  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.GameCollision = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  var EPSILON = 1e-9;

  function coordinate(point, axis) {
    if (Array.isArray(point)) return Number(point[axis === "x" ? 0 : 1]);
    return Number(point[axis]);
  }

  function movementBounds(from, to) {
    return {
      minX: Math.min(coordinate(from, "x"), coordinate(to, "x")),
      minY: Math.min(coordinate(from, "y"), coordinate(to, "y")),
      maxX: Math.max(coordinate(from, "x"), coordinate(to, "x")),
      maxY: Math.max(coordinate(from, "y"), coordinate(to, "y")),
    };
  }

  function boundsOverlap(first, second) {
    return !(
      first.maxX < second.minX ||
      first.minX > second.maxX ||
      first.maxY < second.minY ||
      first.minY > second.maxY
    );
  }

  function pointOnSegment(point, start, end) {
    var px = coordinate(point, "x");
    var py = coordinate(point, "y");
    var ax = coordinate(start, "x");
    var ay = coordinate(start, "y");
    var bx = coordinate(end, "x");
    var by = coordinate(end, "y");
    var cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax);
    if (Math.abs(cross) > EPSILON) return false;
    return (
      px >= Math.min(ax, bx) - EPSILON &&
      px <= Math.max(ax, bx) + EPSILON &&
      py >= Math.min(ay, by) - EPSILON &&
      py <= Math.max(ay, by) + EPSILON
    );
  }

  function orientation(first, second, third) {
    var value =
      (coordinate(second, "y") - coordinate(first, "y")) *
        (coordinate(third, "x") - coordinate(second, "x")) -
      (coordinate(second, "x") - coordinate(first, "x")) *
        (coordinate(third, "y") - coordinate(second, "y"));
    if (Math.abs(value) <= EPSILON) return 0;
    return value > 0 ? 1 : -1;
  }

  function segmentsIntersect(firstStart, firstEnd, secondStart, secondEnd) {
    var o1 = orientation(firstStart, firstEnd, secondStart);
    var o2 = orientation(firstStart, firstEnd, secondEnd);
    var o3 = orientation(secondStart, secondEnd, firstStart);
    var o4 = orientation(secondStart, secondEnd, firstEnd);
    if (o1 !== o2 && o3 !== o4) return true;
    if (o1 === 0 && pointOnSegment(secondStart, firstStart, firstEnd)) return true;
    if (o2 === 0 && pointOnSegment(secondEnd, firstStart, firstEnd)) return true;
    if (o3 === 0 && pointOnSegment(firstStart, secondStart, secondEnd)) return true;
    return o4 === 0 && pointOnSegment(firstEnd, secondStart, secondEnd);
  }

  function pointInPolygon(point, polygon) {
    var x = coordinate(point, "x");
    var y = coordinate(point, "y");
    var inside = false;
    for (var i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
      if (pointOnSegment(point, polygon[j], polygon[i])) return true;
      var xi = coordinate(polygon[i], "x");
      var yi = coordinate(polygon[i], "y");
      var xj = coordinate(polygon[j], "x");
      var yj = coordinate(polygon[j], "y");
      var crosses =
        (yi > y) !== (yj > y) &&
        x < ((xj - xi) * (y - yi)) / (yj - yi) + xi;
      if (crosses) inside = !inside;
    }
    return inside;
  }

  function segmentIntersectsPolygon(from, to, polygon) {
    for (var i = 0; i < polygon.length; i += 1) {
      var next = (i + 1) % polygon.length;
      if (segmentsIntersect(from, to, polygon[i], polygon[next])) return true;
    }
    return false;
  }

  function wouldHitLake(from, to, lakes) {
    var bounds = movementBounds(from, to);
    for (var i = 0; i < lakes.length; i += 1) {
      var lake = lakes[i];
      var points = lake.points || [];
      if (points.length < 3 || !lake.bounds) continue;
      if (!boundsOverlap(bounds, lake.bounds)) continue;
      if (
        pointInPolygon(to, points) ||
        segmentIntersectsPolygon(from, to, points)
      ) {
        return true;
      }
    }
    return false;
  }

  function resolveMovement(from, to, lakes) {
    if (wouldHitLake(from, to, lakes || [])) {
      return { x: coordinate(from, "x"), y: coordinate(from, "y"), hit: true };
    }
    return { x: coordinate(to, "x"), y: coordinate(to, "y"), hit: false };
  }

  return {
    boundsOverlap: boundsOverlap,
    movementBounds: movementBounds,
    pointInPolygon: pointInPolygon,
    resolveMovement: resolveMovement,
    segmentIntersectsPolygon: segmentIntersectsPolygon,
    segmentsIntersect: segmentsIntersect,
    wouldHitLake: wouldHitLake,
  };
});
