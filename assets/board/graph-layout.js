"use strict";
// A small force-directed layout for the Knowledge view. No dependencies: the
// board's content security policy only allows its own scripts.
const LAYOUT_REPULSION = 900,
  LAYOUT_SPRING = 0.04,
  LAYOUT_SPRING_LENGTH = 70,
  LAYOUT_GRAVITY = 0.012,
  LAYOUT_DAMPING = 0.82,
  LAYOUT_MIN_ALPHA = 0.004,
  LAYOUT_MAX_STEP = 18;

class GraphLayout {
  constructor() {
    this.points = new Map();
    this.links = [];
    this.alpha = 1;
  }

  // Keep known positions so live refreshes do not reshuffle the picture.
  update(nodes, edges) {
    const previous = this.points;
    this.points = new Map();
    const placed = [...previous.values()];
    nodes.forEach((node, index) => {
      const old = previous.get(node.id);
      if (old) {
        this.points.set(node.id, old);
        return;
      }
      const anchor = edges
        .filter((edge) => edge.src === node.id || edge.dst === node.id)
        .map((edge) => previous.get(edge.src === node.id ? edge.dst : edge.src))
        .find(Boolean);
      const angle = index * 2.399963;
      const radius = anchor ? 30 : 40 + 9 * Math.sqrt(index + placed.length);
      this.points.set(node.id, {
        x: (anchor?.x ?? 0) + Math.cos(angle) * radius,
        y: (anchor?.y ?? 0) + Math.sin(angle) * radius,
        vx: 0,
        vy: 0,
        fixed: false,
      });
    });
    this.links = edges
      .filter((edge) => this.points.has(edge.src) && this.points.has(edge.dst))
      .map((edge) => ({
        source: this.points.get(edge.src),
        target: this.points.get(edge.dst),
        strength: Math.min(3, edge.weight || 1),
      }));
    this.alpha = previous.size && nodes.every((node) => previous.has(node.id)) ? 0.25 : 1;
  }

  reheat(alpha = 0.5) {
    this.alpha = Math.max(this.alpha, alpha);
  }

  get settled() {
    return this.alpha < LAYOUT_MIN_ALPHA;
  }

  step() {
    if (this.settled) return false;
    const points = [...this.points.values()];
    for (let i = 0; i < points.length; i++) {
      const a = points[i];
      for (let j = i + 1; j < points.length; j++) {
        const b = points[j];
        let dx = a.x - b.x,
          dy = a.y - b.y,
          distance = dx * dx + dy * dy;
        if (distance > 160000) continue;
        if (distance < 1) {
          dx = Math.random() - 0.5;
          dy = Math.random() - 0.5;
          distance = 1;
        }
        const force = (LAYOUT_REPULSION * this.alpha) / distance;
        a.vx += dx * force;
        a.vy += dy * force;
        b.vx -= dx * force;
        b.vy -= dy * force;
      }
    }
    for (const link of this.links) {
      const dx = link.target.x - link.source.x,
        dy = link.target.y - link.source.y,
        distance = Math.sqrt(dx * dx + dy * dy) || 1;
      const force =
        ((distance - LAYOUT_SPRING_LENGTH) * LAYOUT_SPRING * this.alpha * link.strength) /
        distance;
      link.source.vx += dx * force;
      link.source.vy += dy * force;
      link.target.vx -= dx * force;
      link.target.vy -= dy * force;
    }
    for (const point of points) {
      if (point.fixed) {
        point.vx = point.vy = 0;
        continue;
      }
      point.vx = (point.vx - point.x * LAYOUT_GRAVITY * this.alpha) * LAYOUT_DAMPING;
      point.vy = (point.vy - point.y * LAYOUT_GRAVITY * this.alpha) * LAYOUT_DAMPING;
      point.x += Math.max(-LAYOUT_MAX_STEP, Math.min(LAYOUT_MAX_STEP, point.vx));
      point.y += Math.max(-LAYOUT_MAX_STEP, Math.min(LAYOUT_MAX_STEP, point.vy));
    }
    this.alpha *= 0.985;
    return true;
  }

  bounds() {
    let minX = Infinity,
      minY = Infinity,
      maxX = -Infinity,
      maxY = -Infinity;
    for (const point of this.points.values()) {
      minX = Math.min(minX, point.x);
      minY = Math.min(minY, point.y);
      maxX = Math.max(maxX, point.x);
      maxY = Math.max(maxY, point.y);
    }
    return Number.isFinite(minX) ? { minX, minY, maxX, maxY } : null;
  }
}
