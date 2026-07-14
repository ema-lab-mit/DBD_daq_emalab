"""Manual grating jog for finding / tuning the Ti:sa lasing point.

Run this ON the laser-lab machine (the one with the HydraPollux on COM5),
in the same Python environment as GoToHdra.py. It moves the PI stage by
hand while you watch the wavemeter (LaserLab:wavenumber_1) so you can walk
the grating until the cavity lases, then fine-tune. No closed loop -- you
are the feedback.

Prompt commands:
    <number>   relative move in stage units, e.g.  0.01   or  -0.05
    +          repeat the current step size in the + direction
    -          repeat the current step size in the - direction
    <Enter>    repeat the last move
    s <n>      set the default step size (units), e.g.  s 0.0005
    v <n>      set stage velocity, e.g.  v 0.5
    p          print position + wavenumber without moving
    a <pos>    absolute move to <pos>
    q          quit (servo left enabled, connection closed)
"""

import ctypes
import time
from pipython import GCSDevice, pitools
from pipython.pidevice.gcserror import GCSError

CONTROLLERNAME = 'HydraPollux'
COMPORT = 5
BAUD = 19200
AXIS = 1
SETTLE_S = 0.5            # let the laser/wavemeter catch up before reading wn

# Wavemeter readout. We read the HighFinesse wlmData.dll DIRECTLY (same call
# the EPICS IOC uses) so the jog tool needs no IOC / Channel Access running --
# it only has to be on the machine the wavemeter is wired to. If the DLL is not
# available we fall back to EPICS.
WLM_DLL = r"C:\Windows\System32\wlmData.dll"
WLM_CHANNEL = 3          # HighFinesse channel carrying the Ti:sa (Matisse is ch1)
WN_PV = 'LaserLab:wavenumber_3'   # EPICS fallback only
_THZ_PER_WN = 0.0299792458        # GetFrequencyNum returns THz; /this = cm^-1

_wlm = None
_wlm_loaded = False


def _load_wlm():
    """Load wlmData.dll once; return the handle or None if unavailable."""
    global _wlm, _wlm_loaded
    if _wlm_loaded:
        return _wlm
    _wlm_loaded = True
    try:
        dll = ctypes.WinDLL(WLM_DLL)
        dll.GetFrequencyNum.argtypes = [ctypes.c_long, ctypes.c_double]
        dll.GetFrequencyNum.restype = ctypes.c_double
        _wlm = dll
    except Exception as e:
        print(f'  [wlmData.dll unavailable ({e}); falling back to EPICS]')
        _wlm = None
    return _wlm


def read_wn():
    """Wavenumber (cm^-1) on WLM_CHANNEL, or nan if no/low signal."""
    dll = _load_wlm()
    if dll is not None:
        freq_thz = dll.GetFrequencyNum(WLM_CHANNEL, 0.0)
        # Positive = a real measurement. Negative = a HighFinesse error code
        # (e.g. no signal / underexposed / overexposed), not a wavenumber.
        if freq_thz > 0:
            return freq_thz / _THZ_PER_WN
        return float('nan')
    # Fallback: EPICS (needs the IOC + Channel Access reachable).
    try:
        import epics
        return float(epics.caget(WN_PV))
    except Exception as e:
        print(f'  [wn read failed: {e}]')
        return float('nan')


def show(pi, settle=True):
    if settle:
        time.sleep(SETTLE_S)
    pos = pi.qPOS(AXIS)[AXIS]
    print(f'  pos {pos:+.5f}   wn {read_wn():.4f} cm^-1')
    return pos


def main():
    with GCSDevice(CONTROLLERNAME) as pi:
        pi.ConnectRS232(comport=COMPORT, baudrate=BAUD)
        print('connected:', pi.qIDN().strip())

        # The Pollux GCS DLL can fire a spurious GCSError(0) on the first
        # command after connect (stale error buffer). Drain it once, then
        # enable the servo tolerantly: a GCSError(0) on SVO means nothing is
        # actually wrong, so verify the real servo state instead of crashing.
        try:
            pi.qERR()
        except GCSError:
            pass
        try:
            pi.SVO(AXIS, 1)
        except GCSError as e:
            try:
                pi.qERR()
                pi.SVO(AXIS, 1)          # one clean retry
            except GCSError as e2:
                try:
                    servo_on = bool(pi.qSVO(AXIS)[AXIS])
                except Exception:
                    servo_on = False
                if servo_on:
                    print(f'  SVO threw {e2}, but servo reads ON -- continuing')
                else:
                    print(f'  WARNING: could not enable servo ({e2}); '
                          'moves may be rejected. Continuing anyway.')

        pos = pi.qPOS(AXIS)[AXIS]
        step = 0.001       # default jog size; tune with  s <n>
        last_delta = 0.0
        print(f'start: pos {pos:+.5f}   wn {read_wn():.4f} cm^-1   step {step}')
        print('commands: <num> move | +/- step | <Enter> repeat | '
              's <n> setstep | v <n> vel | a <pos> abs | p print | q quit')

        while True:
            cmd = input('jog> ').strip()

            if cmd == 'q':
                break
            if cmd == 'p':
                pos = show(pi, settle=False)
                continue
            if cmd == '':
                delta = last_delta
            elif cmd == '+':
                delta = step
            elif cmd == '-':
                delta = -step
            elif cmd.startswith('s'):
                try:
                    step = abs(float(cmd[1:]))
                    print(f'  step = {step}')
                except ValueError:
                    print('  usage: s <number>')
                continue
            elif cmd.startswith('v'):
                try:
                    vel = float(cmd[1:])
                    pi.VEL(AXIS, vel)
                    print(f'  velocity = {pi.qVEL(AXIS)[AXIS]}')
                except (ValueError, Exception) as e:
                    print(f'  velocity set failed: {e}')
                continue
            elif cmd.startswith('a'):
                try:
                    target = float(cmd[1:])
                except ValueError:
                    print('  usage: a <absolute position>')
                    continue
                pi.MOV(AXIS, target)
                pitools.waitontarget(pi, axes=AXIS)
                pos = show(pi)
                last_delta = 0.0
                continue
            else:
                try:
                    delta = float(cmd)
                except ValueError:
                    print('  ? unrecognized; type a number, +, -, s/v/a, p, or q')
                    continue

            if delta == 0.0:
                pos = show(pi, settle=False)
                continue

            pi.MOV(AXIS, pos + delta)
            pitools.waitontarget(pi, axes=AXIS)
            last_delta = delta
            pos = show(pi)

    print('done (connection closed, servo left on)')


if __name__ == '__main__':
    main()
