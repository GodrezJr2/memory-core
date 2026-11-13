/**
 * Layout for the topology map, in SVG user units. Connectors are orthogonal routes (no diagonals) that
 * never cross: on each side, the agent with the largest drop to its hub port turns closest to the hub.
 *
 * Wide: agents in two columns either side of a square memory bank.
 * Compact (phones): the bank top right, agents stacked below, each on its own lane up into the bank.
 */

export interface Point {
  x: number;
  y: number;
}
export interface Box extends Point {
  w: number;
  h: number;
}
export interface NodeGeom {
  id: string;
  box: Box;
  d: string; // connector path, agent -> hub
  start: Point; // where the connector leaves the agent box
  end: Point; // where it enters the hub
  bends: Point[];
}
export interface Geometry {
  W: number;
  H: number;
  hub: Box;
  nodes: NodeGeom[];
  compact: boolean;
}

const WIDE = { W: 960, margin: 28, boxW: 196, boxH: 58, hub: 188, rowGap: 92, firstTurn: 38, turnStep: 26 };
const COMPACT = { W: 380, margin: 14, boxW: 176, boxH: 52, hubW: 168, hubH: 128, top: 172, rowGap: 66 };

export function computeGeometry(ids: string[], compact: boolean): Geometry {
  return compact ? compactLayout(ids) : wideLayout(ids);
}

function wideLayout(ids: string[]): Geometry {
  const { W, margin, boxW, boxH, rowGap, firstTurn, turnStep } = WIDE;
  const half = Math.ceil(ids.length / 2);
  const columns: [string[], "left" | "right"][] = [
    [ids.slice(0, half), "left"],
    [ids.slice(half), "right"],
  ];
  const perSide = Math.max(half, 1);
  const H = Math.max(440, perSide * rowGap + 72);
  const hub: Box = { x: (W - WIDE.hub) / 2, y: (H - WIDE.hub) / 2, w: WIDE.hub, h: WIDE.hub };
  const nodes: NodeGeom[] = [];

  for (const [list, side] of columns) {
    const k = list.length;
    if (!k) continue;
    const toward = side === "left" ? 1 : -1; // direction from the agents to the hub
    const hubEdge = side === "left" ? hub.x : hub.x + hub.w;
    const cys = list.map((_, i) => (k === 1 ? H / 2 : 66 + (i * (H - 132)) / (k - 1)));
    const pys = list.map((_, i) => hub.y + ((i + 1) * hub.h) / (k + 1));
    const straight = list.map((_, i) => Math.abs(cys[i] - pys[i]) < 2);

    // rank turns separately for agents above their port and below it; bigger drop -> turn nearer the hub
    const rank = new Array<number>(k).fill(0);
    for (const above of [true, false]) {
      list
        .map((_, i) => i)
        .filter((i) => !straight[i] && cys[i] < pys[i] === above)
        .sort((a, b) => Math.abs(cys[b] - pys[b]) - Math.abs(cys[a] - pys[a]))
        .forEach((i, r) => (rank[i] = r));
    }

    list.forEach((id, i) => {
      const box: Box = { x: side === "left" ? margin : W - margin - boxW, y: cys[i] - boxH / 2, w: boxW, h: boxH };
      const start = { x: side === "left" ? box.x + box.w : box.x, y: cys[i] };
      if (straight[i]) {
        nodes.push({ id, box, start, end: { x: hubEdge, y: cys[i] }, bends: [], d: `M ${start.x} ${start.y} H ${hubEdge}` });
        return;
      }
      const turn = hubEdge - toward * (firstTurn + rank[i] * turnStep);
      const end = { x: hubEdge, y: pys[i] };
      nodes.push({
        id, box, start, end,
        bends: [{ x: turn, y: start.y }, { x: turn, y: end.y }],
        d: `M ${start.x} ${start.y} H ${turn} V ${end.y} H ${end.x}`,
      });
    });
  }
  return { W, H, hub, nodes, compact: false };
}

function compactLayout(ids: string[]): Geometry {
  const { W, margin, boxW, boxH, hubW, hubH, top, rowGap } = COMPACT;
  const n = ids.length;
  const hub: Box = { x: W - margin - hubW, y: 18, w: hubW, h: hubH };
  const H = top + Math.max(n - 1, 0) * rowGap + boxH / 2 + 22;
  const inset = 22;
  const nodes = ids.map((id, i): NodeGeom => {
    const cy = top + i * rowGap;
    const box: Box = { x: margin, y: cy - boxH / 2, w: boxW, h: boxH };
    // lanes left to right in agent order, so a lower agent's run never crosses a higher agent's lane
    const lane = n === 1 ? hub.x + hub.w / 2 : hub.x + inset + (i * (hub.w - 2 * inset)) / (n - 1);
    const start = { x: box.x + box.w, y: cy };
    const end = { x: lane, y: hub.y + hub.h };
    return { id, box, start, end, bends: [{ x: lane, y: cy }], d: `M ${start.x} ${cy} H ${lane} V ${end.y}` };
  });
  return { W, H, hub, nodes, compact: true };
}
