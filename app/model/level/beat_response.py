import math

from model.color import clamp


class BeatEnvelope:
    """Turn onset strengths into a held, gently changing lighting level."""

    def __init__(self, hold_seconds=0.12, retrigger_seconds=0.24):
        self.hold_seconds = hold_seconds
        self.retrigger_seconds = retrigger_seconds
        self.reset()

    def reset(self):
        self.level = 0.0
        self.peak = 0.0
        self.reference_peak = 0.0
        self.elapsed = 0.0
        self.hold_until = 0.0
        self.next_onset = 0.0
        self.gate_open = False
        self.accepted_onset = False

    def update(self, raw_level, dt, *, onset, threshold=0.08, decay=0.08, smoothing=0.28):
        raw_level = float(raw_level)
        raw_level = clamp(raw_level) if math.isfinite(raw_level) else 0.0
        dt = max(0.0, float(dt))
        if not math.isfinite(dt) or dt == 0.0:
            return self.level

        started = self.elapsed
        self.elapsed += dt
        self.accepted_onset = False
        self.reference_peak *= math.exp(-dt / 3.0)
        # A weak hat between stronger beats must not restart the hold period.
        high_threshold = max(clamp(threshold), self.reference_peak * 0.35)
        if raw_level >= high_threshold and raw_level > 0:
            self.gate_open = True
        elif raw_level < high_threshold * 0.6 or raw_level == 0:
            self.gate_open = False

        release = clamp(0.05 / max(0.01, decay), 0.25, 1.2)
        release_dt = max(0.0, self.elapsed - max(started, self.hold_until))
        self.peak *= math.exp(-release_dt / release)
        if (onset and self.gate_open and raw_level >= self.reference_peak * 0.35
                and self.elapsed >= self.next_onset):
            self.peak = max(self.peak, raw_level)
            self.reference_peak = max(self.reference_peak, raw_level)
            self.hold_until = self.elapsed + self.hold_seconds
            self.next_onset = self.elapsed + self.retrigger_seconds
            self.accepted_onset = True

        smoothing = clamp(smoothing)
        response_dt = min(dt, 0.05)
        tau = (0.045 + 0.08 * (1.0 - smoothing) if self.peak > self.level
               else 0.18 + 0.25 * (1.0 - smoothing))
        change = (self.peak - self.level) * -math.expm1(-response_dt / tau)
        # Limit rises and falls even after a delayed UI frame or a loud impulse.
        change = clamp(change, -0.75 * response_dt, 2.0 * response_dt)
        self.level = clamp(self.level + change)
        if self.peak < 0.003 and self.level < 0.003:
            self.peak = self.level = 0.0
        return self.level


def smooth_lamp_level(previous, target, dt, smoothing=0.28, *, spectrum=False):
    """Ignore tiny reversals and bound the speed of palette/brightness changes."""
    previous, target = clamp(previous), clamp(target)
    dt = min(0.05, max(0.0, dt))
    if abs(target - previous) < 0.02 and target != 0.0:
        return previous
    smoothing = clamp(smoothing)
    if spectrum:
        tau = 0.06 + (1.0 - smoothing) * 0.15
        target = previous + (target - previous) * -math.expm1(-dt / tau)
    change = clamp(target - previous, -(0.4 + smoothing * 0.7) * dt,
                   (1.0 + smoothing * 1.5) * dt)
    return clamp(previous + change)
