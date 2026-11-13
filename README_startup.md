# Laser-scan startup guide

How to bring up a **real** (non-simulated) laser scan end to end. The setup spans
**two computers**:

| Role | Machine | User | Runs |
|---|---|---|---|
| Laser lab | `10.54.6.1` | `admin` | Matisse Commander, `laser_server.py`, wavemeter IOC (`Documents\Wavemeter\wavemeter_testing.py`) |
| Beamline / DAQ | `10.54.5.139` | `EMALAB` | `main.py` (this repo) |

The HighFinesse wavemeter is physically connected to the **laser-lab** machine, so
the wavemeter IOC must run there.

---

## Quick start (scripts)

Two helper scripts live next to this file:

1. On the **laser-lab computer**, run [`start_laserlab.ps1`](start_laserlab.ps1) —
   clears stale Python, starts `laser_server.py` + the wavemeter IOC each in its own
   window, and verifies the laser ping and `wavenumber_1`.
   *(One-time: edit `$WavemeterIoc` in that script to the real path on that machine.)*
2. On the **beamline computer**, run [`start_beamline.ps1`](start_beamline.ps1) — sets
   `EPICS_CA_ADDR_LIST=10.54.6.1`, refuses to launch if the laser ping fails or the
   wavenumber reads 0.0/None, then starts the DAQ.

The scripts do **not** launch Matisse Commander or do the CounterDrift pre-positioning —
those are manual steps below.

---

## Full procedure (CounterDrift scan)

- **Units first — this is what makes the laser run away.** In Matisse Commander set
  **Position Display Mode = nm** *and* **CounterDrift dialog Unit = nm**. The CD readout
  must show **~792 nm**, *not* ~12625 (cm^-1). A fresh Commander session can default to
  the wrong unit.
- On Matisse Commander, use **CD to place the laser** at the start wavenumber, then
  **turn CD OFF** and close the CD tab.
- On the **laser-lab computer**, start the **laser server**:
  `python C:\Users\admin\Documents\ControllerC663_testing\laser_server.py`
  — wait for `RUNNING ON PORT 8000`.
- On the **laser-lab computer**, start the **wavemeter EPICS IOC**:
  `python C:\Users\admin\Documents\Wavemeter\wavemeter_testing.py` — wait for `started`. Requires the HighFinesse WLM
  software running. *(Without it, every `wavemeter_wn` logs 0.0 silently.)*
- On the **beamline computer**, set `$env:EPICS_CA_ADDR_LIST="10.54.6.1"`, then open the
  **DAQ**. *(Or just run `start_beamline.ps1`.)*
- **Re-open the CD dialog** in Matisse Commander but **do NOT turn it on** — the DAQ
  engages CD itself. Confirm its readout reads ~792 nm.
- **Verify**: laser ping True and `LaserLab:wavenumber_1` reads a real number (not 0.0).
- **Run the scan** from the DAQ, starting at the selected wavenumber.
- **Next isotope**: before closing the DAQ, enter the next starting wavenumber in
  **Start WN**.
- **Shutdown**: end the laser server **and** the wavemeter IOC on the laser-lab computer.

---

## Troubleshooting

**Laser runs away in one direction when CD engages / DAQ prints `COUNTERDRIFT RUNAWAY`.**
The Matisse **CounterDrift dialog Unit is not nm** (or feedback sign inverted). The DAQ
sends the setpoint as a bare number meaning nm (~792); if CD reads it as cm^-1 (~792
cm^-1) the target is far below the measured ~12625 cm^-1 and the servo drives the laser
down without bound. Fix: set CD Unit = nm (readout ~792, not ~12625), re-park the laser,
restart the DAQ. The controller now auto-disengages on this (`runaway_limit` in
`control_settings.laser`) and latches an aborted state until restart.

**`cannot connect to LaserLab:wavenumber_*` or wavenumbers read 0.0.**
The wavemeter IOC isn't running on the laser-lab machine, or EPICS can't reach it. The
lab switch blocks broadcast, so set `EPICS_CA_ADDR_LIST=10.54.6.1` on the beamline
machine. `get_wnum()` swallows failures and returns 0.0 with no error — data taken before
the IOC connects has bogus `wavemeter_wn` columns, so always verify before scanning.

**`cannot connect to LaserLab:spectrum_peak`.**
That PV comes from a **separate** spectrometer publisher, not the wavemeter IOC, so it can
be 0.0/None even when wavenumbers are fine. Only matters if your analysis uses the
spectrum column.

**`MCP_WM_GET_WAVELENGTH -> 0.0 ... HighFinesse_WaitForEvent error 5500`.**
Matisse Commander's own wavemeter read is failing (possibly contention with the IOC for
the HighFinesse event mechanism). This is *not* what drives the laser, but it disables the
DAQ's pre-flight unit cross-check (`_verify_setup`), so the runaway guard above is your
safety net until it's fixed. Open issue.

**`status is 1. Better luck next time` (repeating).**
The time-tagger has no trigger yet — normal at idle. Only a problem if it never flips to
status 0; then check the trigger cable / level / source.
