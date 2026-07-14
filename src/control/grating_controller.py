"""Monotonic-sweep wavenumber control for the grating laser.

The grating drive has ~0.02 stage-units of BACKLASH (~0.2 cm^-1), far larger
than a fine scan step, and the wavemeter response is laggy/nonlinear (small moves
sit in a dead-band, then the grating engages and jumps). Closed-loop lock-to-
target is impossible there: any reversal drops into ~0.2 cm^-1 of lash, and
targets inside the lash are bistable.

So we SWEEP: the sweep direction (in wavenumber) is LOCKED for the whole scan --
taken from the monotonic target sequence -- and the loop NEVER reverses. It steps
the stage a fixed small ``sweep_step`` in the one engaging direction; a bin is
"reached" once the wavemeter has passed that bin's target in the sweep direction
(overshoot is fine -- we log the ACTUAL wavemeter reading and move on, we do NOT
turn around). Backlash is taken up once at the start. Because reversal is what
kills this drive, direction is fixed, not recomputed from per-step error.

``is_stable()`` means "not moving" (reached the checkpoint), NOT "wn within
tolerance" -- the grating can't hold a wavenumber to <backlash, so a tolerance
test drift-resets forever. Bin by the measured wn downstream.

Scan MONOTONICALLY (start->end one direction). Mirrors LaserController's public
surface so Scanner/DAQSystem are unchanged.
"""

import threading


class GratingController:
    def __init__(self, grating_device, epics_client, config: dict = {}):
        self.grating = grating_device
        self.epics = epics_client
        self.config = dict(config)

        self.tolerance = float(self.config.get("tolerance", 0.01))
        self.poll_interval = float(self.config.get("poll_interval", 0.2))
        self.sweep_step = float(self.config.get("sweep_step", 0.002))
        self.wn_per_unit = float(self.config.get("wn_per_unit", 11.0))
        self.backlash = float(self.config.get("backlash", 0.02))
        # CRAWL mode: the drive's stiction is bimodal (steps are either swallowed
        # or lurch ~40x), so stop-and-step can't land inside tolerance. Instead,
        # glide CONTINUOUSLY through the whole scan at the stage's velocity and
        # let the DAQ accumulate while the laser sweeps -- every event is logged
        # with measured_wn, so bins are reconstructed in analysis. A stage in
        # constant motion has no stick-slip (jog long-moves tune smoothly).
        # set_wavenumber() just extends the glide; is_stable() is always True.
        self.crawl = bool(self.config.get("crawl", False))
        self.crawl_leg = float(self.config.get("crawl_leg", 0.05))
        # The drive is DIRECTION-ASYMMETRIC: one direction is motor-driven
        # (smooth), the other spring-restored (lurchy -- landings overshoot ~3x
        # worse). preferred_wn_dir is the wavenumber direction of the smooth
        # side (+1 = scans should run low->high wn). Glides AGAINST it use a
        # much shorter leg so any runaway is bounded (conservative down-moves).
        self.preferred_wn_dir = float(self.config.get("preferred_wn_dir", 1.0))
        self.reverse_leg_scale = float(self.config.get("reverse_leg_scale", 0.2))

        self.wavechannel = int(self.config.get("wavechannel", 1))
        self.wm_pv = self.config.get("wm_pv", f"LaserLab:wavenumber_{self.wavechannel}")

        # State
        self.target_wn = 0.0
        self.is_moving = False
        self._last_move_dir = None      # last stage-move direction (+1/-1)
        self._sweep_dir = None          # LOCKED wavenumber sweep direction (+1/-1)
        self._prev_target = None        # previous target, to infer sweep direction

        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.control_thread = None

    # ---- Public API used by GUI / DAQSystem / Scanner ----

    def set_wavenumber(self, target_wn: float):
        """Set the target and start (or re-aim) the sweep. Locks the sweep
        direction from the target sequence so the loop never reverses."""
        target_wn = float(target_wn)
        with self.lock:
            # Direction (in wavenumber) is taken from the monotonic scan: the
            # sign of consecutive targets. First bin: crawl HOLDS (guessing from
            # the current wn opened scans with a wrong-way glide + reversal);
            # step mode falls back to the current wn.
            if self._prev_target is not None and target_wn != self._prev_target:
                self._sweep_dir = 1.0 if target_wn > self._prev_target else -1.0
            elif self._sweep_dir is None and not self.crawl:
                cur = self.get_wavenumber()
                self._sweep_dir = 1.0 if target_wn >= cur else -1.0
            self._prev_target = target_wn
            self.target_wn = target_wn
            dir_str = f"{self._sweep_dir:+.0f}" if self._sweep_dir is not None else "?"
            print(f"[Grating] set_wavenumber({target_wn:.6f}) sweep_dir={dir_str}")
            self.stop_event.clear()
            if self.crawl:
                if self._sweep_dir is None:
                    print("[Grating] crawl: first bin -- holding (direction locks "
                          "from the next target)")
                    return
                # Continuous-glide scan: extend one long move in the sweep
                # direction and return immediately (is_moving stays False, so the
                # Scanner accumulates while the laser sweeps through the bin).
                self._extend_crawl()
                return
            if self.control_thread and self.control_thread.is_alive():
                return
            self.is_moving = True
            self.control_thread = threading.Thread(target=self._control_loop, daemon=True)
            self.control_thread.start()

    def _extend_crawl(self):
        """Issue/extend the continuous glide: one MOV well ahead in the sweep
        direction. Re-issuing mid-glide in the SAME direction just extends the
        target -- the stage never stops, so stiction never bites. Glides AGAINST
        the preferred (smooth) direction get a much shorter leg: the spring-
        restored side releases in lurches, so keep any runaway bounded. Called
        with self.lock held."""
        slope_sign = 1.0 if self.wn_per_unit >= 0 else -1.0
        stage_dir = (self._sweep_dir if self._sweep_dir is not None else 1.0) * slope_sign
        leg = self.crawl_leg
        conservative = self._sweep_dir is not None and \
            self._sweep_dir != self.preferred_wn_dir
        if conservative:
            leg *= self.reverse_leg_scale
        try:
            position = float(self.grating.qPOS())
            move_cmd = position + stage_dir * leg
            self.grating.MOV(move_cmd)
            self._last_move_dir = stage_dir
            wn = self.get_wavenumber()
            print(f"[Grating] crawl: gliding {position:.5f} -> {move_cmd:.5f} "
                  f"(dir={stage_dir:+.0f}, wn={wn:.6f}"
                  f"{', conservative leg' if conservative else ''})")
        except Exception as e:
            print(f"[Grating] crawl MOV failed: {e}")

    def halt(self):
        """Stop the glide in place (MOV to current position; no reversal)."""
        try:
            pos = float(self.grating.qPOS())
            self.grating.MOV(pos)
            print(f"[Grating] crawl halted at {pos:.5f}")
        except Exception as e:
            print(f"[Grating] halt failed: {e}")

    def start_counterdrift(self, target_wn: float):
        self.set_wavenumber(target_wn)

    def stop_counterdrift(self):
        self.stop()

    def is_stable(self, tolerance=None) -> bool:
        """True when the sweep has reached this bin's checkpoint and is holding
        (not moving). NOT a wn-within-tolerance test -- the grating can't hold a
        wavenumber to <backlash, so that would drift-reset forever. Actual wn is
        logged per event; bin on the measured value downstream."""
        with self.lock:
            return not self.is_moving

    def get_wavenumber(self) -> float:
        return float(self.epics.caget(self.wm_pv))

    def stop(self):
        self.stop_event.set()
        thread = self.control_thread
        if thread and thread.is_alive():
            thread.join(timeout=max(5.0, self.poll_interval * 5))
        if self.crawl:
            self.halt()  # don't leave the stage gliding to the leg end
        with self.lock:
            self.is_moving = False

    def reset_sweep(self):
        """Forget the locked sweep direction (call before a scan that runs the
        opposite way). Harmless if not called -- direction re-locks on the second
        bin of the new scan from the target sequence."""
        with self.lock:
            self._sweep_dir = None
            self._prev_target = None
            self._last_move_dir = None

    def update_config(self, new_config: dict):
        with self.lock:
            self.config.update(new_config)
            self.tolerance = float(self.config.get("tolerance", self.tolerance))
            self.poll_interval = float(self.config.get("poll_interval", self.poll_interval))
            self.sweep_step = float(self.config.get("sweep_step", self.sweep_step))
            self.wn_per_unit = float(self.config.get("wn_per_unit", self.wn_per_unit))
            self.backlash = float(self.config.get("backlash", self.backlash))
            self.crawl = bool(self.config.get("crawl", self.crawl))
            self.crawl_leg = float(self.config.get("crawl_leg", self.crawl_leg))
            self.preferred_wn_dir = float(self.config.get("preferred_wn_dir", self.preferred_wn_dir))
            self.reverse_leg_scale = float(self.config.get("reverse_leg_scale", self.reverse_leg_scale))
            new_channel = int(self.config.get("wavechannel", self.wavechannel))
            if new_channel != self.wavechannel:
                self.wavechannel = new_channel
                self.wm_pv = self.config.get("wm_pv", f"LaserLab:wavenumber_{self.wavechannel}")
        print(
            f"[Grating] config updated: tol={self.tolerance}, sweep_step={self.sweep_step}, "
            f"wn_per_unit={self.wn_per_unit}, backlash={self.backlash}, "
            f"poll={self.poll_interval}, chan={self.wavechannel}"
        )

    # ---- Internal helpers ----

    def _read_position(self) -> float:
        try:
            return float(self.grating.qPOS())
        except Exception as e:
            print(f"[Grating] qPOS failed: {e}")
            return 0.0

    def _wait_move_complete(self, move_cmd: float, settle: float = 0.001) -> bool:
        """Block until the stage has arrived at ``move_cmd`` (within ``settle``
        stage units) or a generous timeout. Used after the backlash take-up move,
        which is far longer than one poll interval at the stage's slew rate --
        issuing another MOV mid-slew re-targets from the current position and
        silently cancels the take-up. Returns False if stop_event fired."""
        import time as _time
        # Generous ceiling: lash distance at ~0.001 units/s plus margin.
        timeout = max(10.0, (self.backlash / 0.001) * 2.0)
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if self.stop_event.wait(self.poll_interval):
                return False
            pos = self._read_position()
            if abs(pos - move_cmd) < settle:
                return True
        print(f"[Grating] WARNING: lash take-up move did not complete within "
              f"{timeout:.0f}s (pos={self._read_position():.5f}, cmd={move_cmd:.5f}); continuing.")
        return True

    def _fresh_valid_wn(self, last_wn, stale_polls, max_stale_polls):
        wn = self.get_wavenumber()
        if wn != wn or wn <= 0:  # NaN or non-positive == dropout / no signal
            return wn, stale_polls, False
        if last_wn is not None and wn == last_wn and stale_polls < max_stale_polls:
            return wn, stale_polls + 1, False
        return wn, 0, True

    # ---- Main control loop (fixed-direction monotonic sweep) ----

    def _control_loop(self):
        with self.lock:
            target = self.target_wn
            sweep_dir = self._sweep_dir if self._sweep_dir is not None else 1.0
        print(f"[Grating] sweep starting (target={target:.6f}, dir={sweep_dir:+.0f})")

        slope_sign = 1.0 if self.wn_per_unit >= 0 else -1.0
        stage_dir = sweep_dir * slope_sign  # FIXED stage direction; never reverses
        last_wn = None
        stale_polls = 0
        max_stale_polls = max(6, int(12.0 / self.poll_interval)) if self.poll_interval > 0 else 24
        prev_wn = None
        away_count = 0
        wn = 0.0
        # Stall detection must be NOISE-AWARE: the wavemeter jitters ~+/-0.003
        # cm^-1, so per-read comparisons see phantom progress. Anchor the wn when
        # we start (or after real progress) and only call it progress when the
        # CUMULATIVE advance since the anchor clears the noise floor. While
        # anchored without progress (lash dead-band or a mode plateau), escalate
        # the step geometrically to cross it, then drop back to fine steps.
        PROGRESS_WN = 0.008          # cm^-1 of cumulative advance = real progress
        STALL_STEPS = 6              # anchored steps with no progress -> escalate
        MAX_SCALE = 16.0             # step-size escalation cap (x sweep_step)
        wn_anchor = None
        steps_since_anchor = 0
        step_scale = 1.0

        while not self.stop_event.is_set():
            wn, stale_polls, ok = self._fresh_valid_wn(last_wn, stale_polls, max_stale_polls)
            if not ok:
                if self.stop_event.wait(self.poll_interval):
                    break
                continue
            last_wn = wn

            with self.lock:
                target = self.target_wn

            # Reached: the wavemeter has PASSED this bin's target in the locked
            # sweep direction. Overshoot counts as reached -- we log actual wn.
            if sweep_dir > 0 and wn >= target - self.tolerance:
                break
            if sweep_dir < 0 and wn <= target + self.tolerance:
                break

            # Wrong-sign guard: wn should advance in the sweep direction. If it
            # moves the OTHER way persistently, the slope sign is wrong -> flip
            # (this flips the STAGE direction, not the sweep direction).
            if prev_wn is not None:
                if sweep_dir * (wn - prev_wn) < -PROGRESS_WN:
                    away_count += 1
                    if away_count >= 4:
                        slope_sign = -slope_sign
                        self.wn_per_unit = -self.wn_per_unit
                        stage_dir = sweep_dir * slope_sign
                        away_count = 0
                        print(f"[Grating] wavemeter advancing wrong way -> flipping stage "
                              f"direction (wn_per_unit={self.wn_per_unit:+.4f})")
                else:
                    away_count = 0
            prev_wn = wn

            # Noise-aware stall detection + step escalation.
            if wn_anchor is None:
                wn_anchor = wn
                steps_since_anchor = 0
            elif sweep_dir * (wn - wn_anchor) > PROGRESS_WN:
                # Real cumulative progress: re-anchor and drop back to fine steps.
                wn_anchor = wn
                steps_since_anchor = 0
                if step_scale != 1.0:
                    print("[Grating] wn responding again -> back to fine steps")
                step_scale = 1.0
            else:
                steps_since_anchor += 1
                if steps_since_anchor >= STALL_STEPS:
                    step_scale = min(step_scale * 2.0, MAX_SCALE)
                    steps_since_anchor = 0
                    print(f"[Grating] wn not advancing (lash or mode plateau) -> "
                          f"escalating step to {self.sweep_step * step_scale:.4f}")

            # Fixed-direction move -- NEVER reverses within a scan. Size the move
            # to the remaining error (GLIDE): many micro-steps cause STICK-SLIP
            # (static friction holds the optic while commanded travel winds up,
            # then it breaks free and lurches past the target -- overshoot grows
            # with bin distance). One continuous glide keeps the stage in kinetic
            # friction and wn tracks smoothly; undershoot via gain, then fine-step
            # the last stretch. Stall escalation still applies on top (plateaus).
            position = self._read_position()
            err_mag = abs(target - wn)
            glide = 0.6 * err_mag / max(abs(self.wn_per_unit), 1e-6)
            base = min(max(self.sweep_step, glide), 0.01)  # cap a single glide
            step = stage_dir * base * step_scale
            takeup = self._last_move_dir is not None and stage_dir != self._last_move_dir
            if takeup:
                step += stage_dir * self.backlash  # take up lash once
            move_cmd = position + step

            try:
                if not self.grating.MOV(move_cmd):
                    print(f"[Grating] WARNING: server rejected MOV({move_cmd:.6f})")
            except Exception as e:
                print(f"[Grating] MOV failed: {e}")
                break
            self._last_move_dir = stage_dir

            print(f"[Grating] pos {position:.5f} -> {move_cmd:.5f}  wn={wn:.6f} "
                  f"target={target:.6f} err={target - wn:+.4f} dir={stage_dir:+.0f}"
                  f"{'  (lash take-up)' if takeup else ''}"
                  f"{f'  (x{step_scale:.0f})' if step_scale > 1.0 else ''}")

            if takeup or step_scale > 1.0 or abs(step) > 1.5 * self.sweep_step:
                # Moves longer than one fine step exceed what the stage can slew
                # in a poll interval; issuing the next MOV mid-slew would
                # RE-TARGET from the current position and cancel the move. Wait
                # for the stage to actually arrive before stepping again.
                if not self._wait_move_complete(move_cmd):
                    break

            if self.stop_event.wait(self.poll_interval):
                break

        with self.lock:
            self.is_moving = False
        print(f"[Grating] sweep reached/exiting (wn={wn:.6f}, target={target:.6f})")


if __name__ == "__main__":
    pass
