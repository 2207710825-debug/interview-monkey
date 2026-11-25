const fingerJoints = [
  [8, 6, 5],
  [12, 10, 9],
  [16, 14, 13],
  [20, 18, 17],
];

function distance(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function fingerExtended(landmarks, [tip, pip, mcp]) {
  return distance(landmarks[tip], landmarks[mcp]) >
    distance(landmarks[pip], landmarks[mcp]) * 1.65;
}

export function isFist(landmarks) {
  if (!landmarks || landmarks.length < 21 || distance(landmarks[0], landmarks[9]) < 0.04) {
    return false;
  }
  return fingerJoints.every((joints) => !fingerExtended(landmarks, joints));
}

export function classifyGesture(landmarks) {
  if (!landmarks || landmarks.length < 21) return 'none';
  const palm = distance(landmarks[0], landmarks[9]);
  if (palm < 0.04) return 'none';
  const [index, middle, ring, pinky] = fingerJoints.map((joints) => fingerExtended(landmarks, joints));
  if (distance(landmarks[4], landmarks[8]) < palm * 0.45 && middle && ring && pinky) {
    return 'ok';
  }
  if (index && middle && !ring && !pinky && distance(landmarks[8], landmarks[12]) > palm * 0.25) {
    return 'victory';
  }
  const tipsApart = distance(landmarks[4], landmarks[8]);
  const indexRaised = distance(landmarks[8], landmarks[5]) >
    distance(landmarks[6], landmarks[5]) * 1.2;
  const thumbRaised = distance(landmarks[4], landmarks[2]) >
    distance(landmarks[3], landmarks[2]) * 1.08;
  if (!middle && !ring && !pinky && indexRaised && thumbRaised &&
      tipsApart > palm * 0.07 && tipsApart < palm * 0.45) {
    return 'heart';
  }
  if (index && middle && ring && pinky && distance(landmarks[4], landmarks[9]) < palm * 0.58) {
    return 'four';
  }
  if (isFist(landmarks)) return 'fist';
  return 'open';
}

export class FistHold {
  constructor(holdMs = 800, releaseMs = 250) {
    this.holdMs = holdMs;
    this.releaseMs = releaseMs;
    this.armed = true;
    this.fistSince = null;
    this.releaseSince = null;
  }

  update(fist, now) {
    if (fist) {
      this.releaseSince = null;
      if (!this.armed) return { progress: 0, triggered: false, armed: false };
      if (this.fistSince === null) this.fistSince = now;
      const progress = Math.min(1, (now - this.fistSince) / this.holdMs);
      if (progress === 1) {
        this.armed = false;
        this.fistSince = null;
        return { progress: 1, triggered: true, armed: false };
      }
      return { progress, triggered: false, armed: true };
    }

    this.fistSince = null;
    if (!this.armed) {
      if (this.releaseSince === null) this.releaseSince = now;
      if (now - this.releaseSince >= this.releaseMs) {
        this.armed = true;
        this.releaseSince = null;
      }
    }
    return { progress: 0, triggered: false, armed: this.armed };
  }
}
