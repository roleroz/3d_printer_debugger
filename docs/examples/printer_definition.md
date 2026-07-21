# Example printer definition document

This is an illustrative example of the printer knowledge-base document the system ingests, as
described in [spec §5.1](../spec.md#51-printer-management). It is modelled on the real document
the author maintains at `~/git/ai_agent/3d_printers.md`.

It is an example, not a template to be filled in mechanically and not a schema. The document is
written and maintained by the user for their own benefit; the system reads it as-is. The point
of this file is to show the shape and the level of detail that makes a document useful to the
system, not to constrain how anyone writes theirs.

What the system relies on being present, somewhere in each printer's section:

- A heading that names the printer.
- A hostname or address, so the printer can be reached over Moonraker.
- A pointer to the printer's Klipper configuration files, when they exist locally.

Everything else — hardware inventory, known problems, planned changes, calibration status — is
prose the system reads for context. The richer it is, the better the diagnosis. Nothing below
is mandatory, and the surrounding sections (slicer, materials, storage) are read as
printer-independent context.

Everything from the horizontal rule down is the example document itself.

---

# 3D Printers

Reference info for calibrating, updating, and debugging my 3D printers. Both printers run stock
Klipper (`Klipper3d/klipper`, not a fork), are managed through Mainsail, and have KlipperScreen
for local touchscreen control.

Configuration files for both printers are tracked in `~/git/printers_config`, under a
subdirectory named after each printer's hostname (`trident/`, `switchwire/`).

## Voron Trident 300

- Hostname: `trident` (resolvable via DNS under the default search domain)
- Firmware: Klipper
- Web UI: Mainsail
- Config files: `~/git/printers_config/trident/`
- SBC: Raspberry Pi 4, 4GB
- Kinematics: CoreXY, 3 independent Z steppers with automatic Z tilt adjust
- Build volume: 300 x 300 x 250mm (from `position_max` in printer.cfg)
- Mainboard: BTT Octopus v1.1, connected over USB serial
- Toolhead board: BTT EBB36 v1.2 over CANbus (`canbus_uuid: 1a2b3c4d5e6f`, bitrate 1000000)
- Toolhead mount: Voron StealthBurner with a Clockwork 2 extruder
- XY drivers: TMC5160, motor `ldo-42sth48-2004mah`
- X/Y endstops: physical mechanical switches
- Z drivers (x3): TMC2209
- Extruder: Clockwork 2, `rotation_distance: 22.6789`, `gear_ratio: 50:10`
- Nozzle: E3D Revo Brass High Flow, 0.4mm
- Hotend: E3D Revo Voron, 60W heater
- Bed: ATC Semitec 104NT-4-R025H42G thermistor, PID tuned (kp=54.201, ki=1.882, kd=390.145)
- Bed surface: textured PEI spring steel flex plate, 310x310mm, max 110°C
- Extruder PID: active values are in printer.cfg's SAVE_CONFIG block (kp=25.900, ki=2.740,
  kd=61.200)
- Probe: Beacon RevH eddy-current probe, z_offset calibrated, contact method used for homing
- Bed leveling: automatic 7x7 bicubic mesh, re-run before every print
- Gantry leveling: `z_tilt_adjust` configured, retry tolerance 0.0075
- Input shaper: ADXL345 on the toolhead board, resonance test point at (150, 150, 20)
- Filament sensor: BTT Smart Filament Sensor v2.0, switch + motion, both pause on runout
- Enclosure: fully enclosed, chamber thermistor on the bed frame
- Air filter: Nevermore v5 Duo
- Chassis LEDs: 2x 300mm daylight strips, wired in parallel as one neopixel chain
- Webcam: Logitech C270, via crowsnest (`/dev/video0`)
- PSU: Meanwell LRS-350-24, 24V 350W
- Klipper plugins in use: `klipper_tmc_autotune`
- Calibration status (as of 2026-06-14): bed and extruder PID last run 2026-06. Input shaping
  last run 2025-11, before the toolhead was rebuilt — almost certainly stale. Bed mesh is not
  a one-time calibration here; it runs before every print.

## Voron Switchwire

- Hostname: `switchwire` (resolvable via DNS under the default search domain)
- Firmware: Klipper
- Web UI: Mainsail
- Config files: `~/git/printers_config/switchwire/`
- SBC: Raspberry Pi 3B+
- Kinematics: bed-slinger CoreXZ, single Z stepper
- Build volume: 250 x 210 x 210mm
- Mainboard: BTT SKR Mini E3 v3.0, USB serial, no CANbus
- Toolhead: Mini StealthBurner, direct drive, wired back to the mainboard (no toolhead board)
- Drivers: TMC2209 on all axes
- X/Z endstops: physical mechanical switches. Y homes on a switch at the front of the bed
- Extruder: Mini Afterburner CW, `rotation_distance: 22.4123`, `gear_ratio: 50:17`
- Nozzle: hardened steel, 0.4mm
- Hotend: Phaetus Dragon HF, 50W heater
- Bed: 200x200mm heated bed, PID tuned (kp=71.104, ki=1.402, kd=901.300)
- Bed surface: smooth PEI spring steel, max 100°C
- Bed leveling: manual only — `[screws_tilt_adjust]` defined, no probe, no automatic mesh
- Input shaper: no accelerometer installed. Shaper values were set from a ringing tower test
  and are entered by hand in printer.cfg
- Filament sensor: none
- Enclosure: none — open frame, on a shelf in an unheated room
- Webcam: none
- Calibration status (as of 2026-05-02): bed PID last run 2026-05. Hotend PID has never been
  run since the hotend was replaced. Input shaping is manual-only, see above.

## Slicer

OrcaSlicer, used for both printers.

## Materials

PLA and PETG on the Switchwire; the Trident handles ABS and ASA as well since it is enclosed.
Which printer gets a job is decided by part size and by which machine is free, not by material,
except that anything in ABS/ASA goes on the Trident. Treat per-print temperatures and cooling
as material-dependent rather than tied to a specific printer.

## Filament storage

Filament is kept in sealed boxes with desiccant. ABS and ASA are dried before use; PETG is
dried if it has been out of the box for more than a few days.

## Known problems

- Both
  - Occasional first-layer scarring when the nozzle is dirty from a previous print.
- Trident
  - Intermittent CAN bus disconnects under heavy toolhead fan load. Suspected wiring, not yet
    tracked down. Shows up as `Lost communication with MCU 'EBB36'` in the log.
- Switchwire
  - Ringing on the Y axis at higher accelerations, worse with tall parts. Expected for a
    bed-slinger with hand-tuned shaper values and no accelerometer.
  - First layer is inconsistent front-to-back, which is probably a manual leveling limitation.

## Planned changes

- Trident
  - Replace the CAN wiring harness to chase the intermittent disconnects.
  - Re-run input shaping now that the toolhead has been rebuilt.
- Switchwire
  - Add an ADXL345 so input shaping can actually be measured instead of estimated.
  - Run hotend PID, which has never been done since the hotend was replaced.

## Open questions (not determinable from config alone)

- The Switchwire's PSU model — no label visible without disassembly.
- Whether the Trident's bed thermistor is the original or the one from the spares kit.
