export interface ElapsedClockState {
  startedAtMs: number | null;
  endedAtMs: number | null;
  pauseStartedAtMs: number | null;
  pausedAccumulatedMs: number;
}

export const IDLE_ELAPSED_CLOCK: ElapsedClockState = {
  startedAtMs: null,
  endedAtMs: null,
  pauseStartedAtMs: null,
  pausedAccumulatedMs: 0
};

export function startElapsedClock(nowMs: number): ElapsedClockState {
  return {
    startedAtMs: nowMs,
    endedAtMs: null,
    pauseStartedAtMs: null,
    pausedAccumulatedMs: 0
  };
}

export function pauseElapsedClock(state: ElapsedClockState, nowMs: number): ElapsedClockState {
  if (state.startedAtMs == null || state.endedAtMs != null || state.pauseStartedAtMs != null) {
    return state;
  }
  return { ...state, pauseStartedAtMs: nowMs };
}

export function resumeElapsedClock(state: ElapsedClockState, nowMs: number): ElapsedClockState {
  if (state.pauseStartedAtMs == null || state.endedAtMs != null) {
    return state;
  }
  return {
    ...state,
    pausedAccumulatedMs: state.pausedAccumulatedMs + Math.max(0, nowMs - state.pauseStartedAtMs),
    pauseStartedAtMs: null
  };
}

export function stopElapsedClock(state: ElapsedClockState, nowMs: number): ElapsedClockState {
  if (state.startedAtMs == null || state.endedAtMs != null) {
    return state;
  }
  const endedAtMs = state.pauseStartedAtMs ?? nowMs;
  return { ...state, endedAtMs, pauseStartedAtMs: null };
}

export function readElapsedMs(state: ElapsedClockState, nowMs: number): number {
  if (state.startedAtMs == null) {
    return 0;
  }
  const end = state.endedAtMs ?? state.pauseStartedAtMs ?? nowMs;
  return Math.max(0, end - state.startedAtMs - state.pausedAccumulatedMs);
}

export function formatElapsedClock(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const paddedMinutes = String(minutes).padStart(2, "0");
  const paddedSeconds = String(seconds).padStart(2, "0");

  if (hours > 0) {
    return `${hours}:${paddedMinutes}:${paddedSeconds}`;
  }
  return `${paddedMinutes}:${paddedSeconds}`;
}

export function elapsedClockIsTicking(state: ElapsedClockState): boolean {
  return state.startedAtMs != null && state.endedAtMs == null && state.pauseStartedAtMs == null;
}

export function elapsedClockSelfCheck(): string[] {
  const errors: string[] = [];
  const started = startElapsedClock(1_000);

  if (readElapsedMs(started, 4_000) !== 3_000) {
    errors.push("running elapsed should follow client wall clock");
  }

  const paused = pauseElapsedClock(started, 5_000);
  if (readElapsedMs(paused, 9_000) !== 4_000) {
    errors.push("HITL pause must freeze elapsed time");
  }

  const resumed = resumeElapsedClock(paused, 8_000);
  if (readElapsedMs(resumed, 10_000) !== 6_000) {
    errors.push("resume should exclude the paused window");
  }

  const stopped = stopElapsedClock(resumed, 12_000);
  if (readElapsedMs(stopped, 99_999) !== 8_000) {
    errors.push("completed elapsed must freeze at actual consumed time");
  }

  const truncatedEventsWouldLie = 90_000;
  if (readElapsedMs(stopped, truncatedEventsWouldLie) === truncatedEventsWouldLie) {
    errors.push("elapsed must not depend on remaining event timestamps");
  }

  return errors;
}
